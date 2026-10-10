"""TigerQuant — evidence-grounded, paper-first AI trading platform.

The package is layered (see ``docs/adr/ADR-0002``): ``core`` and ``events`` are the
foundation and must not import higher layers. Importing :mod:`tigerquant` must never
require an optional integration dependency (``tigeropen``, ``nautilus_trader``,
``litellm``, FastAPI).
"""

from __future__ import annotations

__all__ = ["IMPLEMENTATION_STATUS", "__version__"]

__version__ = "0.1.0"

#: Honest, machine-readable statement of what this build actually is.
#: SIMULATED — every external interaction is a deterministic local simulation.
#: Nothing in this build has been verified against a broker.
IMPLEMENTATION_STATUS = "SIMULATED"
