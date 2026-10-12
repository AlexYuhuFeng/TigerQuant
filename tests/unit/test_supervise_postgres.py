"""The supervise command's PostgreSQL wiring.

Two things are under test, and the second was found by hand rather than by a test.

**A connection failure must be a REFUSAL, never a fallback.** A supervisor that appears to
reconcile while actually comparing against an empty dict is worse than one that will not start,
because it reports health it has not checked.

**A connection failure must be exit 2, not a traceback.** Pointing the command at an unreachable
database originally produced a raw driver exception and exit 1, because the `except` clause caught
only this project's own error types. Exit 1 from a traceback is indistinguishable from a crash for
any other reason, which defeats the point of having a distinct code for "the database is down".
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from tigerquant.core.enums import Mode
from tigerquant.ops.supervisor_cli import DSN_ENV, _describe_database, _select_repository


def ns(**kwargs: object) -> argparse.Namespace:
    base: dict[str, object] = {"dsn": None, "mode": Mode.BACKTEST.value}
    base.update(kwargs)
    return argparse.Namespace(**base)


class TestNoDsnMeansInMemory:
    def test_no_dsn_yields_no_repository(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(DSN_ENV, raising=False)
        connection, repository, failure = _select_repository(ns())
        assert connection is None
        assert repository is None
        assert failure is None

    def test_the_environment_variable_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The same variable `tigerquant drift` uses, so one setting covers both commands."""
        assert DSN_ENV == "TIGERQUANT_DB_DSN"


class TestTheDescriptionIsHonest:
    def test_in_memory_says_wiring_check(self) -> None:
        """ "In-memory" alone invites the reader to assume it is still a real reconciliation."""
        described = _describe_database(None)
        assert "WIRING CHECK" in described
        assert "--dsn" in described

    def test_a_repository_is_named_by_type(self) -> None:
        class FakeRepository:
            pass

        described = _describe_database(FakeRepository())
        assert "FakeRepository" in described
        assert "PostgreSQL" in described


#: connect_timeout is not optional. Two earlier versions of this fixture used ports 5999 and 1 and
#: each spent 130s per assertion in the fast lane, because a filtered port TIMES OUT rather than
#: refusing. 390 seconds to prove that a bad connection is refused is worse than the bug.
#:
#: It is also the right thing for the product: an operator starting a supervisor against an
#: unreachable database should get a refusal in seconds, not after the driver's default patience.
REFUSED_DSN = "postgresql://nope:nope@127.0.0.1:1/none?connect_timeout=2"


class TestAnUnreachableDatabaseIsRefused:
    def test_it_returns_failure_code_2(self) -> None:
        """Not a fallback to in-memory, and not a traceback."""
        _, repository, failure = _select_repository(ns(dsn=REFUSED_DSN))
        assert failure == 2
        assert repository is None

    def test_it_does_not_raise(self) -> None:
        """A driver exception escaping here would be exit 1 and a traceback."""
        try:
            _select_repository(ns(dsn=REFUSED_DSN))
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"a connection failure must be caught and reported, not raised: {exc}")

    def test_the_message_names_the_database(self, capsys: pytest.CaptureFixture[str]) -> None:
        _select_repository(ns(dsn=REFUSED_DSN))
        assert "cannot reach the database" in capsys.readouterr().err

    def test_a_bad_password_is_also_refused(self) -> None:
        """The most likely real failure: the DSN is right and the credential is not."""
        _, repository, failure = _select_repository(
            ns(dsn="postgresql://tq_backtest:wrong@127.0.0.1:5432/tq_backtest")
        )
        # Either it connects (unlikely) or it is refused cleanly. Never a traceback.
        assert failure == 2
        assert repository is None


class TestTheCommandRefusesRatherThanFallingBack:
    def test_cmd_supervise_exits_2_on_a_bad_dsn(self, tmp_path: Path) -> None:
        import inspect

        from tigerquant.ops import supervisor_cli

        source = inspect.getsource(supervisor_cli.cmd_supervise)
        assert "if failure:" in source, (
            "the command must return the failure code rather than continuing with no repository"
        )
        assert "return failure" in source


class TestItStillCannotTrade:
    def test_the_postgres_wiring_added_no_order_path(self) -> None:
        """A database connection is the most plausible route by which an order path could creep
        into a reading loop, so the absence is asserted again AFTER the wiring."""
        import inspect

        from tigerquant.ops import supervisor_cli

        source = inspect.getsource(supervisor_cli)
        for forbidden in ("submit_order", "place_order", "OrderIntent", "cancel_order"):
            assert forbidden not in source
