"""Database identity: catching a connection to the WRONG database.

THE DEFECT THIS CLOSES IS A FALSE NEGATIVE, which is why it outranks everything else that was
left in CR-09. Every mode has its own database (ADR-0013 §2) and a DSN is just a string. Passing
the PAPER DSN while running `--mode BACKTEST` connected to the paper database, compared an empty
paper database against a backtest journal, found the expected divergence, and reported a CLEAN RUN
against the wrong database.

Nothing noticed, because a database with no rows looks exactly like a database with no rows.

WHY THIS DOES NOT CONTRADICT ADR-0013. The mode is still not on any row and no query filters by it;
the database boundary remains the isolation mechanism. This is a single row describing the DATABASE
ITSELF -- a guard against misconfiguration, not a substitute for the boundary.

DENY BY DEFAULT, like everything else here: undeclared is REFUSED, not assumed to match, because
"I cannot tell what this is" and "this is what I expected" are different answers.
"""

from __future__ import annotations

import pytest

from tigerquant.core.enums import Mode
from tigerquant.core.errors import ConfigurationError
from tigerquant.db.identity import (
    DatabaseIdentity,
    IdentityVerdict,
    declare_mode,
    read_identity,
    verify_identity,
)


class FakeCursor:
    def __init__(self, row: object) -> None:
        self._row = row

    def fetchone(self) -> object:
        return self._row


class FakeConnection:
    """A connection whose identity table behaves, or does not exist at all."""

    def __init__(self, *, mode: str | None = "BACKTEST", table: bool = True) -> None:
        self.mode = mode
        self.table = table
        self.declared_by = "test"
        self.statements: list[str] = []

    def execute(self, sql: str, params: object = None) -> FakeCursor:
        self.statements.append(sql)
        if not self.table and "database_identity" in sql:
            msg = 'relation "database_identity" does not exist'
            error = RuntimeError(msg)
            error.sqlstate = "42P01"  # type: ignore[attr-defined]
            raise error
        if sql.strip().upper().startswith("SELECT"):
            if self.mode is None:
                return FakeCursor(None)
            return FakeCursor((self.mode, "2026-10-12 00:00:00+00:00", self.declared_by))
        if sql.strip().upper().startswith("INSERT"):
            if self.mode is not None:
                return FakeCursor(None)  # ON CONFLICT DO NOTHING -> no row
            self.mode = str(params[0])  # type: ignore[index]
            return FakeCursor((self.mode, "2026-10-12 00:00:00+00:00", self.declared_by))
        return FakeCursor(None)

    def commit(self) -> None:
        self.statements.append("COMMIT")


class TestReadingTheIdentity:
    def test_a_declared_database_reports_its_mode(self) -> None:
        identity = read_identity(FakeConnection(mode="PAPER"))
        assert identity is not None
        assert identity.mode == "PAPER"
        assert identity.as_mode is Mode.PAPER

    def test_an_empty_table_reads_as_none(self) -> None:
        assert read_identity(FakeConnection(mode=None)) is None

    def test_a_missing_table_reads_as_none(self) -> None:
        """A missing TABLE and an empty TABLE mean the same thing to a caller: this database has
        not said what it is."""
        assert read_identity(FakeConnection(table=False)) is None

    def test_an_unrecognised_mode_has_no_mode(self) -> None:
        identity = DatabaseIdentity(mode="SOMETHING_ELSE")
        assert identity.as_mode is None


class TestAMismatchIsAlwaysRefused:
    def test_the_wrong_mode_is_refused(self) -> None:
        """The defect. A database declaring PAPER must not be used for a BACKTEST run."""
        verdict = verify_identity(FakeConnection(mode="PAPER"), Mode.BACKTEST)
        assert verdict.refused
        assert verdict.status == "mismatch"

    def test_the_message_names_both_modes(self) -> None:
        verdict = verify_identity(FakeConnection(mode="PAPER"), Mode.BACKTEST)
        assert "PAPER" in verdict.detail
        assert "BACKTEST" in verdict.detail

    def test_allow_undeclared_does_not_permit_a_mismatch(self) -> None:
        """The escape hatch is for an unverifiable database, not for a demonstrably wrong one.
        A mismatch is the case where rows get written under the wrong assumption."""
        verdict = verify_identity(
            FakeConnection(mode="PAPER"), Mode.BACKTEST, allow_undeclared=True
        )
        assert verdict.refused

    def test_raise_if_refused_raises(self) -> None:
        verdict = verify_identity(FakeConnection(mode="PAPER"), Mode.BACKTEST)
        with pytest.raises(ConfigurationError, match="wrong one"):
            verdict.raise_if_refused()

    def test_the_matching_mode_passes(self) -> None:
        verdict = verify_identity(FakeConnection(mode="BACKTEST"), Mode.BACKTEST)
        assert verdict.ok
        assert verdict.status == "matches"

    def test_every_mode_pair_is_checked(self) -> None:
        """No pair is special-cased. Every mode has its own database, so every mismatch matters."""
        for declared in Mode:
            for expected in Mode:
                verdict = verify_identity(FakeConnection(mode=declared.value), expected)
                assert verdict.ok is (declared is expected), f"{declared} vs {expected}"


class TestUndeclaredIsRefusedByDefault:
    def test_an_empty_identity_table_is_refused(self) -> None:
        """Deny by default: 'I cannot tell what this is' is not 'this is what I expected'."""
        verdict = verify_identity(FakeConnection(mode=None), Mode.BACKTEST)
        assert verdict.refused
        assert verdict.status == "undeclared"

    def test_the_refusal_says_how_to_proceed(self) -> None:
        verdict = verify_identity(FakeConnection(mode=None), Mode.BACKTEST)
        assert "--allow-undeclared-database" in verdict.detail

    def test_the_escape_hatch_permits_it_explicitly(self) -> None:
        verdict = verify_identity(FakeConnection(mode=None), Mode.BACKTEST, allow_undeclared=True)
        assert verdict.ok
        assert verdict.status == "undeclared"

    def test_the_escape_hatch_says_it_cannot_be_checked(self) -> None:
        """An operator using it must not come away thinking the database was verified."""
        verdict = verify_identity(FakeConnection(mode=None), Mode.BACKTEST, allow_undeclared=True)
        assert "cannot be checked" in verdict.detail


class TestUnreadableIsRefused:
    def test_a_failing_read_is_refused_not_swallowed(self) -> None:
        class Exploding(FakeConnection):
            def execute(self, sql: str, params: object = None) -> FakeCursor:
                msg = "connection reset"
                raise RuntimeError(msg)  # no SQLSTATE: a real failure, not a missing table

        verdict = verify_identity(Exploding(), Mode.BACKTEST)
        assert verdict.refused
        assert verdict.status == "unreadable"
        assert "connection reset" in verdict.detail


class TestDeclaring:
    def test_it_records_the_mode(self) -> None:
        connection = FakeConnection(mode=None)
        declared = declare_mode(connection, Mode.PAPER, declared_by="operator")
        assert declared.mode == "PAPER"

    def test_it_is_idempotent_for_the_same_mode(self) -> None:
        connection = FakeConnection(mode="PAPER")
        assert declare_mode(connection, Mode.PAPER).mode == "PAPER"

    def test_it_refuses_to_change_an_existing_declaration(self) -> None:
        """A database whose mode silently changed is worse than one that refused to start: every
        row in it was written under the old assumption and becomes unattributable."""
        with pytest.raises(ConfigurationError, match="cannot be re-declared"):
            declare_mode(FakeConnection(mode="PAPER"), Mode.BACKTEST)

    def test_the_refusal_says_who_declared_it_and_when(self) -> None:
        with pytest.raises(ConfigurationError) as caught:
            declare_mode(FakeConnection(mode="PAPER"), Mode.BACKTEST)
        assert "test" in str(caught.value)

    def test_a_race_is_resolved_by_re_reading(self) -> None:
        """Another process may declare between the read and the insert. Re-read rather than
        guess, and refuse if it disagrees."""
        connection = FakeConnection(mode=None)
        # Simulate the race: the INSERT conflicts, and the re-read finds a DIFFERENT mode.
        original = connection.execute

        def racing(sql: str, params: object = None) -> FakeCursor:
            if sql.strip().upper().startswith("INSERT"):
                connection.mode = "SHADOW"
                return FakeCursor(None)
            return original(sql, params)

        connection.execute = racing  # type: ignore[method-assign]
        with pytest.raises(ConfigurationError, match="another process"):
            declare_mode(connection, Mode.BACKTEST)


class TestTheVerdictShape:
    def test_a_refusal_is_never_ok(self) -> None:
        for status_source in (FakeConnection(mode="PAPER"), FakeConnection(mode=None)):
            assert verify_identity(status_source, Mode.BACKTEST).refused

    def test_the_verdict_carries_what_was_expected_and_found(self) -> None:
        verdict = verify_identity(FakeConnection(mode="PAPER"), Mode.BACKTEST)
        assert verdict.expected == "BACKTEST"
        assert verdict.found is not None
        assert verdict.found.mode == "PAPER"

    def test_an_ok_verdict_does_not_raise(self) -> None:
        IdentityVerdict(ok=True, status="matches", detail="").raise_if_refused()
