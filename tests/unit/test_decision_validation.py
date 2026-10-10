"""Decision contract: schema enforcement, evidence discipline, recomputation."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tigerquant.core.enums import Action, ReasonCode
from tigerquant.core.errors import DecisionValidationError
from tigerquant.decision.contract import (
    TradeDecision,
    load_decision_schema,
    validate_against_schema,
)
from tigerquant.decision.parsing import DecisionParseError, parse_decision_json
from tigerquant.decision.validation import DecisionContext, DecisionValidator
from tigerquant.events.market import MarketBar
from tigerquant.evidence.ingest import fact_from_bar
from tigerquant.evidence.snapshot import SnapshotBuilder
from tigerquant.evidence.sources import default_test_registry
from tigerquant.evidence.store import EvidenceStore

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
MAX_AGE = 900

BASE = {
    "snapshot_id": "snap:test",
    "model_version": "test-model",
    "action": "SET_TARGET",
    "instrument": "SPY",
    "target_weight": Decimal("0.05"),
    "horizon": "5 days",
    "thesis": "momentum",
    "counter_thesis": "mean reversion",
    "invalidation": "close below entry",
    "evidence_ids": ["fact:abc"],
    "uncertainties": ["fixture data"],
    "decision_id": "bt-dec_test",
}


def _bar(
    close: str, minutes: int = 0, instrument: str = "SPY", source: str = "fixture:test"
) -> MarketBar:
    occurred = AT + dt.timedelta(minutes=minutes)
    return MarketBar(
        event_id=f"ev-{instrument}-{minutes}",
        mode="BACKTEST",
        seq=minutes,
        occurred_at=occurred,
        recorded_at=occurred,
        correlation_id="c",
        instrument=instrument,
        interval="1m",
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100,
        available_at=occurred + dt.timedelta(minutes=1),
        source_id=source,
        content_hash="sha256:" + f"{minutes:064d}",
        reference_price=close,
    )


class TestSchema:
    def test_repository_schema_loads(self) -> None:
        schema = load_decision_schema()
        assert schema["title"]

    def test_minimal_valid_decision_passes(self) -> None:
        validate_against_schema(dict(BASE))

    def test_extra_field_is_rejected(self) -> None:
        with pytest.raises(DecisionValidationError) as excinfo:
            validate_against_schema({**BASE, "quantity": 1000})
        assert excinfo.value.reason_code is ReasonCode.SCHEMA_INVALID

    def test_model_supplied_quantity_cannot_sneak_through(self) -> None:
        """The strongest form of "the model does not size the order"."""
        for sneaky in ("quantity", "notional", "price", "buying_power", "cost"):
            with pytest.raises(DecisionValidationError):
                validate_against_schema({**BASE, sneaky: "1"})

    def test_set_target_requires_instrument_and_weight(self) -> None:
        with pytest.raises(DecisionValidationError):
            validate_against_schema({**BASE, "instrument": None})

    def test_no_trade_is_valid_without_instrument(self) -> None:
        validate_against_schema(
            {
                **BASE,
                "action": "NO_TRADE",
                "instrument": None,
                "target_weight": None,
                "evidence_ids": [],
            }
        )

    def test_weight_out_of_range_is_rejected(self) -> None:
        with pytest.raises(DecisionValidationError):
            validate_against_schema({**BASE, "target_weight": Decimal("1.5")})

    def test_typed_model_rejects_extra_fields_too(self) -> None:
        with pytest.raises(Exception, match="extra_forbidden|Extra inputs"):
            TradeDecision.model_validate({**BASE, "quantity": 5})


class TestParsing:
    def test_extracts_json_from_prose(self) -> None:
        parsed = parse_decision_json('Here you go:\n{"action": "NO_TRADE"}\nThanks!')
        assert parsed == {"action": "NO_TRADE"}

    def test_decimals_are_exact(self) -> None:
        parsed = parse_decision_json('{"target_weight": 0.05}')
        assert parsed["target_weight"] == Decimal("0.05")
        assert isinstance(parsed["target_weight"], Decimal)

    def test_missing_json_is_an_error(self) -> None:
        with pytest.raises(DecisionParseError):
            parse_decision_json("no json here")

    def test_braces_inside_strings_do_not_confuse_the_scanner(self) -> None:
        parsed = parse_decision_json('{"thesis": "a } b", "action": "NO_TRADE"}')
        assert parsed["thesis"] == "a } b"


class TestValidator:
    def _fixture(self) -> tuple[EvidenceStore, SnapshotBuilder, str, str]:
        store = EvidenceStore(default_test_registry())
        builder = SnapshotBuilder(store, max_age_seconds=MAX_AGE)
        fact = fact_from_bar(_bar("600.00"), max_age_seconds=MAX_AGE)
        store.add(fact)
        return store, builder, fact.fact_id, AT

    def _context(self, snapshot: object, **overrides: object) -> DecisionContext:
        base = {
            "snapshot": snapshot,
            "allowed_instruments": ("SPY",),
            "expected_decision_id": "bt-dec_test",
            "expected_model_version": "test-model",
            "expected_prompt_version": "p1",
            "current_weight": Decimal(0),
            "mark_price": Decimal("600.00"),
            "equity": Decimal(100000),
        }
        base.update(overrides)
        return DecisionContext(**base)  # type: ignore[arg-type]

    def test_accepts_a_well_formed_decision(self) -> None:
        store, builder, fact_id, _as_of = self._fixture()
        snapshot = builder.build(as_of=AT + dt.timedelta(minutes=1), fact_ids=(fact_id,))
        decision = DecisionValidator(store, schema=load_decision_schema()).validate(
            {**BASE, "snapshot_id": snapshot.snapshot_id, "evidence_ids": [fact_id]},
            self._context(snapshot),
        )
        assert decision.action is Action.SET_TARGET

    def test_fabricated_evidence_id_is_rejected(self) -> None:
        store, builder, fact_id, _ = self._fixture()
        snapshot = builder.build(as_of=AT + dt.timedelta(minutes=1), fact_ids=(fact_id,))
        with pytest.raises(DecisionValidationError) as excinfo:
            DecisionValidator(store, schema=load_decision_schema()).validate(
                {**BASE, "snapshot_id": snapshot.snapshot_id, "evidence_ids": ["fact:invented"]},
                self._context(snapshot),
            )
        assert excinfo.value.reason_code is ReasonCode.EVIDENCE_UNRESOLVED

    def test_evidence_outside_the_snapshot_is_rejected(self) -> None:
        store, builder, fact_id, _ = self._fixture()
        other = fact_from_bar(_bar("601.00", minutes=1), max_age_seconds=MAX_AGE)
        store.add(other)
        snapshot = builder.build(as_of=AT + dt.timedelta(minutes=1), fact_ids=(fact_id,))
        with pytest.raises(DecisionValidationError, match="not part of snapshot"):
            DecisionValidator(store, schema=load_decision_schema()).validate(
                {**BASE, "snapshot_id": snapshot.snapshot_id, "evidence_ids": [other.fact_id]},
                self._context(snapshot),
            )

    def test_snapshot_mismatch_is_rejected(self) -> None:
        store, builder, fact_id, _ = self._fixture()
        snapshot = builder.build(as_of=AT + dt.timedelta(minutes=1), fact_ids=(fact_id,))
        with pytest.raises(DecisionValidationError) as excinfo:
            DecisionValidator(store, schema=load_decision_schema()).validate(
                {**BASE, "snapshot_id": "snap:forged", "evidence_ids": [fact_id]},
                self._context(snapshot),
            )
        assert excinfo.value.reason_code is ReasonCode.SNAPSHOT_MISMATCH

    def test_wrong_decision_id_is_rejected(self) -> None:
        store, builder, fact_id, _ = self._fixture()
        snapshot = builder.build(as_of=AT + dt.timedelta(minutes=1), fact_ids=(fact_id,))
        with pytest.raises(DecisionValidationError, match="decision_id"):
            DecisionValidator(store, schema=load_decision_schema()).validate(
                {
                    **BASE,
                    "snapshot_id": snapshot.snapshot_id,
                    "evidence_ids": [fact_id],
                    "decision_id": "bt-dec_other",
                },
                self._context(snapshot),
            )

    def test_unpinned_model_version_is_rejected(self) -> None:
        store, builder, fact_id, _ = self._fixture()
        snapshot = builder.build(as_of=AT + dt.timedelta(minutes=1), fact_ids=(fact_id,))
        with pytest.raises(DecisionValidationError) as excinfo:
            DecisionValidator(store, schema=load_decision_schema()).validate(
                {
                    **BASE,
                    "snapshot_id": snapshot.snapshot_id,
                    "evidence_ids": [fact_id],
                    "model_version": "some-other-model",
                },
                self._context(snapshot),
            )
        assert excinfo.value.reason_code is ReasonCode.MODEL_VERSION_UNPINNED

    def test_instrument_outside_the_universe_is_rejected(self) -> None:
        store, builder, fact_id, _ = self._fixture()
        snapshot = builder.build(as_of=AT + dt.timedelta(minutes=1), fact_ids=(fact_id,))
        with pytest.raises(DecisionValidationError) as excinfo:
            DecisionValidator(store, schema=load_decision_schema()).validate(
                {
                    **BASE,
                    "snapshot_id": snapshot.snapshot_id,
                    "evidence_ids": [fact_id],
                    "instrument": "TSLA",
                },
                self._context(snapshot),
            )
        assert excinfo.value.reason_code is ReasonCode.SYMBOL_NOT_ALLOWED

    def test_empty_justification_is_rejected(self) -> None:
        store, builder, fact_id, _ = self._fixture()
        snapshot = builder.build(as_of=AT + dt.timedelta(minutes=1), fact_ids=(fact_id,))
        with pytest.raises(DecisionValidationError, match="counter_thesis"):
            DecisionValidator(store, schema=load_decision_schema()).validate(
                {
                    **BASE,
                    "snapshot_id": snapshot.snapshot_id,
                    "evidence_ids": [fact_id],
                    "counter_thesis": "   ",
                },
                self._context(snapshot),
            )

    def test_trade_without_evidence_is_rejected(self) -> None:
        store, builder, fact_id, _ = self._fixture()
        snapshot = builder.build(as_of=AT + dt.timedelta(minutes=1), fact_ids=(fact_id,))
        with pytest.raises(DecisionValidationError, match="no evidence"):
            DecisionValidator(store, schema=load_decision_schema()).validate(
                {**BASE, "snapshot_id": snapshot.snapshot_id, "evidence_ids": []},
                self._context(snapshot),
            )

    def test_close_with_non_zero_weight_is_an_arithmetic_contradiction(self) -> None:
        store, builder, fact_id, _ = self._fixture()
        snapshot = builder.build(as_of=AT + dt.timedelta(minutes=1), fact_ids=(fact_id,))
        with pytest.raises(DecisionValidationError) as excinfo:
            DecisionValidator(store, schema=load_decision_schema()).validate(
                {
                    **BASE,
                    "snapshot_id": snapshot.snapshot_id,
                    "evidence_ids": [fact_id],
                    "action": "CLOSE",
                    "target_weight": Decimal("0.05"),
                },
                self._context(snapshot, current_weight=Decimal("0.05")),
            )
        assert excinfo.value.reason_code is ReasonCode.NUMERIC_DISAGREEMENT

    def test_reduce_that_does_not_reduce_is_rejected(self) -> None:
        store, builder, fact_id, _ = self._fixture()
        snapshot = builder.build(as_of=AT + dt.timedelta(minutes=1), fact_ids=(fact_id,))
        with pytest.raises(DecisionValidationError) as excinfo:
            DecisionValidator(store, schema=load_decision_schema()).validate(
                {
                    **BASE,
                    "snapshot_id": snapshot.snapshot_id,
                    "evidence_ids": [fact_id],
                    "action": "REDUCE",
                    "target_weight": Decimal("0.09"),
                },
                self._context(snapshot, current_weight=Decimal("0.05")),
            )
        assert excinfo.value.reason_code is ReasonCode.NUMERIC_DISAGREEMENT

    def test_unknown_evidence_fails_against_an_incomplete_snapshot(self) -> None:
        store = EvidenceStore(default_test_registry())
        builder = SnapshotBuilder(store, max_age_seconds=MAX_AGE)
        snapshot = builder.build(as_of=AT, fact_ids=())
        assert snapshot.completeness.value == "INCOMPLETE"
        assert not snapshot.is_usable
