"""Credential settings: one place a human configures Tiger and the model provider.

The problem this solves is real and was felt during development: Tiger credentials arrive by
OAuth, the model provider's by API key, and a third may arrive as a client secret. Each landed
somewhere different, and "is this configured?" could only be answered by knowing where to look.

Three rules shape the design.

**A secret is never written to a tracked file.** :func:`resolve_store` refuses a store directory
that Git tracks, and the write path double-checks with ``git check-ignore`` before writing. A
settings mechanism that could put a key into a public repository is worse than no settings
mechanism, because it makes the mistake easy.

**A value is never printed.** :func:`describe` reports a *fingerprint* — the last four characters
and a digest prefix — plus where the value came from and whether it is verified. That is enough
to answer "is this the key I think it is" without the value ever reaching a log, a screenshot or
a support ticket.

**Resolution order is explicit and documented.** Environment first, then the secret store, then a
config-file *reference* (a path or variable name, never a value). Environment first is deliberate:
containers and CI inject secrets that way, and a file left over from a previous session must not
silently win over what the deployment intends.

What this module deliberately does NOT do: it does not fetch, rotate or validate a credential
against a provider beyond an explicit ``verify`` call, and it does not decide whether a
credential *should* exist. ``api.trade:write`` remains an owner decision
(docs/LIVE_READINESS.md).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tigerquant.core.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "CREDENTIALS",
    "CredentialSpec",
    "CredentialState",
    "CredentialStatus",
    "SettingsReport",
    "resolve_store",
    "set_secret",
    "settings_report",
]

#: The store directory. Git-ignored, and verified so before any write.
DEFAULT_STORE = Path(".secrets")


class CredentialState(StrEnum):
    """How a credential is currently configured."""

    #: No value anywhere.
    MISSING = "MISSING"
    #: A value exists but has not been exercised against the provider.
    PRESENT = "PRESENT"
    #: A value exists and a live check succeeded.
    VERIFIED = "VERIFIED"
    #: A value exists but the last check failed.
    FAILED = "FAILED"
    #: A value exists in a location that would be committed. Refused.
    UNSAFE = "UNSAFE"


@dataclass(frozen=True, slots=True)
class CredentialSpec:
    """One configurable credential."""

    key: str
    label: str
    provider: str
    #: How a human obtains it, in one line.
    how: str
    #: Environment variables checked first, in order.
    env_vars: tuple[str, ...]
    #: Path under the store, or a glob when the filename is chosen by a tool.
    store_path: str
    #: When set, ``store_path`` is a directory and this pattern selects the file(s).
    store_glob: str = ""
    #: Whether the value is a secret that must never be printed.
    secret: bool = True
    #: Optional: the tool that configures it interactively.
    tool: str = ""

    def describe(self) -> str:
        return f"{self.key}: {self.how}"


#: The credentials a deployment needs. Adding one here is how it becomes configurable.
CREDENTIALS: tuple[CredentialSpec, ...] = (
    CredentialSpec(
        key="tiger.oauth",
        label="Tiger OpenAPI OAuth 2.0 token",
        provider="tiger",
        how=(
            "Tiger OAuth 2.0, read-only scopes. Run `python tools/tiger_login.py` and approve "
            "in a browser; the token is stored under the secret store. Add "
            "`--scopes api.trade:write` only if order submission is intended, and read "
            "docs/LIVE_READINESS.md first: write scope is NOT paper-bound."
        ),
        env_vars=("TIGERQUANT_TIGER_TOKEN",),
        # The OAuth session names its file after the dynamically-registered client id, so the
        # store location is a directory rather than a fixed path.
        store_path="tiger/tokens",
        store_glob="*.json",
        tool="tools/tiger_login.py",
    ),
    CredentialSpec(
        key="tiger.account",
        label="Tiger account number (verified paper account)",
        provider="tiger",
        how=(
            "Selected, not typed: `python tools/tiger_login.py --check --select-paper` verifies "
            "the account against the broker's own response and stores it."
        ),
        env_vars=("TIGERQUANT_TIGER_ACCOUNT",),
        store_path="tiger/account",
        tool="tools/tiger_login.py",
    ),
    CredentialSpec(
        key="llm.api_key",
        label="Model provider API key",
        provider="llm",
        how=(
            "API key for any LiteLLM-supported provider. Provide it on stdin, not on the "
            "command line, so it does not reach shell history: "
            "`tigerquant settings set llm.api_key` reads from stdin."
        ),
        env_vars=(
            "DEEPSEEK_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "TIGERQUANT_LLM_API_KEY",
        ),
        store_path="llm/api_key",
    ),
    CredentialSpec(
        key="llm.base_url",
        label="Model provider base URL (optional, for a self-hosted or proxied endpoint)",
        provider="llm",
        how="Not a secret. Set with `tigerquant settings set llm.base_url --value ...`.",
        env_vars=("TIGERQUANT_LLM_BASE_URL",),
        store_path="llm/base_url",
        secret=False,
    ),
    CredentialSpec(
        key="db.dsn",
        label="Database DSN (optional; per-mode development DSNs are derived if absent)",
        provider="db",
        how="Not a secret in development. Set with `--value`, or leave unset to use the per-mode DSN.",
        env_vars=("TIGERQUANT_DB_DSN",),
        store_path="db/dsn",
        secret=False,
    ),
)

_BY_KEY: dict[str, CredentialSpec] = {spec.key: spec for spec in CREDENTIALS}


def spec_for(key: str) -> CredentialSpec:
    try:
        return _BY_KEY[key]
    except KeyError:
        msg = f"unknown credential {key!r}; known: {sorted(_BY_KEY)}"
        raise ConfigurationError(msg) from None


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
def resolve_store(root: Path | str | None = None, *, require_ignored: bool = True) -> Path:
    """The secret store directory, refusing one that Git tracks.

    A settings mechanism that can write a key into a tracked directory is worse than none,
    because it makes the dangerous thing the easy thing. Checked with ``git check-ignore``
    rather than by reading ``.gitignore``, because only Git knows the real answer.
    """
    store = Path(root) if root is not None else DEFAULT_STORE
    if not require_ignored:
        return store
    if not _is_git_ignored(store):
        msg = (
            f"refusing to use {store} as a secret store: Git does not ignore it, so a key "
            "written there could be committed. Add it to .gitignore first, or pass "
            "require_ignored=False only for a throwaway location outside a repository."
        )
        raise ConfigurationError(msg)
    return store


def _is_git_ignored(path: Path) -> bool:
    """Whether Git ignores ``path``, asked of Git rather than of a config file."""
    try:
        # `git` is resolved from PATH deliberately, and the argument list is a constant plus a
        # path we control; there is no shell and no user input in the command.
        result = subprocess.run(  # noqa: S603
            ["git", "check-ignore", "-q", str(path)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        # No git available: refuse rather than assume it is safe.
        return False
    return result.returncode == 0


def _path_for(spec: CredentialSpec, store: Path) -> Path:
    return store / spec.store_path


def _resolve_file(spec: CredentialSpec, store: Path) -> Path | None:
    """The file holding this credential, or None.

    A single path is the common case. When ``store_glob`` is set the path is a directory whose
    filename is chosen by a tool (the OAuth session names its token after the client id it
    registered), so the newest matching file is used.
    """
    if not spec.store_glob:
        path = _path_for(spec, store)
        return path if path.is_file() else None
    directory = _path_for(spec, store)
    if not directory.is_dir():
        return None
    matches = sorted(directory.glob(spec.store_glob), key=lambda item: item.stat().st_mtime)
    return matches[-1] if matches else None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CredentialStatus:
    """What is known about one credential, without revealing it."""

    key: str
    label: str
    state: CredentialState
    #: Where the value came from: an env var name, a store path, or nothing.
    source: str = ""
    #: Last four characters, and a digest prefix. Never the value.
    fingerprint: str = ""
    verified_at: str = ""
    detail: str = ""

    @property
    def configured(self) -> bool:
        return self.state in {
            CredentialState.PRESENT,
            CredentialState.VERIFIED,
            CredentialState.FAILED,
        }

    def as_record(self) -> dict[str, str]:
        return {
            "key": self.key,
            "label": self.label,
            "state": self.state.value,
            "source": self.source,
            "fingerprint": self.fingerprint,
            "verified_at": self.verified_at,
            "detail": self.detail,
        }


def fingerprint(value: str) -> str:
    """A stable, non-reversible identifier for a secret.

    Enough to answer "is this the key I think it is" and to tell two keys apart in a log,
    without either value being recoverable.
    """
    if not value:
        return ""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    # The stored value can be JSON with trailing whitespace, and a fingerprint spanning a
    # newline makes every report line ragged. It is an identifier, not a value.
    flat = "".join(value.split())
    tail = flat[-4:] if len(flat) >= 4 else ""
    return f"…{tail} (sha256:{digest})"


def read_credential(
    spec: CredentialSpec,
    *,
    store: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> CredentialStatus:
    """Resolve one credential, in the documented order: environment, store, nothing."""
    environment = env if env is not None else os.environ
    for name in spec.env_vars:
        value = environment.get(name, "")
        if value:
            return CredentialStatus(
                key=spec.key,
                label=spec.label,
                state=CredentialState.PRESENT,
                source=f"environment {name}",
                fingerprint=fingerprint(value),
                detail="from the environment; this wins over any stored value by design",
            )

    if store is not None:
        path = _resolve_file(spec, store)
        if path is not None:
            value = path.read_text(encoding="utf-8").strip()
            if value:
                verified = _verified_path(spec, store).is_file()
                return CredentialStatus(
                    key=spec.key,
                    label=spec.label,
                    state=CredentialState.VERIFIED if verified else CredentialState.PRESENT,
                    source=str(path),
                    fingerprint=fingerprint(value),
                    verified_at=(
                        _verified_path(spec, store).read_text(encoding="utf-8").strip()
                        if verified
                        else ""
                    ),
                    detail="from the secret store",
                )

    return CredentialStatus(
        key=spec.key,
        label=spec.label,
        state=CredentialState.MISSING,
        detail=spec.how,
    )


@dataclass(frozen=True, slots=True)
class SettingsReport:
    """Everything configurable, and how to configure what is not."""

    store: str
    statuses: tuple[CredentialStatus, ...] = field(default_factory=tuple)

    @property
    def missing(self) -> tuple[CredentialStatus, ...]:
        return tuple(status for status in self.statuses if not status.configured)

    @property
    def ready_for_paper(self) -> bool:
        """Whether the two credentials a paper session needs are both present."""
        needed = {"tiger.oauth", "tiger.account"}
        return all(status.configured for status in self.statuses if status.key in needed)

    def summary(self) -> str:
        configured = sum(1 for status in self.statuses if status.configured)
        return (
            f"settings: {configured}/{len(self.statuses)} credential(s) configured; "
            f"store {self.store}; paper-ready={self.ready_for_paper}"
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "store": self.store,
            "paper_ready": self.ready_for_paper,
            "missing": [status.key for status in self.missing],
            "credentials": [status.as_record() for status in self.statuses],
        }

    def render(self) -> str:
        lines = [self.summary(), ""]
        for status in self.statuses:
            mark = {
                CredentialState.VERIFIED: "[ok]  ",
                CredentialState.PRESENT: "[set] ",
                CredentialState.FAILED: "[FAIL]",
                CredentialState.UNSAFE: "[!!]  ",
                CredentialState.MISSING: "[--]  ",
            }[status.state]
            lines.append(f"{mark} {status.key:<16} {status.state.value:<9} {status.fingerprint}")
            if status.source:
                lines.append(f"       source: {status.source}")
            if not status.configured:
                lines.append(f"       how:    {status.detail}")
        return "\n".join(lines)


def settings_report(
    *,
    store: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    require_ignored: bool = True,
) -> SettingsReport:
    """Report every credential's state without revealing any value."""
    try:
        resolved = resolve_store(store, require_ignored=require_ignored)
    except ConfigurationError as exc:
        return SettingsReport(
            store=f"UNSAFE: {exc}",
            statuses=tuple(
                CredentialStatus(
                    key=spec.key,
                    label=spec.label,
                    state=CredentialState.UNSAFE,
                    detail=str(exc),
                )
                for spec in CREDENTIALS
            ),
        )
    return SettingsReport(
        store=str(resolved),
        statuses=tuple(read_credential(spec, store=resolved, env=env) for spec in CREDENTIALS),
    )


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def set_secret(
    key: str,
    value: str,
    *,
    store: Path | str | None = None,
    mark_verified: bool = False,
) -> Path:
    """Write a credential to the secret store, refusing anything unsafe.

    Creates the file with owner-only permissions where the OS supports it, and refuses an empty
    value rather than leaving a file that looks configured and is not.
    """
    spec = spec_for(key)
    if not value.strip():
        msg = f"refusing to store an empty value for {key!r}"
        raise ConfigurationError(msg)

    resolved = resolve_store(store)
    path = _path_for(spec, resolved)
    if spec.store_glob:
        # A glob credential is owned by its tool; writing one here would create a file the tool
        # does not read and that looks configured. Refuse and point at the tool.
        msg = (
            f"{key!r} is written by {spec.tool or 'its own tool'}, not by the settings command: "
            f"the filename is chosen at run time. Use that tool, then re-run `settings show`."
        )
        raise ConfigurationError(msg)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Belt and braces: the directory was checked, and the file is checked again. A nested path
    # is what a mistake would look like.
    if not _is_git_ignored(path):
        msg = f"refusing to write {path}: Git does not ignore it"
        raise ConfigurationError(msg)

    path.write_text(value.strip(), encoding="utf-8")
    # Windows ACLs are not POSIX modes, so this may be unsupported; the directory being
    # git-ignored is the real control and this is defence in depth.
    with contextlib.suppress(OSError, NotImplementedError):
        path.chmod(0o600)

    if mark_verified:
        _verified_path(spec, resolved).write_text(
            dt.datetime.now(tz=dt.UTC).isoformat(), encoding="utf-8"
        )
    return path


def mark_credential_verified(key: str, *, store: Path | str | None = None) -> Path:
    """Record that a credential has been exercised against its provider and worked."""
    spec = spec_for(key)
    resolved = resolve_store(store)
    marker = _verified_path(spec, resolved)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(dt.datetime.now(tz=dt.UTC).isoformat(), encoding="utf-8")
    return marker


def clear_credential(key: str, *, store: Path | str | None = None) -> tuple[Path, ...]:
    """Remove a credential and its verification marker. Returns what was removed."""
    spec = spec_for(key)
    resolved = resolve_store(store)
    removed: list[Path] = []
    for path in (_path_for(spec, resolved), _verified_path(spec, resolved)):
        if path.is_file():
            path.unlink()
            removed.append(path)
    return tuple(removed)


def _verified_path(spec: CredentialSpec, store: Path) -> Path:
    """Where the verification marker lives.

    For a glob credential the marker sits beside the DIRECTORY, because the file it refers to
    is replaced whenever the tool re-authenticates.
    """
    if spec.store_glob:
        return store / f"{spec.store_path}/verified"
    return store / f"{spec.store_path}.verified"


def known_credentials() -> tuple[str, ...]:
    """Every configurable credential key."""
    return tuple(spec.key for spec in CREDENTIALS)
