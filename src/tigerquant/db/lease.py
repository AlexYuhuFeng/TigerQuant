"""Worker lease — exactly one execution owner per mode (ADR-0008 §7, gate 8).

The lease answers one question: *may this process submit orders?* Two processes sharing a
broker account and both believing they own it is the failure mode this prevents, so the
answer must be derived from shared, atomic state rather than from configuration.

Two implementations, one contract:

* :class:`InMemoryLease` — offline tests and the replay slice. It enforces the same rules
  (single owner, expiry, heartbeat, no silent takeover of a live lease).
* :class:`~tigerquant.db.postgres.PostgresLease` — the deployment implementation, using a
  single atomic ``INSERT ... ON CONFLICT DO UPDATE ... WHERE`` statement so that two
  concurrent acquirers cannot both win.

Deliberate properties:

* **Expiry is mandatory.** A lease without an expiry would survive a crashed worker and
  block the mode forever.
* **A live lease cannot be stolen.** Acquisition fails while ``expires_at`` is in the
  future, however stale the heartbeat looks.
* **Takeover is recorded.** Every acquisition returns the previous owner (if any) so the
  caller can journal a takeover, which is exactly the event an operator needs after an
  unexplained restart.
* **Releasing is owner-only.** A process that lost its lease cannot release it out from
  under the real owner.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from tigerquant.core.clock import ensure_utc
from tigerquant.core.errors import TigerQuantError

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "InMemoryLease",
    "Lease",
    "LeaseError",
    "LeaseManager",
    "LeaseState",
]

#: How long a lease stays valid without a heartbeat. Long enough to survive a slow cycle,
#: short enough that a crashed worker does not block the mode for a session.
DEFAULT_LEASE_SECONDS = 120


class LeaseError(TigerQuantError):
    """The lease could not be acquired or released."""


@dataclass(frozen=True, slots=True)
class LeaseState:
    """Who holds the lease, and until when."""

    name: str
    owner: str | None
    acquired_at: dt.datetime | None
    heartbeat_at: dt.datetime | None
    expires_at: dt.datetime | None

    @property
    def is_held(self) -> bool:
        return self.owner is not None

    def is_expired(self, now: dt.datetime) -> bool:
        if self.expires_at is None:
            return True
        return ensure_utc(now) >= self.expires_at

    def holder_is_live(self, now: dt.datetime) -> bool:
        return self.is_held and not self.is_expired(now)


@dataclass(frozen=True, slots=True)
class Lease:
    """A successfully acquired lease."""

    name: str
    owner: str
    acquired_at: dt.datetime
    expires_at: dt.datetime
    #: The previous holder, when this acquisition took over an expired lease.
    previous_owner: str | None = None

    def describe(self) -> str:
        if self.previous_owner:
            return (
                f"lease {self.name!r} acquired by {self.owner} (took over expired lease of "
                f"{self.previous_owner}), expires {self.expires_at.isoformat()}"
            )
        return (
            f"lease {self.name!r} acquired by {self.owner}, expires {self.expires_at.isoformat()}"
        )


@runtime_checkable
class LeaseManager(Protocol):
    """What the execution service requires of a lease."""

    @property
    def name(self) -> str: ...

    def state(self) -> LeaseState: ...

    def acquire(self, owner: str) -> Lease | None:
        """Take the lease, or return ``None`` when another live owner holds it."""
        ...

    def heartbeat(self, owner: str) -> bool:
        """Extend the lease. ``False`` means the lease is no longer ours."""
        ...

    def release(self, owner: str) -> bool:
        """Release the lease. ``False`` means we were not the owner."""
        ...


class InMemoryLease:
    """Single-process lease used by the offline slice and the contract tests."""

    __slots__ = ("_clock", "_name", "_seconds", "_state")

    def __init__(
        self,
        name: str,
        *,
        clock: Callable[[], dt.datetime],
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        if lease_seconds < 1:
            msg = "lease_seconds must be positive"
            raise LeaseError(msg)
        self._name = name
        self._clock = clock
        self._seconds = lease_seconds
        self._state = LeaseState(
            name=name, owner=None, acquired_at=None, heartbeat_at=None, expires_at=None
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def lease_seconds(self) -> int:
        return self._seconds

    def state(self) -> LeaseState:
        return self._state

    def acquire(self, owner: str) -> Lease | None:
        if not owner:
            msg = "lease owner must be non-empty; an anonymous lease cannot be released"
            raise LeaseError(msg)
        now = ensure_utc(self._clock())
        if self._state.holder_is_live(now):
            if self._state.owner == owner:
                # Re-acquiring our own live lease refreshes it; it is not a takeover.
                return self._grant(owner, now, None)
            return None
        previous = self._state.owner if self._state.is_held else None
        return self._grant(owner, now, previous)

    def _grant(self, owner: str, now: dt.datetime, previous: str | None) -> Lease:
        expires = now + dt.timedelta(seconds=self._seconds)
        self._state = LeaseState(
            name=self._name,
            owner=owner,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=expires,
        )
        return Lease(
            name=self._name,
            owner=owner,
            acquired_at=now,
            expires_at=expires,
            previous_owner=previous,
        )

    def heartbeat(self, owner: str) -> bool:
        state = self._state
        now = ensure_utc(self._clock())
        if state.owner != owner or state.is_expired(now):
            return False
        self._state = LeaseState(
            name=self._name,
            owner=owner,
            acquired_at=state.acquired_at,
            heartbeat_at=now,
            expires_at=now + dt.timedelta(seconds=self._seconds),
        )
        return True

    def release(self, owner: str) -> bool:
        if self._state.owner != owner:
            return False
        self._state = LeaseState(
            name=self._name, owner=None, acquired_at=None, heartbeat_at=None, expires_at=None
        )
        return True

    def force_release(self) -> None:
        """Test/operator hook: drop the lease regardless of owner."""
        self._state = LeaseState(
            name=self._name, owner=None, acquired_at=None, heartbeat_at=None, expires_at=None
        )

    def describe(self) -> str:
        if not self._state.is_held:
            return f"lease {self._name!r} is free"
        expires = self._state.expires_at
        stamp = "unknown" if expires is None else expires.isoformat()
        return f"lease {self._name!r} held by {self._state.owner} until {stamp}"
