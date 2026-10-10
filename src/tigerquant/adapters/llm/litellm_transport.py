"""LiteLLM transport: the vendor binding for the provider-neutral decision model.

This lives in ``adapters/`` because ADR-0009 confines vendor SDK imports to the adapter
layer. ``decision/model.py`` holds the provider-neutral logic — prompt assembly, response
parsing, failure handling — and knows nothing about LiteLLM; swapping vendors replaces this
file and nothing above it.

LiteLLM is an optional extra. Importing this module must not require it, so the import
happens inside the call.
"""

from __future__ import annotations

import os
from typing import Any

from tigerquant.core.errors import ConfigurationError, IntegrationUnavailable
from tigerquant.core.redaction import redact

__all__ = ["litellm_transport"]


def litellm_transport(
    messages: list[dict[str, str]],
    model: str,
    *,
    timeout_seconds: float,
    max_tokens: int,
    temperature: float,
    api_key_env: str | None = None,
) -> dict[str, Any]:
    """Call any LiteLLM-supported chat model and return a plain mapping.

    ``num_retries=0`` is deliberate. A retry makes latency unbounded and hides an outage
    behind a slower failure; the caller's abstention path handles both, and a decision that
    arrives after the market moved is worse than no decision.

    A missing API key raises ``ConfigurationError`` *before* the network, because a missing
    key is a misconfiguration rather than a provider outage, and conflating the two hides
    the actual problem. No key is ever read from a file, the repository, or the environment
    by any other route.
    """
    try:
        import litellm
    except ImportError as exc:  # pragma: no cover - environment dependent
        msg = "litellm is not installed. Install the optional extra: uv sync --frozen --extra llm"
        raise IntegrationUnavailable("LiteLLM", msg) from exc

    if api_key_env is not None and not os.environ.get(api_key_env):
        msg = (
            f"environment variable {api_key_env!r} is not set. The key is read only from the "
            "environment: never from a file, and never from this repository."
        )
        raise ConfigurationError(msg)

    try:
        response = litellm.completion(
            model=model,
            messages=messages,
            timeout=timeout_seconds,
            max_tokens=max_tokens,
            temperature=temperature,
            num_retries=0,
        )
    except Exception as exc:
        # Redacted because a provider error can echo the request, and the request can contain
        # a URL carrying a token.
        raise IntegrationUnavailable(
            "LiteLLM", f"{type(exc).__name__}: {redact(str(exc))}"
        ) from exc

    return _as_mapping(response)


def _as_mapping(value: object) -> dict[str, Any]:
    """Normalise an SDK response object into a plain mapping."""
    if isinstance(value, dict):
        return value
    for attribute in ("model_dump", "to_dict", "dict"):
        method = getattr(value, attribute, None)
        if callable(method):
            try:
                dumped = method()
            except TypeError:
                continue
            if isinstance(dumped, dict):
                return dumped
    msg = f"cannot interpret a {type(value).__name__} as a chat-completion response"
    raise IntegrationUnavailable("LiteLLM", msg)
