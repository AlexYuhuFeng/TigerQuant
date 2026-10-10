"""The typed trade-decision contract.

`schema/trade_decision.schema.json` in the repository is the contract of record. It is
loaded from disk at runtime and validated with ``jsonschema`` **and** mirrored by the
pydantic model below for typed access — the two must agree, and a test asserts they do.

Running the file-based validator is what makes "a schema change is a reviewed repository
event" true (ADR-0006 §1): a prompt edit or a Python-side tweak cannot silently change
what a model is allowed to say.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tigerquant.core.enums import Action, ReasonCode
from tigerquant.core.errors import ConfigurationError, DecisionValidationError

__all__ = ["SCHEMA_FILENAME", "TradeDecision", "decision_schema_path", "load_decision_schema"]

SCHEMA_FILENAME = "trade_decision.schema.json"


def decision_schema_path() -> Path:
    """Locate ``schemas/trade_decision.schema.json``.

    Search order: ``$TIGERQUANT_SCHEMA_DIR``, then the repository root inferred by
    walking up from this file, then the current working directory. The schema must be
    found: silently falling back to a built-in copy would mean the repository file could
    drift from what is enforced.
    """
    import os

    candidates: list[Path] = []
    override = os.environ.get("TIGERQUANT_SCHEMA_DIR")
    if override:
        candidates.append(Path(override) / SCHEMA_FILENAME)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "schemas" / SCHEMA_FILENAME
        if candidate.is_file():
            candidates.append(candidate)
            break
    candidates.append(Path.cwd() / "schemas" / SCHEMA_FILENAME)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    msg = (
        f"could not locate schemas/{SCHEMA_FILENAME}; set TIGERQUANT_SCHEMA_DIR or run "
        "from the repository root"
    )
    raise ConfigurationError(msg)


def load_decision_schema(path: Path | None = None) -> dict[str, Any]:
    """Load the JSON Schema and compile a validator over it."""
    schema_path = path or decision_schema_path()
    try:
        raw = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"cannot load decision schema from {schema_path}: {exc}"
        raise ConfigurationError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"decision schema {schema_path} must be a JSON object"
        raise ConfigurationError(msg)
    return raw


class TradeDecision(BaseModel):
    """Typed mirror of the untrusted proposal shape.

    ``additionalProperties: false`` is deliberate: it is what prevents a model from
    smuggling in ``quantity``, ``notional`` or ``price`` fields and having them used
    (ADR-0006 §2).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: Annotated[str, Field(min_length=1)]
    model_version: Annotated[str, Field(min_length=1)]
    action: Action
    instrument: str | None
    target_weight: Decimal | None = Field(default=None, ge=0, le=1)
    horizon: Annotated[str, Field(min_length=1)]
    thesis: str
    counter_thesis: str
    invalidation: str
    evidence_ids: tuple[str, ...]
    uncertainties: tuple[str, ...]
    decision_id: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def _check_action_fields(self) -> TradeDecision:
        if self.action is Action.SET_TARGET and (
            self.instrument is None or self.target_weight is None
        ):
            msg = "SET_TARGET requires both instrument and target_weight"
            raise ValueError(msg)
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            msg = "evidence_ids must be unique"
            raise ValueError(msg)
        return self

    def requires_evidence(self) -> bool:
        """Actions that change exposure must cite evidence; others need not."""
        return self.action in {Action.SET_TARGET, Action.REDUCE, Action.CLOSE}

    def is_trade(self) -> bool:
        return self.action in {Action.SET_TARGET, Action.REDUCE, Action.CLOSE}

    def justification_fields(self) -> dict[str, str]:
        return {
            "thesis": self.thesis,
            "counter_thesis": self.counter_thesis,
            "invalidation": self.invalidation,
        }


def validate_against_schema(raw: dict[str, Any], *, schema: dict[str, Any] | None = None) -> None:
    """Validate ``raw`` against the repository schema, raising a typed error."""
    payload = schema if schema is not None else load_decision_schema()
    validator = Draft202012Validator(payload)
    errors = sorted(validator.iter_errors(raw), key=lambda err: list(err.absolute_path))
    if not errors:
        return
    first: JsonSchemaValidationError = errors[0]
    location = "/".join(str(part) for part in first.absolute_path) or "<root>"
    msg = f"decision failed schema validation at {location}: {first.message}"
    raise DecisionValidationError(msg, ReasonCode.SCHEMA_INVALID, stage="schema")
