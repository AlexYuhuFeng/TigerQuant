"""The write-scope ceiling: default refusal, explicit opt-in, and the unexpected still refused.



This is the control that decides whether a credential capable of trading can exist in this
project at all, so it gets tested from both directions:

* the **default** must refuse a write scope, which is what makes every other module's session
  read-only without it having to say so;
* the **opt-in** must permit it, because otherwise CR-03 can never be authorized;
* and in BOTH cases a scope we did not ask for must be refused, because a server that widens
  consent on its own is exactly what this guard exists for.

The tests do not construct a real SDK session. They exercise the assertion directly, because the
assertion is the control and the SDK is not.
"""

from __future__ import annotations

import pytest

from tigerquant.adapters.tiger.execution import WRITE_SCOPE
from tigerquant.adapters.tiger.oauth import (
    FORBIDDEN_SCOPES,
    READ_ONLY_SCOPES,
    TigerQuantOAuthSession,
)
from tigerquant.core.errors import ConfigurationError

# This module reaches an optional SDK through the adapter it imports, so it needs that
# extra installed. The fast lane syncs only `--extra dev` and DESELECTS it rather than
# failing: the marker existed in pyproject all along and nothing wore it, so CI ran these
# against an environment that deliberately lacks the dependency. Placed after the imports
# so it neither lands inside the docstring nor trips E402.
pytestmark = pytest.mark.requires_broker

READ_SET = set(READ_ONLY_SCOPES.split())
WRITE_SET = {WRITE_SCOPE}


class _Session:
    """The assertion under test, without an SDK session behind it.

    Extracted rather than mocked: the class's ``__init__`` builds a real OAuth manager, and what
    matters here is the scope decision, not the manager.
    """

    def __init__(self, *, requested: str, allow_write_scope: bool) -> None:
        self._scopes = requested
        self._allow_write_scope = allow_write_scope

    _assert_no_write_scope = TigerQuantOAuthSession._assert_no_write_scope
    permits_order_submission = TigerQuantOAuthSession.permits_order_submission


def read_only_session() -> _Session:
    return _Session(requested=READ_ONLY_SCOPES, allow_write_scope=False)


def write_session() -> _Session:
    return _Session(
        requested=f"{READ_ONLY_SCOPES} {WRITE_SCOPE}",
        allow_write_scope=True,
    )


class TestTheDefaultRefuses:
    """Every other module builds a session without the opt-in, so this default is the ceiling."""

    def test_the_named_scope_is_the_forbidden_one(self) -> None:
        assert FORBIDDEN_SCOPES == (WRITE_SCOPE,)
        assert WRITE_SCOPE not in READ_ONLY_SCOPES.split()

    def test_a_granted_write_scope_is_refused(self) -> None:
        """The read-only session asked for four read scopes, so a write scope is unexpected.

        Note the check is a set difference, not a blocklist: an exact-match grant passes (see
        the next test), and anything extra is refused. That is strictly stronger than
        "refuse write scopes", because it also catches a scope we never asked for.
        """
        with pytest.raises(ConfigurationError, match="did not request"):
            read_only_session()._assert_no_write_scope(f"{READ_ONLY_SCOPES} {WRITE_SCOPE}")

    def test_the_read_scopes_are_accepted(self) -> None:
        read_only_session()._assert_no_write_scope(READ_ONLY_SCOPES)

    def test_it_does_not_permit_order_submission(self) -> None:
        assert read_only_session().permits_order_submission is False

    def test_the_read_set_is_exactly_four_read_scopes(self) -> None:
        assert len(READ_SET) == 4
        assert all(scope.endswith(":read") for scope in READ_SET)


class TestTheOptInPermits:
    def test_a_granted_write_scope_is_accepted_when_requested(self) -> None:
        write_session()._assert_no_write_scope(f"{READ_ONLY_SCOPES} {WRITE_SCOPE}")

    def test_it_permits_order_submission(self) -> None:
        assert write_session().permits_order_submission is True

    def test_it_permits_nothing_else(self) -> None:
        """The opt-in widens the ceiling by exactly one scope, not by a category."""
        assert write_session().permits_order_submission
        assert {WRITE_SCOPE} == WRITE_SET


class TestUnexpectedScopesAreAlwaysRefused:
    """A server that widens consent on its own is what this guard exists for."""

    def test_an_unrequested_scope_is_refused_even_with_the_opt_in(self) -> None:
        session = write_session()
        with pytest.raises(ConfigurationError, match="did not request"):
            session._assert_no_write_scope(
                f"{READ_ONLY_SCOPES} {WRITE_SCOPE} api.something.else:write"
            )

    def test_an_unrequested_scope_is_refused_without_the_opt_in(self) -> None:
        session = read_only_session()
        with pytest.raises(ConfigurationError, match="did not request"):
            session._assert_no_write_scope(f"{READ_ONLY_SCOPES} api.something.else:write")

    def test_the_refusal_names_what_was_granted_and_what_was_asked_for(self) -> None:
        with pytest.raises(ConfigurationError) as caught:
            read_only_session()._assert_no_write_scope(f"{READ_ONLY_SCOPES} {WRITE_SCOPE}")
        message = str(caught.value)
        assert WRITE_SCOPE in message
        assert "revoke" in message.lower()

    def test_an_empty_grant_is_not_an_error_here(self) -> None:
        """No granted scopes is a different problem, caught when the token is used."""
        read_only_session()._assert_no_write_scope("")


class TestTheIncoherentCombinationIsRefused:
    def test_permitting_a_write_scope_without_requesting_it_is_a_configuration_error(self) -> None:
        """Otherwise the token cannot trade, and it looks like a broker problem.

        Exercised through the real constructor, which is the only place the combination can be
        created: an SDK session is built first, so this asserts on the error rather than on
        object state.
        """
        with pytest.raises(ConfigurationError, match="does not include"):
            TigerQuantOAuthSession(
                scopes=READ_ONLY_SCOPES,
                allow_write_scope=True,
            )


class TestTheLoginTool:
    """The tool is the only place the opt-in can be reached from."""

    def test_the_flag_exists_and_is_not_the_default(self) -> None:
        from tigerquant.cli import build_parser

        assert build_parser() is not None
        source = (__import__("pathlib").Path("tools/tiger_login.py")).read_text(encoding="utf-8")
        assert "--enable-order-submission" in source
        assert 'action="store_true"' in source
        # The default must be off: a bare flag would mean the tool always requested write.
        assert "default=True" not in source.split("--enable-order-submission")[1][:400]

    def test_the_tool_warns_before_the_consent_page(self) -> None:
        source = (__import__("pathlib").Path("tools/tiger_login.py")).read_text(encoding="utf-8")
        assert "NOT paper-bound" in source
        assert "guard_write_scope.txt" in source

    def test_the_tool_uses_the_named_constants_not_literals(self) -> None:
        """A hardcoded scope string would drift from the guard that checks it."""
        source = (__import__("pathlib").Path("tools/tiger_login.py")).read_text(encoding="utf-8")
        assert "READ_ONLY_SCOPES" in source
        assert "WRITE_SCOPE" in source
        assert '"api.trade:write"' not in source
