"""Parsing untrusted model output.

The single most important detail here: ``parse_float=Decimal``. ``json.loads`` defaults
to binary floats, so ``{"target_weight": 0.05}`` would become the nearest double and the
target weight would silently be 0.05000000000000000277... That is unacceptable for a
number that sizes a real order, and it would break byte-identical replay (ADR-0011 §5).

The parser also extracts the first balanced JSON object from prose, because models wrap
JSON in commentary; extraction is tolerant, validation is not.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

__all__ = ["DecisionParseError", "extract_json_object", "parse_decision_json"]


class DecisionParseError(ValueError):
    """The model output contained no usable JSON object."""


def _scan_balanced_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` block, ignoring braces inside strings."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        start = text.find("{", start + 1)
    return None


def extract_json_object(text: str) -> str:
    """Extract the first balanced JSON object from ``text``."""
    block = _scan_balanced_object(text)
    if block is None:
        msg = "no JSON object found in model output"
        raise DecisionParseError(msg)
    return block


def parse_decision_json(text: str) -> dict[str, Any]:
    """Parse model output into a mapping with exact decimals.

    Raises
    ------
    DecisionParseError
        If no JSON object is present or it is malformed.
    """
    block = extract_json_object(text)
    try:
        parsed = json.loads(block, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        msg = f"model output is not valid JSON: {exc}"
        raise DecisionParseError(msg) from exc
    if not isinstance(parsed, dict):
        msg = f"model output must be a JSON object, got {type(parsed).__name__}"
        raise DecisionParseError(msg)
    return parsed
