"""Canonical JSON encoding and content digests.

Exactly one function defines how bytes are produced for hashing and for the audit
journal (:func:`canonical_json_bytes`). If two components disagree about encoding, the
hash chain silently stops meaning anything, so nothing else in the codebase may call
``json.dumps`` for a value that will be hashed.

Rejected on purpose (see :class:`~tigerquant.core.errors.CanonicalizationError`):
``float`` (inexact), ``set``/``frozenset`` (unordered iteration), and mappings with
non-string keys.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from enum import Enum
from typing import Any

from tigerquant.core.clock import ensure_utc
from tigerquant.core.errors import CanonicalizationError
from tigerquant.core.money import dec_str

__all__ = ["canonical_json_bytes", "digest_of", "sha256_hex", "to_canonical"]


def to_canonical(value: Any) -> Any:
    """Recursively normalise ``value`` into canonical JSON-compatible primitives."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        msg = (
            f"float is not canonicalisable (got {value!r}); "
            "use Decimal, int or str for values that are hashed"
        )
        raise CanonicalizationError(msg)
    if isinstance(value, Decimal):
        return dec_str(value)
    if isinstance(value, dt.datetime):
        return ensure_utc(value).isoformat(timespec="microseconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Enum):
        return to_canonical(value.value)
    if isinstance(value, (set, frozenset)):
        msg = f"unordered collection is not canonicalisable: {type(value).__name__}"
        raise CanonicalizationError(msg)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        # pydantic v2 model; duck-typed so that ``core`` needs no import of it.
        return to_canonical(model_dump(mode="python"))
    if isinstance(value, dict):
        items: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                msg = f"mapping keys must be str for canonical JSON, got {type(key).__name__}"
                raise CanonicalizationError(msg)
            items[key] = to_canonical(item)
        return items
    if isinstance(value, (list, tuple)):
        return [to_canonical(item) for item in value]
    if hasattr(value, "value") and isinstance(getattr(value, "value", None), str):
        # StrEnum-like objects that are not Enum subclasses.
        return to_canonical(value.value)
    msg = f"cannot canonicalise value of type {type(value).__name__}"
    raise CanonicalizationError(msg)


def canonical_json_bytes(value: Any) -> bytes:
    """UTF-8 canonical JSON: sorted keys, compact separators, no NaN/Infinity."""
    normalised = to_canonical(value)
    return json.dumps(
        normalised,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Lowercase hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def digest_of(value: Any) -> str:
    """``sha256:<hex>`` digest of the canonical encoding of ``value``."""
    return f"sha256:{sha256_hex(canonical_json_bytes(value))}"
