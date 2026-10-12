"""A repository proxy that re-establishes its connection after a connection-shaped failure.

Verification runs on EVERY connection, not only the first. A guard that runs once expires at the
first reconnect, and a reconnect is precisely when the guarantee can silently stop holding.

Why this exists. The supervisor's backoff tier retries a transient failure, but it retried the
CYCLE while the repository kept its original connection. Against a database that had actually
restarted -- which is the case backoff is FOR -- every retry failed again for a reason backoff
cannot fix, and the loop halted after exhausting its budget having never once tried a fresh
connection.

The fix belongs here rather than in the supervisor, because the supervisor supervises a CALLABLE
and knows nothing about databases. Making the repository reconnect underneath keeps that boundary
and means the monitor, the cycle and the supervisor all need no changes.

Two decisions worth stating:

**It reconnects on the NEXT call, not during the failing one.** The failure is propagated
unchanged, so the classifier still sees the real exception and the supervisor still decides whether
to retry. A proxy that swallowed the error and retried internally would hide the failure from the
backoff accounting and could block for an unbounded time inside a single cycle.

**It delegates by attribute rather than by subclassing the repository.** The concrete repository
lives in a module that needs the optional `db` extra; this one must import without it, so it cannot
name the type it wraps.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["ReconnectingRepository"]


class ReconnectingRepository:
    """Wraps ``(connect, repository_for)`` and reconnects after a connection failure.

    ``connect`` returns a live connection; ``repository_for(connection)`` builds the repository
    that uses it. Both are injected so this class never imports a driver.
    """

    __slots__ = (
        "_connect",
        "_connection",
        "_ever_connected",
        "_is_dead",
        "_reconnects",
        "_repository",
        "_repository_for",
        "_verify_on_connect",
    )

    def __init__(
        self,
        connect: Callable[[], Any],
        repository_for: Callable[[Any], Any],
        *,
        is_dead: Callable[[BaseException], bool],
        verify_on_connect: Callable[[Any], None] | None = None,
    ) -> None:
        self._connect = connect
        self._repository_for = repository_for
        self._is_dead = is_dead
        #: Called with EVERY new connection, including reconnections.
        #:
        #: A guard that runs only at startup expires at the first reconnect, and a reconnect is
        #: exactly when the guarantee can silently stop holding -- a failover, a DNS change, a load
        #: balancer moving the connection elsewhere. Raising from here is treated as PERMANENT by
        #: the supervisor's classifier, which is right: a reconnect that landed on the wrong
        #: database is not something to retry.
        self._verify_on_connect = verify_on_connect
        self._connection: Any = None
        self._repository: Any = None
        #: How many times a connection has been re-established. Exposed because "it recovered"
        #: and "it never actually dropped" are different claims.
        self._reconnects = 0
        self._ever_connected = False

    @property
    def connected(self) -> bool:
        """Whether a connection is currently held. Reported, not guessed."""
        return self._connection is not None

    @property
    def reconnects(self) -> int:
        return self._reconnects

    def _ensure(self) -> Any:
        """The live repository, connecting or RE-connecting if there is none."""
        if self._repository is None:
            reconnecting = self._connection is not None or self._ever_connected
            if reconnecting:
                # A connection was held and has been dropped, so this is a RE-connection.
                self._reconnects += 1
            self._ever_connected = True
            connection = self._connect()
            if self._verify_on_connect is not None:
                try:
                    self._verify_on_connect(connection)
                except Exception:
                    # Do NOT keep a connection that failed verification. It is closed and the
                    # error propagates, so the caller sees a refused reconnect rather than a
                    # working repository pointed at the wrong database.
                    close = getattr(connection, "close", None)
                    if callable(close):
                        with suppress(Exception):
                            close()
                    if reconnecting:
                        self._reconnects -= 1
                    raise
            self._connection = connection
            self._repository = self._repository_for(connection)
        return self._repository

    def disconnect(self) -> None:
        """Drop the connection so the next call reconnects.

        Called after a connection-shaped failure, and available to a caller that knows the
        database went away by some other route.
        """
        self._repository = None
        connection, self._connection = self._connection, None
        close = getattr(connection, "close", None)
        if callable(close):
            # Closing a connection that is ALREADY broken is expected to fail, and there is
            # nothing useful to do about it: the connection is being discarded either way, and
            # raising here would replace the real error with a bookkeeping one.
            with suppress(Exception):
                close()

    def __getattr__(self, name: str) -> Any:
        # __getattr__ is only consulted when normal lookup fails, so the slots above are reached
        # directly and only the wrapped repository's methods come through here.
        repository = self._ensure()
        attribute = getattr(repository, name)
        if not callable(attribute):
            return attribute

        def guarded(*args: Any, **kwargs: Any) -> Any:
            try:
                return attribute(*args, **kwargs)
            except Exception as exc:
                if self._is_dead(exc):
                    # Mark disconnected for the NEXT call and re-raise. The supervisor's
                    # classifier must still see the real exception, or backoff would be deciding
                    # on a substitute error.
                    self.disconnect()
                raise

        return guarded
