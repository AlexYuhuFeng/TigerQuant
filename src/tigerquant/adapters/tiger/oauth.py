"""Tiger OpenAPI OAuth 2.0 session with a read-only scope ceiling.

Why this module exists
----------------------
Tiger's OAuth 2.0 authorization server advertises five scopes::

    api.basic:read  api.quote:read  api.account:read  api.trade:read  api.trade:write

`OAuth2SessionManager._scopes()` requests **every** scope the server advertises, so using
the SDK session directly would ask the owner to consent to ``api.trade:write`` and would
hand this process a credential that can place orders. That directly contradicts
AGENTS.md invariants 1 and 3 and ADR-0009 §3.

:class:`TigerQuantOAuthSession` overrides the scope set to the four read scopes. The
result is a control that is stronger than a code review: **a token that was never granted
``api.trade:write`` cannot place an order, whatever any module in this repository does
with it.** It is the credential-level counterpart of the single-write-path rule in
ADR-0007.

The override targets a private method, so it is guarded by
``tests/unit/test_tiger_oauth.py::test_scope_override_still_wins``, which fails loudly if a
future SDK release changes how scopes are chosen.

What is still true
------------------
The authorization server is a single production issuer for both paper and live Tiger
identities, and an explicit `client_id` is not required because the SDK performs dynamic
client registration (RFC 7591). Which *accounts* the resulting token can reach is decided
by Tiger at consent time, so the scope ceiling is a necessary control but not a sufficient
one: :mod:`tigerquant.adapters.tiger.guard` additionally requires the broker-reported
account to be a verified PAPER account before anything is read or written.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tigerquant.core.errors import ConfigurationError, IntegrationUnavailable
from tigerquant.core.redaction import REDACTED

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "FORBIDDEN_SCOPES",
    "READ_ONLY_SCOPES",
    "WRITE_SCOPE",
    "TigerEnvironment",
    "TigerQuantOAuthSession",
    "build_oauth2_config",
    "default_token_home",
    "load_oauth2_types",
    "load_session_manager_type",
]

#: The only scopes this project ever requests: everything needed to read, nothing to write.
READ_ONLY_SCOPES: str = "api.basic:read api.account:read api.quote:read api.trade:read"

#: The scope that permits placing an order. Requested only with an explicit operator flag.
WRITE_SCOPE: str = "api.trade:write"

#: Scopes that are never requested unless order submission has been deliberately enabled.
FORBIDDEN_SCOPES: tuple[str, ...] = (WRITE_SCOPE,)

#: Dynamic-registration surface name. Each surface registers its own client so it appears
#: separately on the owner's authorization page and can be revoked on its own.
SURFACE = "tigerquant"
CLIENT_NAME = "TigerQuant"


class TigerEnvironment(StrEnum):
    """Which Tiger **gateway** business calls are routed to.

    This is *not* a paper/live switch, and the distinction matters. Verified on 2026-10-10:

    * A real Tiger **paper account** is an ordinary account on the ordinary production
      gateway. Its account list is returned by ``get_managed_accounts()`` alongside any
      other accounts the holder has.
    * ``openapi-sandbox.tigerfintech.com`` is a *separate developer sandbox* with its own
      credentials. It is **not** where a paper account lives.
    * The OAuth issuer is the production authorization server in both cases; there is no
      sandbox issuer.

    So the gateway does not decide paper versus live — the account does. The control that
    keeps this project paper-only is
    :mod:`tigerquant.adapters.tiger.guard`, never the host name.
    """

    #: The normal gateway. Correct for a real Tiger paper account, and therefore the default.
    GATEWAY = "GATEWAY"
    #: The developer sandbox host. Only usable with sandbox-issued credentials.
    SANDBOX = "SANDBOX"


def default_token_home() -> Path:
    """Where OAuth tokens are stored.

    Defaults to ``<workspace>/.secrets/tiger``, which is git-ignored, rather than the SDK's
    ``~/.tiger/openapi``. Two reasons: the DSH workspace sandbox may deny writes outside
    the workspace, and keeping the token inside a path that is explicitly ignored makes an
    accidental `git add -A` harmless. Override with ``TIGERQUANT_TIGER_HOME``.

    Tokens are never printed, logged or copied into the audit journal.
    """
    override = os.environ.get("TIGERQUANT_TIGER_HOME")
    if override:
        return Path(override)
    return Path.cwd() / ".secrets" / "tiger"


def load_session_manager_type() -> Any:
    """Import ``OAuth2SessionManager``.

    Deliberately narrower than :func:`load_oauth2_types`: the authorization flow needs only
    ``tigeropen.auth.oauth2``, which has no third-party dependencies. That keeps the login
    step runnable even when the rest of the SDK's dependency tree is not installed, so
    authorization is never blocked by an unrelated import.
    """
    try:
        from tigeropen.auth.oauth2 import OAuth2SessionManager
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise IntegrationUnavailable("Tiger OAuth2", "broker") from exc
    return OAuth2SessionManager


def load_oauth2_types() -> tuple[Any, Any]:
    """Import the SDK's config and authentication types.

    These pull in ``tigeropen.tiger_open_config``, which needs the SDK's full dependency
    set. Used only when a business client is actually being built.

    Raises
    ------
    IntegrationUnavailable
        When the ``broker`` extra is not installed. Missing functionality is never hidden
        behind a stub (AGENTS.md invariant 11).
    """
    try:
        from tigeropen.auth.oauth2.authentication import OAuth2Authentication
        from tigeropen.tiger_open_config import TigerOpenClientConfig
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise IntegrationUnavailable("Tiger trade client", "broker") from exc
    return OAuth2Authentication, TigerOpenClientConfig


def _normalise_scopes(raw: str) -> str:
    return " ".join(sorted(set(raw.split())))


class TigerQuantOAuthSession:
    """An ``OAuth2SessionManager`` restricted to read-only scopes.

    Composition rather than inheritance: the SDK type is created lazily so that importing
    this module never requires the optional ``broker`` extra, and the scope decision is
    made in one visible place instead of being spread across overrides.
    """

    __slots__ = ("_allow_write_scope", "_client_name", "_home", "_manager", "_scopes")

    def __init__(
        self,
        *,
        home: Path | None = None,
        client_id: str | None = None,
        issuer: str | None = None,
        scopes: str = READ_ONLY_SCOPES,
        client_name: str = CLIENT_NAME,
        authorize_timeout_seconds: int = 900,
        allow_write_scope: bool = False,
    ) -> None:
        manager_type = load_session_manager_type()
        self._scopes = _normalise_scopes(scopes)
        #: Whether this session was deliberately built to hold api.trade:write.
        #:
        #: Default False, and the default is the control: every other module in the project
        #: constructs a session without this argument, so a write scope in a granted token is
        #: a refusal rather than a surprise. Only the login tool, with an explicit operator
        #: flag, sets it.
        self._allow_write_scope = allow_write_scope
        if allow_write_scope:
            # Refuse the incoherent combination rather than discovering it at the first order:
            # a session that permits a write scope but never ASKS for one would produce a token
            # that cannot trade, which looks like a broker problem instead of a configuration
            # mistake.
            missing = [scope for scope in FORBIDDEN_SCOPES if scope not in self._scopes.split()]
            if missing:
                msg = (
                    f"allow_write_scope=True but the requested scope set does not include "
                    f"{missing}. Ask for it explicitly or leave the flag off."
                )
                raise ConfigurationError(msg)
        self._client_name = client_name
        self._home = home or default_token_home()
        self._home.mkdir(parents=True, exist_ok=True)
        self._manager = manager_type(
            issuer=issuer,
            client_id=client_id,
            home=str(self._home),
            surface=SURFACE,
            client_name=client_name,
            authorize_timeout_seconds=authorize_timeout_seconds,
        )
        self._pin_scopes()

    # -- scope control -----------------------------------------------------------

    def _pin_scopes(self) -> None:
        """Force the session to request exactly our read-only scope set.

        The SDK resolves scopes in ``_scopes()``, which consults ``_resolved_scopes`` first
        and otherwise requests *all* advertised scopes. Setting the cache is what keeps the
        owner's consent screen honest, and the assertion below is the guard that fails
        loudly if the SDK stops honouring it.
        """
        manager = self._manager
        if not hasattr(manager, "_resolved_scopes"):
            msg = (
                "the installed tigeropen no longer exposes the scope cache this adapter "
                "relies on; refusing to continue because it would request trade-write scope"
            )
            raise ConfigurationError(msg)
        manager._resolved_scopes = self._scopes
        resolved = manager.scopes  # a property in tigeropen 3.8.0, not a method
        if resolved != self._scopes:
            msg = (
                f"failed to restrict OAuth scopes: SDK resolved {resolved!r}, expected "
                f"{self._scopes!r}"
            )
            raise ConfigurationError(msg)

    @property
    def scopes(self) -> str:
        return self._scopes

    @property
    def issuer(self) -> str:
        return str(self._manager.issuer)

    @property
    def client_id(self) -> str | None:
        """The dynamically registered client id, if one exists. Not a secret."""
        value = self._manager.client_id
        return None if value is None else str(value)

    @property
    def home(self) -> Path:
        return self._home

    @property
    def manager(self) -> Any:
        """The underlying session manager. Used only by the adapter itself."""
        return self._manager

    # -- authorization -----------------------------------------------------------

    def is_authorized(self) -> bool:
        return bool(self._manager.is_authorized())

    def login_if_needed(self, on_authorization_url: Callable[[str], None]) -> Any:
        """Reuse, refresh, or open a browser. Returns the token (never logged)."""
        token = self._manager.login_if_needed(on_authorization_url)
        self._assert_no_write_scope(getattr(token, "scope", "") or "")
        return token

    def login_with_device_code_if_needed(
        self, on_device_authorization: Callable[[Any], None]
    ) -> Any:
        """Device-code flow (RFC 8628), for environments with no usable local browser."""
        token = self._manager.login_with_device_code_if_needed(on_device_authorization)
        self._assert_no_write_scope(getattr(token, "scope", "") or "")
        return token

    def get_access_token(self) -> str:
        return str(self._manager.get_access_token())

    def logout(self) -> None:
        self._manager.logout()

    def _assert_no_write_scope(self, granted: str) -> None:
        """Refuse any scope the session did not explicitly ask for.

        The rule is not "never hold a write scope" -- that would make CR-03 impossible to
        authorize. It is **"never hold a scope we did not request"**, which is strictly stronger
        and is what defends against a server that ignores the requested set: if consent came
        back wider than asked, the credential is not one this project is willing to hold.

        So the check is a set difference against what we asked for, not a blocklist. With the
        default (no write scope requested) the practical effect is identical to before: a write
        scope in the granted set is refused. With ``allow_write_scope=True`` the write scope is
        requested, so it is permitted -- and anything ELSE that appears is still refused.
        """
        granted_set = set(granted.split())
        requested = set(self._scopes.split())
        unexpected = sorted(granted_set - requested)
        if unexpected:
            msg = (
                f"the authorization server granted {unexpected}, which this session did not "
                f"request (it asked for {sorted(requested)}). Refusing a credential wider than "
                "the one intended; revoke the grant and re-authorize."
            )
            raise ConfigurationError(msg)
        if not self._allow_write_scope:
            offending = sorted(granted_set.intersection(FORBIDDEN_SCOPES))
            if offending:
                # Unreachable when the requested set is read-only, because such a grant would
                # already have failed the check above. Kept as a second lock on the same door.
                msg = (
                    f"the granted scope includes {offending} and this session was not built to "
                    "hold it. Revoke the grant and re-authorize, or ask for it deliberately "
                    "with --enable-order-submission."
                )
                raise ConfigurationError(msg)

    @property
    def permits_order_submission(self) -> bool:
        """Whether this session was deliberately authorized to place orders.

        A property rather than a raw scope check, so callers ask a question about intent rather
        than about a string, and so the one place that knows the answer is this class.
        """
        return self._allow_write_scope and WRITE_SCOPE in self._scopes.split()

    def describe(self) -> dict[str, str]:
        """Non-secret description of the session, safe to print or journal."""
        return {
            "issuer": self.issuer,
            "client_id": self.client_id or "(not yet registered)",
            "scopes": self.scopes,
            "token_home": str(self._home),
            "token": REDACTED,
        }


def build_oauth2_config(
    session: TigerQuantOAuthSession,
    *,
    environment: TigerEnvironment = TigerEnvironment.GATEWAY,
    account: str | None = None,
) -> Any:
    """Build a ``TigerOpenClientConfig`` wired for OAuth2.

    Parameters
    ----------
    session:
        A read-only :class:`TigerQuantOAuthSession`.
    environment:
        ``GATEWAY`` (default) for a real Tiger paper account. ``SANDBOX`` selects the
        separate developer sandbox host and its matching public key; the two must move
        together or response signature verification fails with an opaque
        ``response sign verify failed``, which is what happens if the gateway is switched
        without the key.
    account:
        The account number to use for account-scoped calls. Under OAuth2 the *account*
        decides paper versus live, so this value is verified against the broker's own
        account list by :mod:`tigerquant.adapters.tiger.guard` and is never trusted from
        configuration.

    Note there is deliberately **no** parameter that relaxes the account guard. Paper-only
    operation is enforced by verifying the broker's response, not by choosing a host.
    """
    authentication_type, config_type = load_oauth2_types()
    config = config_type()
    config.authentication = authentication_type(session.manager)

    if environment is TigerEnvironment.SANDBOX:
        from tigeropen.tiger_open_config import (
            SANDBOX_SERVER_URL,
            SANDBOX_SOCKET_HOST_PORT,
            SANDBOX_TIGER_PUBLIC_KEY,
        )

        # Gateway, socket endpoint and response-verification key move together.
        config.tiger_public_key = SANDBOX_TIGER_PUBLIC_KEY
        config.server_url = SANDBOX_SERVER_URL
        config.quote_server_url = SANDBOX_SERVER_URL
        config.socket_host_port = SANDBOX_SOCKET_HOST_PORT

    if account:
        # The SDK's account setter flips is_paper when the number looks like a paper
        # account. That flag is a hint only; the authoritative check is the broker's own
        # account list, performed by the guard.
        config.account = account
    return config
