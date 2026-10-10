"""Trigger a Tiger OpenAPI OAuth 2.0 authorization and verify the account is PAPER.

Read-only by construction:

* the requested scope set never includes ``api.trade:write`` (see
  :mod:`tigerquant.adapters.tiger.oauth`), so the resulting credential cannot place an
  order even if a bug elsewhere tried to;
* this tool performs no order call and imports no write path;
* the token is stored outside Git (``<workspace>/.secrets/tiger`` by default) and is never
  printed, logged or copied into an artifact.

Usage::

    python tools/tiger_login.py --flow browser --timeout 900
    python tools/tiger_login.py --check           # report state, trigger nothing
    python tools/tiger_login.py --logout          # revoke and clear the local token

Environment:
    TIGERQUANT_TIGER_HOME   token directory (default ``<cwd>/.secrets/tiger``)
    TIGERQUANT_TIGER_ACCOUNT  account number to verify (optional; omit to accept the only
                              account the broker reports)
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tigerquant.adapters.tiger.guard import TigerAccountGuard, mask_account  # noqa: E402
from tigerquant.adapters.tiger.oauth import (  # noqa: E402
    TigerEnvironment,
    TigerQuantOAuthSession,
    build_oauth2_config,
    default_token_home,
)
from tigerquant.core.errors import TigerQuantError  # noqa: E402

URL_MARKER = "AUTHORIZATION_URL="


def _emit(key: str, value: str) -> None:
    """Print a machine-readable line that a supervising process can parse."""
    print(f"{key}{value}", flush=True)


def _report(session: TigerQuantOAuthSession, environment: TigerEnvironment) -> dict[str, str]:
    described = session.describe()
    described["environment"] = environment.value
    _emit("SESSION=", json.dumps(described, sort_keys=True))
    return described


def _account_file() -> Path:
    """Where the selected paper account number is remembered.

    Inside the git-ignored token directory, so the identifier never reaches Git, a log or a
    command line. It is an identifier, not a secret, but it is still personal data and does
    not belong in the repository.
    """
    return default_token_home() / "account"


def _verify_account(
    session: TigerQuantOAuthSession,
    environment: TigerEnvironment,
    account: str | None,
    *,
    select_paper: bool,
) -> bool:
    """Verify the broker-reported account when the full SDK is installed."""
    try:
        from tigeropen.trade.trade_client import TradeClient
    except ImportError as exc:
        _emit("ACCOUNT_VERIFICATION=", f"pending: {type(exc).__name__}: {exc}")
        _emit(
            "NOTE=",
            "install the broker extra (uv sync --extra broker) and re-run with --check to "
            "verify the account is PAPER",
        )
        return False

    stored = _account_file()
    if account is None and not select_paper and stored.is_file():
        account = stored.read_text(encoding="utf-8").strip() or None
        if account:
            _emit("ACCOUNT_SOURCE=", f"stored selection ({mask_account(account)})")

    try:
        config = build_oauth2_config(session, environment=environment, account=account)
        client = TradeClient(config)
        guard = TigerAccountGuard(requested_account=account)
        verdict = (
            guard.select_from_client(client) if select_paper else guard.verify_from_client(client)
        )
    except TigerQuantError as exc:
        _emit("ACCOUNT_VERIFICATION=", f"refused: {exc}")
        return False

    _emit("ACCOUNT_VERIFIED=", "true" if verdict.verified else "false")
    _emit("ACCOUNT_DETAIL=", verdict.detail)
    for identity in verdict.observed:
        _emit("ACCOUNT_SEEN=", json.dumps(identity.as_record(), sort_keys=True))
    if verdict.verified and verdict.identity is not None:
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_text(verdict.identity.account, encoding="utf-8")
        _emit("ACCOUNT_STORED=", verdict.identity.masked())
    return verdict.verified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--flow", choices=("browser", "device", "auto"), default="auto")
    parser.add_argument("--timeout", type=int, default=900, help="callback timeout, seconds")
    parser.add_argument("--account", default=None, help="account number to verify")
    parser.add_argument("--check", action="store_true", help="report state; trigger nothing")
    parser.add_argument(
        "--select-paper",
        action="store_true",
        help=(
            "choose the unique PAPER account from the broker list, store it outside Git, "
            "and verify it. Refuses if there is not exactly one."
        ),
    )
    parser.add_argument(
        "--environment",
        choices=("gateway", "sandbox"),
        default="gateway",
        help=(
            "gateway (default) is the normal host, correct for a real Tiger PAPER account. "
            "sandbox is the separate developer sandbox, which needs sandbox credentials."
        ),
    )
    parser.add_argument("--logout", action="store_true", help="revoke and clear the token")
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="ask the operating system to open the authorization URL",
    )
    args = parser.parse_args(argv)

    # A real Tiger paper account lives on the ordinary gateway; the paper/live decision is
    # made by the account guard, not by the host. `--environment sandbox` selects the
    # separate developer sandbox host instead, which is a different thing entirely.
    environment = (
        TigerEnvironment.SANDBOX if args.environment == "sandbox" else TigerEnvironment.GATEWAY
    )
    home = default_token_home()
    _emit("TOKEN_HOME=", str(home))

    try:
        session = TigerQuantOAuthSession(authorize_timeout_seconds=args.timeout)
    except TigerQuantError as exc:
        _emit("ERROR=", str(exc))
        return 2

    if args.logout:
        session.logout()
        _emit("LOGOUT=", "done")
        return 0

    _report(session, environment)

    if args.check:
        if not session.is_authorized():
            _emit("STATE=", "not-authorized")
            return 1
        _emit("STATE=", "authorized")
        return (
            0
            if _verify_account(session, environment, args.account, select_paper=args.select_paper)
            else 1
        )

    url_seen: list[str] = []

    def on_url(url: str) -> None:
        url_seen.append(url)
        _emit(URL_MARKER, url)
        print(
            "\nOpen this URL in a browser and sign in with your TIGER PAPER account.\n"
            "The page will show the scopes being granted: TigerQuant requests READ-ONLY\n"
            "access (basic, account, quote, trade:read). It never requests trade:write.\n",
            flush=True,
        )
        if args.open_browser:
            try:
                webbrowser.open(url)
            except Exception as exc:
                # The URL was already printed above, so a machine with no browser is a
                # normal outcome here, not a reason to fail the login.
                _emit("BROWSER_LAUNCH=", f"failed ({type(exc).__name__}); use the URL above")

    def on_device(device: object) -> None:
        complete = getattr(device, "verification_uri_complete", None)
        uri = getattr(device, "verification_uri", None)
        code = getattr(device, "user_code", None)
        target = complete or uri
        if target:
            url_seen.append(str(target))
            _emit(URL_MARKER, str(target))
        _emit("USER_CODE=", str(code))
        print(
            "\nOpen the URL above in a browser and enter the code shown.\n",
            flush=True,
        )

    flows = ("browser", "device") if args.flow == "auto" else (args.flow,)
    last_error: Exception | None = None
    for flow in flows:
        try:
            if flow == "browser":
                token = session.login_if_needed(on_url)
            else:
                token = session.login_with_device_code_if_needed(on_device)
        except Exception as exc:
            last_error = exc
            _emit("FLOW_FAILED=", f"{flow}: {type(exc).__name__}: {exc}")
            continue

        _emit("FLOW_USED=", flow)
        _emit("GRANTED_SCOPE=", str(getattr(token, "scope", "") or ""))
        _emit("HAS_REFRESH_TOKEN=", str(bool(getattr(token, "has_refresh_token", lambda: False)())))
        remaining = getattr(token, "remaining_millis", None)
        if callable(remaining):
            _emit("EXPIRES_IN_SECONDS=", str(int(remaining() // 1000)))
        _emit("LOGIN=", "ok")
        _verify_account(session, environment, args.account, select_paper=args.select_paper)
        return 0

    _emit("LOGIN=", "failed")
    if last_error is not None:
        _emit("ERROR=", f"{type(last_error).__name__}: {last_error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
