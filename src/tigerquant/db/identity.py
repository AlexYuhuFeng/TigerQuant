"""Which Mode is this database for? Asked, and refused when the answer is wrong or absent.

THE DEFECT THIS CLOSES IS A FALSE NEGATIVE, which makes it more dangerous than anything else left
in CR-09. Every mode has its own database (ADR-0013 §2), and the DSN is just a string. Passing the
PAPER DSN while running `--mode BACKTEST` connected to the paper database, compared an empty paper
database against a backtest journal, found the expected divergence -- and would have reported a
clean run against the WRONG database. Nothing anywhere noticed, because a database with no rows
looks exactly like a database with no rows.

So the database states which mode it serves, once, and a connection asserts it.

WHY THIS DOES NOT CONTRADICT ADR-0013. The mode is still NOT on any row, no query filters by it,
and the database boundary remains the isolation mechanism. This is a single row describing the
DATABASE ITSELF -- a guard against misconfiguration, not a substitute for the boundary.

DENY BY DEFAULT, like the rest of this project: an EMPTY identity table means UNDECLARED, and
undeclared is REFUSED rather than assumed to match. A database migrated before this table existed
must be declared deliberately, because "I cannot tell what this is" and "this is what I expected"
are different answers and only one of them is safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tigerquant.core.enums import Mode
from tigerquant.core.errors import ConfigurationError

__all__ = [
    "DatabaseIdentity",
    "IdentityVerdict",
    "declare_mode",
    "read_identity",
    "verify_identity",
]


@dataclass(frozen=True, slots=True)
class DatabaseIdentity:
    """What a database says about itself."""

    mode: str
    declared_at: str = ""
    declared_by: str = ""

    @property
    def as_mode(self) -> Mode | None:
        try:
            return Mode(self.mode)
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class IdentityVerdict:
    """Whether it is safe to proceed, and why not when it is not."""

    ok: bool
    #: One of: "matches", "mismatch", "undeclared", "unreadable".
    status: str
    detail: str
    expected: str = ""
    found: DatabaseIdentity | None = None

    def raise_if_refused(self) -> None:
        if not self.ok:
            msg = f"refusing to use this database: {self.detail}"
            raise ConfigurationError(msg)

    @property
    def refused(self) -> bool:
        return not self.ok


def declare_mode(
    connection: Any,
    mode: Mode,
    *,
    declared_by: str = "",
) -> DatabaseIdentity:
    """Record which Mode this database serves.

    Refuses to CHANGE an existing declaration: a database whose mode silently changed is worse
    than one that refused to start, because every row in it was written under the old assumption.
    Re-declaring requires deliberately clearing the row, which is not offered here.
    """
    existing = read_identity(connection)
    if existing is not None:
        if existing.mode != mode.value:
            msg = (
                f"this database is declared as {existing.mode!r} (by {existing.declared_by or 'unknown'} "
                f"at {existing.declared_at or 'an unknown time'}) and cannot be re-declared as "
                f"{mode.value!r}. Every row in it was written under the old assumption; a "
                "silent change would make them unattributable. Use a different database."
            )
            raise ConfigurationError(msg)
        return existing

    cursor = connection.execute(
        "INSERT INTO database_identity (mode, declared_by) VALUES (%s, %s) "
        "ON CONFLICT (id) DO NOTHING RETURNING mode, declared_at, declared_by",
        (mode.value, declared_by),
    )
    row = cursor.fetchone()
    connection.commit()
    if row is None:
        # Another process declared it between the read and the insert. Re-read rather than guess.
        declared = read_identity(connection)
        if declared is None:
            msg = "the identity table vanished between the insert and the read"
            raise ConfigurationError(msg)
        if declared.mode != mode.value:
            msg = f"another process declared this database as {declared.mode!r}, not {mode.value!r}"
            raise ConfigurationError(msg)
        return declared
    return DatabaseIdentity(mode=str(row[0]), declared_at=str(row[1]), declared_by=str(row[2]))


def _is_undefined_table(exc: BaseException) -> bool:
    """Whether this is "the table does not exist" rather than a real failure.

    Checked by SQLSTATE (42P01) because that is the condition the standard defines, with a message
    check as a fallback for drivers that do not expose it. A false negative here means an
    undeclared database is reported as UNREADABLE, which errs toward refusing -- the right
    direction.
    """
    state = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if state == "42P01":
        return True
    text = str(exc).lower()
    return "does not exist" in text and "database_identity" in text


def read_identity(connection: Any) -> DatabaseIdentity | None:
    """The database's declaration, or None when undeclared or the table is absent.

    A missing TABLE and an empty TABLE are both reported as None, because from a caller's point of
    view they mean the same thing: this database has not said what it is.
    """
    try:
        cursor = connection.execute(
            "SELECT mode, declared_at, declared_by FROM database_identity LIMIT 1"
        )
        row = cursor.fetchone()
    except Exception as exc:
        # ONLY an absent table means "undeclared". The first version caught everything here, which
        # made erify_identity's "unreadable" branch UNREACHABLE -- a status that claimed a
        # behaviour it could not have, and would have reported a broken connection as a database
        # that simply had not declared itself. A test caught it.
        if _is_undefined_table(exc):
            return None
        raise
    if row is None:
        return None
    return DatabaseIdentity(mode=str(row[0]), declared_at=str(row[1]), declared_by=str(row[2]))


def verify_identity(
    connection: Any,
    expected: Mode,
    *,
    allow_undeclared: bool = False,
) -> IdentityVerdict:
    """Whether this database is the one ``expected`` refers to.

    ``allow_undeclared`` exists for reading an older database deliberately. It does NOT extend to
    a MISMATCH: connecting to a database declared as a different mode is refused either way,
    because that is the case where rows get written under the wrong assumption.
    """
    try:
        identity = read_identity(connection)
    except Exception as exc:  # noqa: BLE001 - reported as unreadable rather than swallowed
        return IdentityVerdict(
            ok=False,
            status="unreadable",
            expected=expected.value,
            detail=(
                f"could not read the database identity: {type(exc).__name__}: {str(exc)[:160]}. "
                "Refusing rather than assuming, because a database that cannot say what it is "
                "cannot be shown to be the right one."
            ),
        )

    if identity is None:
        if allow_undeclared:
            return IdentityVerdict(
                ok=True,
                status="undeclared",
                expected=expected.value,
                detail=(
                    "this database does not declare a mode and --allow-undeclared-database was "
                    "given, so it is being used on the operator's word. It cannot be checked."
                ),
            )
        return IdentityVerdict(
            ok=False,
            status="undeclared",
            expected=expected.value,
            detail=(
                "this database does not declare which mode it serves, so it cannot be shown to be "
                f"the {expected.value} one. Declare it, or pass --allow-undeclared-database to "
                "accept an unverifiable database deliberately."
            ),
        )

    if identity.mode != expected.value:
        return IdentityVerdict(
            ok=False,
            status="mismatch",
            expected=expected.value,
            found=identity,
            detail=(
                f"this database declares itself {identity.mode!r} but {expected.value!r} was "
                f"expected (declared by {identity.declared_by or 'unknown'} at "
                f"{identity.declared_at or 'an unknown time'}). Every mode has its own database, "
                "so this is the wrong one to be reconciling against."
            ),
        )

    return IdentityVerdict(
        ok=True,
        status="matches",
        expected=expected.value,
        found=identity,
        detail=f"declared as {identity.mode!r}, which matches",
    )
