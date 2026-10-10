"""Model-provider adapters. Vendor SDK imports are confined to this package (ADR-0009)."""

from __future__ import annotations

from tigerquant.adapters.llm.litellm_transport import litellm_transport

__all__ = ["litellm_transport"]
