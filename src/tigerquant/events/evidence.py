"""Evidence reaching the journal, so it can be projected.

The evidence store is durable and canonical, and none of it reached the database: `evidence_facts` was
declared in the schema, both repositories implemented writing and the point-in-time read, and **NOTHING
CALLED EITHER**. A table that is never populated looks exactly like a table that works.

**THE PAYLOAD IS THE FACT'S OWN JSON, NOT A HAND-WRITTEN FIELD LIST.** The insert needs fifteen
columns, and copying them here would create a SECOND definition of :class:`Fact` kept in step with the
first by hand -- a shape this project has found six times. The projector rebuilds with
``Fact.model_validate``, so a field added to ``Fact`` needs no change to this module, the registry, or
the projector.
"""

from __future__ import annotations

from typing import Any, Literal

from tigerquant.events.base import EventBase

__all__ = ["EvidenceFactRecorded"]


class EvidenceFactRecorded(EventBase):
    """One ADMITTED evidence fact, carried for projection.

    Emitted only for facts the pipeline admitted. A rejected document is not evidence, and projecting
    one would put something in the audit trail that a decision was never allowed to rely on.
    """

    event_type: Literal["EvidenceFactRecorded"] = "EvidenceFactRecorded"

    #: ``Fact.model_dump(mode="json")``, validated back into a Fact by the projector rather than
    #: unpacked field by field -- see the module docstring.
    fact: dict[str, Any]
