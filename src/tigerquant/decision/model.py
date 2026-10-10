"""Provider-neutral model provider.

One implementation serves every provider LiteLLM supports, so swapping DeepSeek for another
vendor is a configuration change rather than a code change (ADR-0009). Nothing above this
module knows which vendor answered, and nothing below it knows about trading.

The important part of this file is not the successful path. It is that **every failure mode
ends in an abstention, never in a bypass**:

* a timeout, a connection error, a rate limit or a provider outage,
* a response that is not JSON, or JSON that does not match the decision schema,
* a response whose ``model_version`` is not the one we asked for,
* a response that tries to answer more than once.

Each returns ``NO_TRADE`` with a machine-readable reason, which the existing decision
validation then treats like any other abstention. There is no code path in which a model
failure produces a trade, and no path in which it skips a check (acceptance gate 11).

Two deliberate omissions:

* **No tool calling and no function registry.** The provider's only capability is "return
  text". ``DecisionRequest`` carries no broker handle and this module imports nothing from
  ``execution`` or ``adapters``, so there is no tool boundary through which an order could be
  reached (acceptance gate 2).
* **No vendor SDK import at module scope.** LiteLLM is an optional extra; importing this
  module must not require it, and the transport is injectable so the whole thing is testable
  with no network and no API key.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from tigerquant.core.enums import Action, ReasonCode
from tigerquant.core.errors import ConfigurationError, IntegrationUnavailable
from tigerquant.core.redaction import redact
from tigerquant.decision.prompt import build_prompt, render_messages

if TYPE_CHECKING:
    from tigerquant.decision.provider import DecisionRequest

__all__ = [
    "ModelCall",
    "ModelTransport",
    "ProviderNeutralModelProvider",
    "abstention",
    "parse_model_json",
]

#: A callable that takes messages and returns the provider's raw response mapping. Injected so
#: tests need neither network nor credentials.
ModelTransport = "Callable[[list[dict[str, str]], str], dict[str, Any]]"


def abstention(
    request: DecisionRequest,
    *,
    reason: str,
    detail: str,
    reason_code: ReasonCode = ReasonCode.MODEL_UNAVAILABLE,
) -> dict[str, Any]:
    """A well-formed NO_TRADE decision.

    Returned for *every* failure, so the pipeline never has to handle "the model did not
    answer" as a special case. An abstention is a first-class decision, not an error.
    """
    return {
        "snapshot_id": request.snapshot.snapshot_id,
        "decision_id": request.decision_id,
        "model_version": request.model_version,
        "action": Action.NO_TRADE.value,
        "instrument": None,
        "target_weight": None,
        "horizon": "none",
        "thesis": f"no trade: {reason}",
        "counter_thesis": "",
        "invalidation": "",
        "evidence_ids": [],
        "uncertainties": [detail],
        "provider_failure": reason_code.value,
    }


@dataclass(frozen=True, slots=True)
class ModelCall:
    """The outcome of one model call, successful or not."""

    ok: bool
    payload: dict[str, Any] | None
    latency_ms: int
    model_version_returned: str
    error: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    prompt_digest: str = ""

    def describe(self) -> str:
        if self.ok:
            return f"ok in {self.latency_ms}ms from {self.model_version_returned}"
        return f"failed in {self.latency_ms}ms: {self.error}"


def parse_model_json(text: str) -> dict[str, Any] | None:
    """Parse a model's answer into a single JSON object, or ``None``.

    Tolerates a ```json fence, which models add even when told not to. Refuses anything that
    is not exactly one JSON object: an array, a bare scalar, or two concatenated objects are
    all rejected rather than guessed at, because "which of these did it mean" is precisely
    the kind of question that should never be answered by a heuristic in a trading system.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop the opening fence (with an optional language tag) and the closing one.
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
        stripped = stripped.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class ProviderNeutralModelProvider:
    """A decision provider backed by any LiteLLM-supported model.

    Implements :class:`~tigerquant.decision.provider.DecisionProvider`. Every failure mode
    returns an abstention; none returns a trade.
    """

    __slots__ = (
        "_api_key_env",
        "_clock",
        "_expected_model_version",
        "_last_call",
        "_max_tokens",
        "_model",
        "_temperature",
        "_timeout_seconds",
        "_transport",
    )

    def __init__(
        self,
        *,
        model: str,
        expected_model_version: str,
        transport: Any = None,
        api_key_env: str | None = None,
        timeout_seconds: float = 30.0,
        max_tokens: int = 1200,
        temperature: float = 0.0,
        clock: Any = None,
    ) -> None:
        if not model:
            msg = "a model identifier is required"
            raise ConfigurationError(msg)
        if transport is None or not callable(transport):
            # Required, not defaulted. This layer must not know how to reach a vendor
            # (ADR-0009 confines that to adapters/), so the composition root wires the
            # implementation in. A default here would also mean an unconfigured system
            # silently tries to call something.
            msg = (
                "a transport callable is required. The decision layer defines the port; the "
                "adapter layer provides the LiteLLM implementation; the composition root "
                "(pipeline/cli) wires them together."
            )
            raise ConfigurationError(msg)
        self._model = model
        #: The version we require the provider to report back. A provider that silently
        #: upgrades under us is a different system, and the answer is to stop, not to
        #: accept it (acceptance gate 11).
        self._expected_model_version = expected_model_version
        self._transport = transport
        self._api_key_env = api_key_env
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens
        #: Zero by default: a decision component whose output feeds a trading system should
        #: not be sampling. Determinism is worth more here than variety.
        self._temperature = temperature
        self._clock = clock or _SystemClock()
        self._last_call: ModelCall | None = None

    # -- DecisionProvider --------------------------------------------------------

    @property
    def model_version(self) -> str:
        return self._expected_model_version

    @property
    def prompt_version(self) -> str:
        from tigerquant.decision.prompt import PROMPT_TEMPLATE_VERSION

        return PROMPT_TEMPLATE_VERSION

    @property
    def last_call(self) -> ModelCall | None:
        """The most recent call, successful or not, for the operator surface."""
        return self._last_call

    def propose(self, request: DecisionRequest) -> dict[str, Any]:
        """Ask the model for one decision. Never raises; always returns a decision document."""
        bundle = build_prompt(request)
        call = self._call(render_messages(bundle), prompt_digest=bundle.prompt_digest)
        self._last_call = call

        if not call.ok or call.payload is None:
            return abstention(
                request,
                reason="model unavailable or unparseable",
                detail=call.error or "no payload",
                reason_code=ReasonCode.MODEL_UNAVAILABLE,
            )

        if (
            call.model_version_returned
            and call.model_version_returned != self._expected_model_version
        ):
            # The provider answered from a different model than the one approved for this
            # run. Accepting it would mean the recorded model_version is a lie.
            return abstention(
                request,
                reason="provider version changed",
                detail=(
                    f"expected {self._expected_model_version!r}, provider reported "
                    f"{call.model_version_returned!r}"
                ),
                reason_code=ReasonCode.MODEL_VERSION_UNPINNED,
            )

        return self._coerce(request, call.payload)

    def _coerce(self, request: DecisionRequest, payload: dict[str, Any]) -> dict[str, Any]:
        """Force identity fields to the values we sent, and reject an unknown action.

        The model is told to echo ``snapshot_id``, ``decision_id`` and ``model_version`` back.
        If it does not, we do not trust its answer to describe *our* request, so the identity
        fields are set from the request rather than from the reply. A model that could choose
        its own snapshot id could attribute a decision to evidence it never saw.
        """
        coerced = dict(payload)
        coerced["snapshot_id"] = request.snapshot.snapshot_id
        coerced["decision_id"] = request.decision_id
        coerced["model_version"] = request.model_version

        action = str(coerced.get("action", "")).strip().upper()
        if action not in {member.value for member in Action}:
            return abstention(
                request,
                reason="model returned an unusable action",
                detail=f"action {coerced.get('action')!r} is not one of {[m.value for m in Action]}",
                reason_code=ReasonCode.MODEL_OUTPUT_INVALID,
            )
        coerced["action"] = action

        for key in ("evidence_ids", "uncertainties"):
            value = coerced.get(key)
            if value is None:
                coerced[key] = []
            elif isinstance(value, str):
                # A single string where a list was asked for is a common and harmless
                # deviation; wrapping it is not the same as guessing, because the value is
                # still validated afterwards against the real evidence set.
                coerced[key] = [value]
        return coerced

    # -- transport ---------------------------------------------------------------

    def _call(self, messages: list[dict[str, str]], *, prompt_digest: str) -> ModelCall:
        started = time.monotonic()
        transport = self._transport
        try:
            response = transport(messages, self._model)
        except IntegrationUnavailable as exc:
            return self._failure(started, str(exc), prompt_digest)
        except Exception as exc:  # noqa: BLE001 - a model call must never propagate
            # Redacted: a provider error can echo the request, and the request can contain a
            # URL with a token in it.
            return self._failure(started, redact(f"{type(exc).__name__}: {exc}"), prompt_digest)

        elapsed = int((time.monotonic() - started) * 1000)
        if elapsed > self._timeout_seconds * 1000:
            # The transport may not enforce the timeout itself, so a slow answer is refused
            # here rather than trusted. A decision that arrives after the market moved is
            # worse than no decision.
            return ModelCall(
                ok=False,
                payload=None,
                latency_ms=elapsed,
                model_version_returned="",
                error=f"latency {elapsed}ms exceeded the {self._timeout_seconds}s budget",
                prompt_digest=prompt_digest,
            )

        return self._interpret(response, elapsed, prompt_digest)

    def _interpret(self, response: object, elapsed: int, prompt_digest: str) -> ModelCall:
        """Turn a provider response into either a payload or a precise failure."""
        if not isinstance(response, dict):
            return ModelCall(
                ok=False,
                payload=None,
                latency_ms=elapsed,
                model_version_returned="",
                error=f"transport returned {type(response).__name__}, expected a mapping",
                prompt_digest=prompt_digest,
            )
        text = _first_content(response)
        reported = str(
            response.get("model") or response.get("model_version") or self._expected_model_version
        )
        if text is None:
            return ModelCall(
                ok=False,
                payload=None,
                latency_ms=elapsed,
                model_version_returned=reported,
                error="the provider returned no message content",
                prompt_digest=prompt_digest,
            )
        payload = parse_model_json(text)
        if payload is None:
            return ModelCall(
                ok=False,
                payload=None,
                latency_ms=elapsed,
                model_version_returned=reported,
                error="the response was not a single JSON object",
                prompt_digest=prompt_digest,
            )
        return ModelCall(
            ok=True,
            payload=payload,
            latency_ms=elapsed,
            model_version_returned=reported,
            usage=dict(response.get("usage") or {}),
            prompt_digest=prompt_digest,
        )

    def _failure(self, started: float, error: str, prompt_digest: str) -> ModelCall:
        return ModelCall(
            ok=False,
            payload=None,
            latency_ms=int((time.monotonic() - started) * 1000),
            model_version_returned="",
            error=error,
            prompt_digest=prompt_digest,
        )

    # -- reporting ---------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """Audit-safe description. No key, no prompt text, no endpoint."""
        return {
            "model": self._model,
            "expected_model_version": self._expected_model_version,
            "prompt_version": self.prompt_version,
            "timeout_seconds": self._timeout_seconds,
            "temperature": self._temperature,
            "transport": getattr(self._transport, "__name__", "injected"),
            "api_key_env": self._api_key_env or "(none)",
        }


def _first_content(response: dict[str, Any]) -> str | None:
    """Extract the assistant text from an OpenAI-shaped chat completion."""
    try:
        choices = response["choices"]
    except (KeyError, TypeError):
        return None
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
    content = first.get("text")
    return content if isinstance(content, str) else None


class _SystemClock:
    """Fallback clock. The pipeline injects the real one; this exists so the provider can be
    constructed in isolation."""

    __slots__ = ()

    def now(self) -> Any:
        import datetime as dt

        return dt.datetime.now(tz=dt.UTC)


def coerce_decimal(value: object) -> Decimal | None:
    """Parse a model-supplied number without ever trusting its precision."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - any unparseable value is simply absent  # noqa: S110
        return None
