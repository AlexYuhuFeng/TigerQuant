"""Deterministic identifiers.

There is no ``uuid4()`` in this codebase. Every identifier is derived from an injected
:class:`~tigerquant.core.clock.Clock` plus a per-factory monotonic counter, which makes
IDs stable across runs of the same scenario (ADR-0011) and lets a restart re-derive the
same order-intent ID for the same logical intent instead of inventing a second one
(ADR-0008).

Encoded form: ``<kind>_<10 base32 chars of epoch-ms><4 base32 chars of counter>``, using
the Crockford alphabet (no ``I``/``L``/``O``/``U``), sorted lexicographically in roughly
creation order.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from tigerquant.core.clock import ensure_utc
from tigerquant.core.errors import CanonicalizationError

if TYPE_CHECKING:
    from tigerquant.core.clock import Clock

__all__ = ["CROCKFORD", "IdFactory", "encode_base32", "stable_token"]

#: Crockford base32: excludes I, L, O and U to avoid visual ambiguity.
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_MS_CHARS = 10  # 50 bits of milliseconds — ~35 000 years
_SEQ_CHARS = 4  # 20 bits — 1 048 576 ids per kind per process
_SEQ_MASK = (1 << (5 * _SEQ_CHARS)) - 1
_FACTORY_PREFIX = "tq"


def encode_base32(value: int, length: int) -> str:
    """Encode a non-negative integer as ``length`` Crockford base32 characters."""
    if value < 0:
        msg = f"cannot encode a negative id component: {value}"
        raise CanonicalizationError(msg)
    if value >= (1 << (5 * length)):
        msg = f"id component {value} does not fit in {length} base32 characters"
        raise CanonicalizationError(msg)
    chars = ["0"] * length
    remaining = value
    for index in range(length - 1, -1, -1):
        chars[index] = CROCKFORD[remaining & 0x1F]
        remaining >>= 5
    return "".join(chars)


def stable_token(value: str, length: int = 16) -> str:
    """A short, stable, case-folded token derived from ``value``.

    Used for broker client order ids, which must be identical across a retry after an
    unknown submission outcome.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


class IdFactory:
    """Mints deterministic, sortable identifiers for one namespace.

    Parameters
    ----------
    namespace:
        Mode namespace (see :func:`tigerquant.core.enums.capabilities_for`). Part of
        the id, so identifiers from different modes can never collide or be confused.
    clock:
        Injected time source. A :class:`~tigerquant.core.clock.SimulatedClock` makes
        ids reproducible.
    """

    __slots__ = ("_clock", "_counters", "_namespace")

    def __init__(self, namespace: str, clock: Clock) -> None:
        if not namespace or not namespace.isascii() or not namespace.isalnum():
            msg = f"namespace must be a short alphanumeric string, got {namespace!r}"
            raise CanonicalizationError(msg)
        self._namespace = namespace.lower()
        self._clock = clock
        self._counters: dict[str, int] = {}

    @property
    def namespace(self) -> str:
        return self._namespace

    def next_id(self, kind: str) -> str:
        """Return the next identifier of ``kind`` (e.g. ``"oi"``, ``"dec"``, ``"ev"``)."""
        if not kind or not kind.isascii() or not kind.isalpha():
            msg = f"id kind must be a short alphabetic string, got {kind!r}"
            raise CanonicalizationError(msg)
        counter = self._counters.get(kind, 0)
        if counter > _SEQ_MASK:
            msg = f"id counter exhausted for kind {kind!r}"
            raise CanonicalizationError(msg)
        self._counters[kind] = counter + 1
        instant = ensure_utc(self._clock.now())
        millis = int(instant.timestamp() * 1000)
        return (
            f"{self._namespace}-{kind}_{encode_base32(millis, _MS_CHARS)}"
            f"{encode_base32(counter, _SEQ_CHARS)}"
        )

    def __repr__(self) -> str:
        return f"IdFactory(namespace={self._namespace!r})"


def global_prefix() -> str:
    """Repository-wide id prefix, kept for readability of raw journal records."""
    return _FACTORY_PREFIX
