"""NautilusTrader adapter package (ADR-0010).

The engine is an *external dependency*, never a fork, and never the risk authority or the
audit store.

Only :mod:`tigerquant.adapters.nautilus.modes` exists so far. It is deliberately free of any
engine import: the mode translation table and its two refusals are pure logic, so they are
testable without a large compiled wheel, and a wheel that cannot install does not block them.

The engine-backed classes -- ``InstrumentProvider``, ``DataClient`` and ``ExecutionClient`` --
are not written yet. They need ``nautilus_trader==1.231.0`` present, and must be written
against the pinned tag's source tree rather than any published guide: the project's own
research (ADR-0010, ``docs/integrations/nautilus_api_notes.md``) established that the public
"latest" documentation describes the v2 Rust-core line, whose adapter interface differs, and
that there is no versioned documentation path for 1.231.0.
"""

from __future__ import annotations

from tigerquant.adapters.nautilus.modes import (
    ENGINE_ENVIRONMENTS,
    ModeTranslation,
    describe_translation_table,
    translate,
)

__all__ = [
    "ENGINE_ENVIRONMENTS",
    "ModeTranslation",
    "describe_translation_table",
    "translate",
]
