"""The identity guard covers EVERY database command, not just the convenient one.

The guard went into `supervise` first, which left the same defect reachable through `drift` and
`project`. **A guard that covers one of three entry points is not a guard; it is a guard plus two
ways around it.**

These tests are STRUCTURAL as well as behavioural, because the failure mode was structural: an
insertion script that replaced the first occurrence of an anchor put the guard into `drift` TWICE
and `project` not at all. Behavioural tests alone would have caught the missing one; a structural
count catches the double one too, which is silent.
"""

from __future__ import annotations

import argparse
import inspect

import pytest

from tigerquant.cli import _add_identity_arguments, _assert_database_identity
from tigerquant.core.enums import Mode

MESSAGES = "the guard must be wired into every command that opens a database"


class FakeConnection:
    def __init__(self, *, mode: str | None = "BACKTEST", table: bool = True) -> None:
        self.mode = mode
        self.table = table

    def execute(self, sql: str, params: object = None) -> object:
        if not self.table and "database_identity" in sql:
            msg = 'relation "database_identity" does not exist'
            error = RuntimeError(msg)
            error.sqlstate = "42P01"  # type: ignore[attr-defined]
            raise error

        class Cursor:
            def __init__(self, row: object) -> None:
                self._row = row

            def fetchone(self) -> object:
                return self._row

        upper = sql.strip().upper()
        if upper.startswith("SELECT"):
            if self.mode is None:
                return Cursor(None)
            return Cursor((self.mode, "2026-10-12 00:00:00+00:00", "test"))
        if upper.startswith("INSERT"):
            # ON CONFLICT DO NOTHING: a row comes back only when the insert actually happened.
            if self.mode is not None:
                return Cursor(None)
            self.mode = str(params[0])  # type: ignore[index]
            return Cursor((self.mode, "2026-10-12 00:00:00+00:00", "test"))
        return Cursor(None)

    def commit(self) -> None:
        pass


def ns(**kwargs: object) -> argparse.Namespace:
    base: dict[str, object] = {"declare_mode": False, "allow_undeclared_database": False}
    base.update(kwargs)
    return argparse.Namespace(**base)


class TestTheGuardIsWiredEverywhere:
    """Structural, because the defect was structural."""

    def test_every_database_handler_calls_it_exactly_once(self) -> None:
        source = inspect.getsource(__import__("tigerquant.cli", fromlist=["x"]))
        for handler in ("_cmd_drift", "_cmd_project"):
            start = source.index(f"def {handler}(")
            rest = source[start + 1 :]
            next_def = rest.find("\ndef ")
            body = rest[:next_def] if next_def > 0 else rest
            assert body.count("_assert_database_identity") == 1, (
                f"{handler} must call the guard exactly once, {MESSAGES}"
            )

    def test_the_double_insertion_bug_cannot_recur_silently(self) -> None:
        """The insertion script replaced the FIRST occurrence of its anchor; because the guarded
        text CONTAINS the anchor, the second pass nested a second guard into _cmd_drift and left
        _cmd_project unguarded. Counting catches the silent half."""
        source = inspect.getsource(__import__("tigerquant.cli", fromlist=["x"]))
        assert source.count("refused = _assert_database_identity") == 2

    def test_supervise_is_guarded_too(self) -> None:
        from tigerquant.ops import supervisor_cli

        assert "verify_identity" in inspect.getsource(supervisor_cli)

    def test_both_database_commands_accept_the_flags(self) -> None:
        """A guard with no way to declare is a guard that can only refuse."""
        source = inspect.getsource(__import__("tigerquant.cli", fromlist=["x"]))
        assert source.count("_add_identity_arguments(") >= 3


class TestTheGuardBehaviour:
    def test_a_matching_database_is_accepted(self) -> None:
        assert _assert_database_identity(ns(), FakeConnection(mode="BACKTEST"), Mode.BACKTEST) == ""

    def test_a_mismatch_is_refused(self) -> None:
        message = _assert_database_identity(ns(), FakeConnection(mode="PAPER"), Mode.BACKTEST)
        assert message, MESSAGES
        assert "refusing to use this database" in message
        assert "PAPER" in message

    def test_an_undeclared_database_is_refused_by_default(self) -> None:
        assert _assert_database_identity(ns(), FakeConnection(mode=None), Mode.BACKTEST) != ""

    def test_the_escape_hatch_permits_undeclared(self) -> None:
        message = _assert_database_identity(
            ns(allow_undeclared_database=True), FakeConnection(mode=None), Mode.BACKTEST
        )
        assert message == ""

    def test_the_escape_hatch_does_not_permit_a_mismatch(self) -> None:
        message = _assert_database_identity(
            ns(allow_undeclared_database=True), FakeConnection(mode="PAPER"), Mode.BACKTEST
        )
        assert message != "", "a demonstrably wrong database is refused either way"

    def test_declare_mode_stamps_an_undeclared_database(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        connection = FakeConnection(mode=None)
        message = _assert_database_identity(ns(declare_mode=True), connection, Mode.BACKTEST)
        assert message == ""
        assert connection.mode == "BACKTEST"
        assert "declared this database" in capsys.readouterr().err

    def test_declare_mode_does_not_override_a_mismatch(self) -> None:
        """Declaring must not be a way to make the guard agree with a mistake."""
        message = _assert_database_identity(
            ns(declare_mode=True), FakeConnection(mode="PAPER"), Mode.BACKTEST
        )
        assert message != ""

    def test_every_mode_pair_is_checked(self) -> None:
        for declared in Mode:
            for expected in Mode:
                message = _assert_database_identity(
                    ns(), FakeConnection(mode=declared.value), expected
                )
                assert (message == "") is (declared is expected), f"{declared} vs {expected}"


class TestTheSharedFlags:
    def test_both_flags_are_registered(self) -> None:
        parser = argparse.ArgumentParser()
        _add_identity_arguments(parser)
        parsed = parser.parse_args(["--declare-mode", "--allow-undeclared-database"])
        assert parsed.declare_mode is True
        assert parsed.allow_undeclared_database is True

    def test_they_default_to_off(self) -> None:
        """Deny by default: neither flag is on unless the operator says so."""
        parser = argparse.ArgumentParser()
        _add_identity_arguments(parser)
        parsed = parser.parse_args([])
        assert parsed.declare_mode is False
        assert parsed.allow_undeclared_database is False
