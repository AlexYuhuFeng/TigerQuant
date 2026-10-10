"""Secret redaction.

Nothing that reaches a log line, a journal record, an alert message or a model prompt
may contain credential material (AGENTS.md "Security"). Redaction is applied at the
boundary — the journal writer and the logging formatter both call :func:`redact` — so a
mistake at a call site cannot leak a secret into the audit trail.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["REDACTED", "redact", "redact_mapping", "scan_for_secrets"]

REDACTED = "***REDACTED***"

#: Ordered patterns. Each entry is (name, regex). Patterns are deliberately generic:
#: a false positive costs readability, a false negative costs a credential.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
        ),
    ),
    ("tiger_private_key", re.compile(r"\bMII[A-Za-z0-9+/=]{40,}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/=]{12,}", re.IGNORECASE)),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("deepseek_key", re.compile(r"\bsk-[a-f0-9]{32}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    (
        "key_value_assignment",
        # A negative lookahead keeps our own ``secret://`` reference URIs (which are
        # pointers, not secrets) from being flagged as credentials.
        re.compile(
            r"(?i)\b(?:private[_-]?key|secret|password|passwd|token|api[_-]?key|tiger_id)"
            r"\s*[:=]\s*[\"']?(?![/*])([A-Za-z0-9+/=_\-]{16,})[\"']?"
        ),
    ),
)


def redact(text: str) -> str:
    """Return ``text`` with credential-shaped substrings replaced by ``***REDACTED***``."""
    result = text
    for name, pattern in _PATTERNS:
        if name == "key_value_assignment":
            # Keep the field name so operators can see *which* setting was redacted.
            result = pattern.sub(lambda m: m.group(0).replace(m.group(1), REDACTED), result)
        else:
            result = pattern.sub(REDACTED, result)
    return result


def redact_mapping(value: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact a JSON-like structure, preserving shape."""
    out: dict[str, Any] = {}
    for key, item in value.items():
        out[key] = _redact_value(item)
    return out


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def scan_for_secrets(text: str) -> list[str]:
    """Return the names of patterns that matched ``text``.

    Used by the test suite and by ``tigerquant doctor`` to assert that fixtures and
    journal output contain no credential-shaped material.
    """
    return [name for name, pattern in _PATTERNS if pattern.search(text)]
