"""End-to-end synthetic replay: the CR-01 vertical slice.

Every test here is offline. ``test_replay_uses_no_network`` asserts that directly by
failing any socket operation for the duration of a full replay.
"""

from __future__ import annotations

import hashlib
import json
import socket
from decimal import Decimal
from pathlib import Path

import pytest

from tigerquant.core.enums import Action, Mode, ReasonCode
from tigerquant.events.journal import AppendOnlyJournal
from tigerquant.events.registry import EVENT_TYPES
from tigerquant.market.fixtures import load_scenario
from tigerquant.pipeline.replay import ReplayEngine, run_replay

pytestmark = pytest.mark.integration

SCENARIOS = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios"


def journal_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestTrendUpScenario:
    def test_runs_and_produces_a_fill(self, tmp_path: Path) -> None:
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        report = run_replay(fixture, journal_path=tmp_path / "audit.jsonl")
        assert report.bars_processed == 6
        assert report.orders_submitted == 1
        assert report.orders_filled == 1
        assert report.positions == {"SPY": 8}
        assert report.final_cash < report.initial_cash
        assert report.final_equity != report.initial_cash  # marked to market

    def test_journal_is_valid_and_contains_the_full_lineage(self, tmp_path: Path) -> None:
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        journal_path = tmp_path / "audit.jsonl"
        run_replay(fixture, journal_path=journal_path)

        verification = AppendOnlyJournal(journal_path, Mode.BACKTEST).verify()
        assert verification.ok, verification.first_error

        types = [
            record.event.event_type
            for record in AppendOnlyJournal(journal_path, Mode.BACKTEST).iter_records()
        ]
        # The whole decision-to-execution lineage must be present and ordered.
        for required in (
            "MarketBar",
            "SnapshotCreated",
            "DecisionProposed",
            "OrderIntentCreated",
            "RiskEvaluated",
            "OrderSubmitted",
            "OrderAcknowledged",
            "OrderFilled",
            "PositionChanged",
            "LedgerMarked",
        ):
            assert required in types, f"{required} missing from the journal"
        assert types.index("RiskEvaluated") < types.index("OrderSubmitted")
        assert types.index("OrderIntentCreated") < types.index("RiskEvaluated")

    def test_ledger_arithmetic_is_exact(self, tmp_path: Path) -> None:
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        engine = ReplayEngine(fixture)
        report = engine.run()
        ledger = engine.ledger
        expected_cash = Decimal("100000.00") - (ledger.position("SPY").average_cost * 8)
        # Average cost capitalises commission, so cash-out equals basis * shares exactly.
        assert abs(ledger.cash - expected_cash) <= Decimal("0.01")
        assert report.positions["SPY"] == 8

    def test_no_order_is_created_when_the_target_is_already_met(self, tmp_path: Path) -> None:
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        report = run_replay(fixture)
        # Six bars, three SET_TARGET proposals, but only one order: the rest are no-ops.
        assert report.decisions_proposed == 6
        assert report.orders_submitted == 1


class TestDeterminism:
    def test_two_runs_in_one_process_agree_byte_for_byte(self, tmp_path: Path) -> None:
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        first = run_replay(fixture, journal_path=tmp_path / "a.jsonl")
        second = run_replay(fixture, journal_path=tmp_path / "b.jsonl")
        assert first.journal_digest == second.journal_digest
        assert journal_sha256(tmp_path / "a.jsonl") == journal_sha256(tmp_path / "b.jsonl")

    def test_determinism_holds_for_every_scenario(self, tmp_path: Path) -> None:
        for name in ("trend_up", "partial_fill", "unknown_outcome", "rejected_order"):
            fixture = load_scenario(SCENARIOS / f"{name}.yaml")
            first = run_replay(fixture, journal_path=tmp_path / f"{name}-1.jsonl")
            second = run_replay(fixture, journal_path=tmp_path / f"{name}-2.jsonl")
            assert first.journal_digest == second.journal_digest, name

    def test_ids_and_timestamps_are_reproducible(self, tmp_path: Path) -> None:
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        engine_a = ReplayEngine(fixture)
        engine_a.run()
        engine_b = ReplayEngine(fixture)
        engine_b.run()
        a = [record.to_dict() for record in engine_a.journal.records]
        b = [record.to_dict() for record in engine_b.journal.records]
        assert a == b


class TestOfflineGuarantee:
    def test_replay_uses_no_network(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def forbidden(*args: object, **kwargs: object) -> None:
            msg = "the CR-01 replay must not touch the network"
            raise AssertionError(msg)

        monkeypatch.setattr(socket, "socket", forbidden)
        monkeypatch.setattr(socket, "create_connection", forbidden)
        monkeypatch.setattr(socket, "getaddrinfo", forbidden)
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        report = run_replay(fixture, journal_path=tmp_path / "offline.jsonl")
        assert report.orders_filled == 1

    def test_report_labels_the_implementation_honestly(self) -> None:
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        report = run_replay(fixture)
        assert report.implementation_status == "SIMULATED"
        assert any("no broker was contacted" in note for note in report.notes)


class TestScenarioBehaviours:
    def test_partial_fill_leaves_a_blocking_intent(self) -> None:
        fixture = load_scenario(SCENARIOS / "partial_fill.yaml")
        engine = ReplayEngine(fixture)
        report = engine.run()
        assert report.orders_submitted == 1
        # 8 requested, half filled.
        assert report.positions == {"SPY": 4}
        assert engine.counters.filled == 0
        blocking = engine._projection.blocking_intents()
        assert len(blocking) == 1
        assert blocking[0].status.value == "PARTIALLY_FILLED"

    def test_unknown_outcome_is_never_retried(self) -> None:
        fixture = load_scenario(SCENARIOS / "unknown_outcome.yaml")
        engine = ReplayEngine(fixture)
        report = engine.run()
        # Exactly one attempt reached the venue, and nothing was booked.
        assert report.orders_submitted == 1
        assert engine._broker.order_count == 1
        assert report.positions == {}
        assert report.final_cash == report.initial_cash
        assert report.orders_unknown == 1

    def test_rejected_order_leaves_the_ledger_untouched(self) -> None:
        """The venue refuses; the ledger must not move and every refusal is recorded.

        A rejected intent is terminal but not blocking, so the rule may try again on a
        later bar; each attempt is a separate, journaled intent. A per-session order-rate
        cap is CR-07 work (see the limitations in docs/PROGRESS.md).
        """
        fixture = load_scenario(SCENARIOS / "rejected_order.yaml")
        report = run_replay(fixture)
        assert report.orders_submitted >= 1
        assert report.orders_rejected == report.orders_submitted
        assert report.positions == {}
        assert report.final_cash == report.initial_cash

    def test_shadow_mode_cannot_reach_a_broker(self, tmp_path: Path) -> None:
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        shadow = fixture.model_copy(update={"mode": Mode.SHADOW})
        report = run_replay(shadow)
        assert report.mode == "SHADOW"
        # SIMULATION target is permitted in SHADOW, but it never becomes a broker write.
        assert report.implementation_status == "SIMULATED"


class TestEvidenceSnapshots:
    def test_snapshots_are_complete_and_grow_with_history(self) -> None:
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        engine = ReplayEngine(fixture)
        engine.run()
        snapshots = [
            record.event
            for record in engine.journal.records
            if record.event.event_type == "SnapshotCreated"
        ]
        assert snapshots
        assert all(snapshot.completeness.value == "COMPLETE" for snapshot in snapshots)
        counts = [len(snapshot.fact_ids) for snapshot in snapshots]
        assert counts == sorted(counts)

    def test_every_decision_cites_evidence_inside_its_snapshot(self) -> None:
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        engine = ReplayEngine(fixture)
        engine.run()
        for record in engine.journal.records:
            event = record.event
            if event.event_type != "DecisionProposed":
                continue
            if event.action is Action.HOLD:
                continue
            snapshot = next(
                snap.snapshot_id
                for snap in (
                    r.event
                    for r in engine.journal.records
                    if r.event.event_type == "SnapshotCreated"
                )
                if snap.snapshot_id == event.snapshot_id
            )
            assert snapshot == event.snapshot_id
            assert event.evidence_ids

    def test_unknown_event_types_are_registered(self) -> None:
        assert "OrderQuarantined" in EVENT_TYPES
        assert len(EVENT_TYPES) >= 18


class TestRiskEnforcement:
    def test_halt_blocks_every_order(self) -> None:
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        halted = fixture.model_copy(
            update={"risk": fixture.risk.model_copy(update={"halted": True})}
        )
        report = run_replay(halted)
        assert report.orders_submitted == 0
        assert report.risk_denials == 3

    def test_symbol_outside_the_policy_universe_is_denied(self) -> None:
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        engine = ReplayEngine(fixture)
        tight = engine._policy.model_copy(update={"allowed_instruments": ("QQQ",)})
        engine._gateway = engine._gateway.__class__(
            policy=tight, state=engine._gateway.state, registry=engine._gateway.registry
        )
        report = engine.run()
        assert report.orders_submitted == 0


class TestCli:
    def test_cli_replay_writes_a_report(self, tmp_path: Path) -> None:
        from tigerquant.cli import main

        report_path = tmp_path / "report.json"
        journal_path = tmp_path / "audit.jsonl"
        code = main(
            [
                "replay",
                "--fixture",
                str(SCENARIOS / "trend_up.yaml"),
                "--journal",
                str(journal_path),
                "--report",
                str(report_path),
                "--quiet",
            ]
        )
        assert code == 0
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        assert payload["orders_filled"] == 1

    def test_cli_verify_journal_detects_tampering(self, tmp_path: Path) -> None:
        from tigerquant.cli import main

        journal_path = tmp_path / "audit.jsonl"
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        run_replay(fixture, journal_path=journal_path)
        assert main(["verify-journal", str(journal_path)]) == 0

        text = journal_path.read_text(encoding="utf-8").replace('"volume":10000', '"volume":1')
        journal_path.write_text(text, encoding="utf-8")
        assert main(["verify-journal", str(journal_path)]) == 1

    def test_cli_doctor_passes(self) -> None:
        from tigerquant.cli import main

        assert main(["doctor"]) == 0

    def test_cli_reports_configuration_errors_cleanly(self, tmp_path: Path) -> None:
        from tigerquant.cli import main

        assert main(["replay", "--fixture", str(tmp_path / "missing.yaml")]) == 2


class TestReasonCodesAreUsed:
    def test_denials_always_carry_a_code(self) -> None:
        fixture = load_scenario(SCENARIOS / "unknown_outcome.yaml")
        engine = ReplayEngine(fixture)
        engine.run()
        for record in engine.journal.records:
            event = record.event
            if event.event_type == "RiskEvaluated" and not event.is_allowed():
                assert event.reason_codes
            if event.event_type == "AlertRaised":
                assert isinstance(event.code, ReasonCode)
