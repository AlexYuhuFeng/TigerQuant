"""The hang verdict is REPORTED on restart, not merely computed.

`detect_hang` is only worth having if something calls it. **Dead detection is worse than none**: it
looks like the failure is covered. This project has found that shape repeatedly, so the wiring is
tested rather than assumed.

The window matters. The heartbeat is read BEFORE the first write overwrites it, and that moment is the
only chance to learn how the previous run ended -- restarting a supervisor that HUNG is otherwise
indistinguishable from restarting one that stopped cleanly, and the two call for different responses.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from tigerquant.ops.supervisor_cli import _report_previous_run


def write_heartbeat(path: Path, *, phase: str, age: float, cycle: int = 41) -> None:
    stamp = dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=age)
    path.write_text(
        json.dumps({"phase": phase, "cycle": cycle, "written_at": stamp.isoformat()}),
        encoding="utf-8",
    )


@pytest.fixture
def heartbeat_path(tmp_path: Path) -> Path:
    return tmp_path / "supervisor.json"


class TestTheReporterSaysWhatHappened:
    def test_a_hung_previous_run_is_reported(
        self, heartbeat_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """THE reason this exists."""
        write_heartbeat(heartbeat_path, phase="running", age=1200)
        _report_previous_run(heartbeat_path, hang_after=900.0)
        printed = capsys.readouterr().out
        assert "HUNG" in printed
        assert "ATTENTION" in printed

    def test_it_names_the_cycle_and_how_long(
        self, heartbeat_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bare HUNG does not tell a responder whether to restart or to look at what it waited on."""
        write_heartbeat(heartbeat_path, phase="running", age=1200, cycle=41)
        _report_previous_run(heartbeat_path, hang_after=900.0)
        printed = capsys.readouterr().out
        assert "41" in printed
        assert "1200" in printed

    def test_it_warns_that_the_cause_may_persist(
        self, heartbeat_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Restarting into the same broken dependency is the mistake this line prevents."""
        write_heartbeat(heartbeat_path, phase="running", age=1200)
        _report_previous_run(heartbeat_path, hang_after=900.0)
        assert "never finished" in capsys.readouterr().out

    def test_a_clean_stop_is_reported_ok(
        self, heartbeat_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write_heartbeat(heartbeat_path, phase="idle", age=5)
        _report_previous_run(heartbeat_path, hang_after=900.0)
        printed = capsys.readouterr().out
        assert "IDLE" in printed
        assert "ATTENTION" not in printed

    def test_a_long_backoff_is_reported_ok(
        self, heartbeat_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The case a naive detector gets wrong, asserted through the REPORTER as well as the
        verdict, because the wiring is what an operator actually sees."""
        write_heartbeat(heartbeat_path, phase="backoff", age=9999)
        _report_previous_run(heartbeat_path, hang_after=900.0)
        printed = capsys.readouterr().out
        assert "BACKOFF" in printed
        assert "ATTENTION" not in printed


class TestItStaysQuietWhenItHasNothingToSay:
    def test_an_absent_heartbeat_prints_nothing(
        self, heartbeat_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A first run has nothing to report, and a line printed every time is a line ignored."""
        _report_previous_run(heartbeat_path, hang_after=900.0)
        assert capsys.readouterr().out == ""

    def test_a_corrupt_heartbeat_does_not_crash(
        self, heartbeat_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A supervisor that refuses to start because its OWN status file is malformed has turned a
        diagnostic into an outage."""
        heartbeat_path.write_text("{not json", encoding="utf-8")
        _report_previous_run(heartbeat_path, hang_after=900.0)
        assert "previous run" not in capsys.readouterr().out


class TestTheFlagIsWired:
    def _args(self, *extra: str) -> object:
        """Parse `supervise` through the REAL CLI.

        `supervise_parser(sub)` registers onto a subparsers object rather than building a parser, so
        calling it with no argument raises TypeError -- which the first version of this test did. And
        `supervise` requires a journal positional, so `parse_args(["supervise"])` alone exits. Going
        through `build_parser` checks the flag where an operator would find it.
        """
        from tigerquant.cli import build_parser

        return build_parser().parse_args(["supervise", "out/journal.jsonl", *extra])

    def test_supervise_accepts_hang_after(self) -> None:
        """The reporter is called with `args.hang_after`, so the flag must exist or every restart
        would fail on an AttributeError -- which no unit test of the reporter would catch."""
        args = self._args()
        assert hasattr(args, "hang_after"), "the --hang-after flag is missing"
        assert args.hang_after > 0

    def test_the_default_is_generous_enough_for_a_real_cycle(self) -> None:
        """A default that fires on healthy work trains operators to ignore the line."""
        assert self._args().hang_after >= 300

    def test_the_flag_is_overridable(self) -> None:
        """A bound nobody can change is a bound that is wrong for somebody."""
        assert self._args("--hang-after", "60").hang_after == 60
