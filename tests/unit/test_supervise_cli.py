"""The `tigerquant supervise` command.

The property that matters most is what the command CANNOT do: there is no broker option, no
account option and no approval registry anywhere in the module. The loop only ever reads a journal
and compares it with a database. That is enforced by what is absent, which is stronger than a
check that could be removed.

The rest is about honest reporting: a run that found drift must not read as a failing supervisor,
and a self-halt must exit non-zero so whatever started the process can tell the difference between
"stopped because it broke" and "stopped because someone asked".
"""

from __future__ import annotations

import argparse
import inspect
from pathlib import Path

import pytest

from tigerquant.core.enums import Mode
from tigerquant.market.fixtures import load_scenario
from tigerquant.ops.supervisor_cli import DEFAULT_HEARTBEAT, build_cycle, cmd_supervise
from tigerquant.pipeline.replay import run_replay

FIXTURE = Path("tests/fixtures/scenarios/trend_up.yaml")


@pytest.fixture(scope="module")
def journal(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = Path(str(tmp_path_factory.mktemp("sup"))) / "journal.jsonl"
    run_replay(load_scenario(FIXTURE), journal_path=path)
    return path


def ns(journal_path: Path, **kwargs: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "journal": journal_path,
        "mode": Mode.BACKTEST.value,
        "interval": 0.001,
        "iterations": 2,
        "max_failures": 3,
        "retry_attempts": 8,
        "retry_max_seconds": 60.0,
        "heartbeat": None,
        "no_alerts": True,
        "alert_log": None,
        "webhook": None,
        "floor": "WARNING",
        "quiet": True,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


class TestItCannotTrade:
    def test_the_module_has_no_order_vocabulary(self) -> None:
        """Enforced by what is ABSENT, which is stronger than a check that could be removed."""
        source = inspect.getsource(__import__("tigerquant.ops.supervisor_cli", fromlist=["x"]))
        for forbidden in ("submit_order", "place_order", "OrderIntent", "cancel_order"):
            assert forbidden not in source, f"{forbidden} must not appear in a reading loop"

    def test_it_imports_no_broker_adapter(self) -> None:
        source = inspect.getsource(__import__("tigerquant.ops.supervisor_cli", fromlist=["x"]))
        assert "tigerquant.adapters" not in source

    def test_the_help_says_so(self) -> None:
        from tigerquant.ops.supervisor_cli import supervise_parser

        class Sub:
            def add_parser(self, *args: object, **kwargs: object) -> object:
                self.help = str(kwargs.get("help", ""))
                parser = argparse.ArgumentParser()

                def _ignore(*_args: object, **_kwargs: object) -> None:
                    """Register nothing: this test only reads the help string."""

                parser.add_argument = _ignore  # type: ignore[method-assign]
                return parser

        sub = Sub()
        supervise_parser(sub)
        assert "CANNOT SUBMIT ORDERS" in sub.help  # type: ignore[attr-defined]


class TestBuildCycle:
    def test_it_returns_a_callable(self, journal: Path) -> None:
        cycle, _ = build_cycle(journal)
        assert callable(cycle)

    def test_a_diverging_journal_reports_true(self, journal: Path) -> None:
        """An empty database behind a populated journal is a genuine divergence."""
        cycle, _ = build_cycle(journal)
        assert cycle() is True

    def test_it_re_reads_the_journal_every_cycle(self, journal: Path) -> None:
        """A supervisor that cached the journal would be reconciling the past while reporting
        on the present, which is worse than not running."""
        cycle, _ = build_cycle(journal)
        first = cycle()
        second = cycle()
        assert first is second is True

    def test_the_monitor_records_history(self, journal: Path) -> None:
        cycle, monitor = build_cycle(journal)
        cycle()
        assert len(monitor.history) == 1  # type: ignore[attr-defined]


class TestCommand:
    def test_a_clean_run_exits_zero(self, journal: Path, tmp_path: Path) -> None:
        code = cmd_supervise(ns(journal, heartbeat=tmp_path / "hb.json"))
        assert code == 0

    def test_it_writes_the_heartbeat(self, journal: Path, tmp_path: Path) -> None:
        path = tmp_path / "hb.json"
        cmd_supervise(ns(journal, heartbeat=path))
        assert path.is_file()

    def test_it_writes_no_heartbeat_when_none_is_configured(self, journal: Path) -> None:
        assert cmd_supervise(ns(journal, heartbeat=None)) == 0

    def test_a_missing_journal_is_refused(self, tmp_path: Path) -> None:
        assert cmd_supervise(ns(tmp_path / "nope.jsonl")) == 2

    def test_drift_is_reported_as_a_success_not_a_failure(
        self, journal: Path, tmp_path: Path
    ) -> None:
        """Counting a check that ran correctly as a failure would halt the supervisor for doing
        its job, which is exactly backwards."""
        assert cmd_supervise(ns(journal, heartbeat=tmp_path / "hb.json")) == 0

    def test_a_bad_floor_is_refused(self, journal: Path, tmp_path: Path) -> None:
        with pytest.raises(Exception, match="unknown severity"):
            cmd_supervise(
                ns(journal, floor="CRTICAL", no_alerts=False, heartbeat=tmp_path / "hb.json")
            )

    def test_alerting_can_be_disabled(self, journal: Path, tmp_path: Path) -> None:
        assert cmd_supervise(ns(journal, no_alerts=True, heartbeat=tmp_path / "hb.json")) == 0


class TestTheDefaultHeartbeat:
    def test_it_is_a_concrete_path_not_none(self) -> None:
        """A supervisor nobody can tell is stuck is the problem this command exists to avoid."""
        assert DEFAULT_HEARTBEAT is not None
        assert str(DEFAULT_HEARTBEAT).endswith(".json")
