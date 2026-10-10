"""Decision-to-fill lineage: the operator's view of why anything happened.

CR-08's gate is "operator can inspect complete decision/evidence/execution lineage". That
sentence defines the data structure: one record per decision, carrying every hop from the
evidence it was shown, through the risk verdict, to the orders and fills that resulted, and the
ledger state afterwards.

Three properties matter more than the presentation:

**It is assembled from the journal, never from live objects.** The journal is the record of
authority (ADR-0004), so a lineage built from it describes what actually happened rather than
what some in-memory object currently believes. It also means the same view works for a finished
replay, a running paper session, or a forensic read of an old journal.

**A citation the model made up is visible as such.** :attr:`LineageRecord.evidence_not_shown`
is the set of evidence IDs the decision cited minus the fact IDs the snapshot actually
contained. It should always be empty — validation rejects a decision that cites unknown
evidence — but if it ever is not, the operator sees a fabrication in the lineage rather than
having to infer it from a rejection code.

**Nothing here is a projection.** This module reads; it does not write, project, repair or
mutate. The dashboard on top of it is read-only by construction, not by policy.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from tigerquant.core.canonical import digest_of
from tigerquant.core.money import ZERO, q_cash

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from tigerquant.events.base import EventBase

__all__ = [
    "LineageRecord",
    "LineageReport",
    "build_lineage",
    "render_csv",
    "render_json",
    "render_text",
]

#: Events that carry an execution.
_FILL_EVENTS = frozenset({"OrderFilled", "OrderPartiallyFilled"})

#: Order-lifecycle events, in the order a successful order passes through them.
_ORDER_STATES: tuple[str, ...] = (
    "OrderIntentCreated",
    "OrderSubmitted",
    "OrderAcknowledged",
    "OrderPartiallyFilled",
    "OrderFilled",
    "OrderCanceled",
    "OrderRejected",
    "OrderSubmissionUnknown",
    "OrderQuarantined",
    "OrderCancelRequested",
)


@dataclass(frozen=True, slots=True)
class LineageRecord:
    """One decision and everything downstream of it."""

    decision_id: str
    #: The snapshot the decision was shown.
    snapshot_id: str
    model_version: str
    prompt_version: str
    action: str
    instrument: str | None
    target_weight: Decimal | None
    thesis: str
    #: Evidence the model cited, and evidence the snapshot actually contained.
    evidence_cited: tuple[str, ...]
    evidence_available: tuple[str, ...]
    #: Cited but never shown. Always empty in a healthy system; a non-empty set is a
    #: fabrication and is surfaced deliberately rather than hidden behind a rejection code.
    evidence_not_shown: tuple[str, ...]
    #: The risk verdict, if the decision reached the gateway.
    risk_outcome: str | None = None
    risk_reason_codes: tuple[str, ...] = ()
    risk_checks_run: tuple[str, ...] = ()
    policy_hash: str | None = None
    #: Execution.
    intent_id: str | None = None
    client_order_id: str | None = None
    broker_order_id: str | None = None
    order_states: tuple[str, ...] = ()
    fills: tuple[dict[str, str], ...] = ()
    #: Ledger state after this decision's activity.
    cash_after: Decimal | None = None
    equity_after: Decimal | None = None
    decided_at: str = ""

    @property
    def filled_quantity(self) -> int:
        return sum(int(fill["quantity"]) for fill in self.fills)

    @property
    def traded_notional(self) -> Decimal:
        return q_cash(
            sum(
                (Decimal(fill["price"]) * int(fill["quantity"]) for fill in self.fills),
                ZERO,
            )
        )

    @property
    def reached_the_venue(self) -> bool:
        """Whether an order for this decision was ever submitted."""
        return self.broker_order_id is not None or "OrderSubmitted" in self.order_states

    @property
    def was_refused(self) -> bool:
        return self.risk_outcome is not None and self.risk_outcome.upper() == "DENY"

    def digest(self) -> str:
        return digest_of(
            {
                "decision_id": self.decision_id,
                "snapshot_id": self.snapshot_id,
                "evidence_cited": list(self.evidence_cited),
                "intent_id": self.intent_id,
                "fills": [fill["exec_id"] for fill in self.fills],
            }
        )

    def as_record(self) -> dict[str, Any]:
        """Audit-safe rendering. No account numbers, no tokens, no prompt text."""
        return {
            "decision_id": self.decision_id,
            "snapshot_id": self.snapshot_id,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "action": self.action,
            "instrument": self.instrument,
            "target_weight": None if self.target_weight is None else str(self.target_weight),
            "thesis": self.thesis,
            "evidence_cited": list(self.evidence_cited),
            "evidence_available": list(self.evidence_available),
            "evidence_not_shown": list(self.evidence_not_shown),
            "risk_outcome": self.risk_outcome,
            "risk_reason_codes": list(self.risk_reason_codes),
            "risk_checks_run": list(self.risk_checks_run),
            "policy_hash": self.policy_hash,
            "intent_id": self.intent_id,
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "order_states": list(self.order_states),
            "fills": list(self.fills),
            "filled_quantity": self.filled_quantity,
            "traded_notional": str(self.traded_notional),
            "reached_the_venue": self.reached_the_venue,
            "was_refused": self.was_refused,
            "cash_after": None if self.cash_after is None else str(self.cash_after),
            "equity_after": None if self.equity_after is None else str(self.equity_after),
            "decided_at": self.decided_at,
            "digest": self.digest(),
        }


@dataclass(frozen=True, slots=True)
class LineageReport:
    """Every decision in one journal, plus the isolation facts an operator must see."""

    mode: str
    records: tuple[LineageRecord, ...] = ()
    #: Facts about this run that the operator must not have to infer.
    facts: dict[str, str] = field(default_factory=dict)
    unlinked_orders: tuple[str, ...] = ()
    unlinked_fills: tuple[str, ...] = ()

    @property
    def decisions(self) -> int:
        return len(self.records)

    @property
    def fills(self) -> int:
        return sum(len(record.fills) for record in self.records)

    @property
    def fabrications(self) -> tuple[LineageRecord, ...]:
        """Decisions citing evidence that was never shown to them."""
        return tuple(record for record in self.records if record.evidence_not_shown)

    @property
    def is_clean(self) -> bool:
        """Whether every order and fill traces back to a decision."""
        return not (self.unlinked_orders or self.unlinked_fills or self.fabrications)

    def digest(self) -> str:
        return digest_of(
            {
                "mode": self.mode,
                "records": [record.digest() for record in self.records],
                "unlinked_orders": list(self.unlinked_orders),
            }
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "decisions": self.decisions,
            "fills": self.fills,
            "is_clean": self.is_clean,
            "fabrications": [record.decision_id for record in self.fabrications],
            "unlinked_orders": list(self.unlinked_orders),
            "unlinked_fills": list(self.unlinked_fills),
            "facts": dict(self.facts),
            "lineage": [record.as_record() for record in self.records],
            "digest": self.digest(),
        }

    def summary(self) -> str:
        state = "clean" if self.is_clean else "ATTENTION"
        return (
            f"lineage [{self.mode}]: {self.decisions} decision(s), {self.fills} fill(s), "
            f"{state}"
            + (f", {len(self.fabrications)} fabricated citation(s)" if self.fabrications else "")
            + (f", {len(self.unlinked_orders)} unlinked order(s)" if self.unlinked_orders else "")
        )


@dataclass
class _Scan:
    """Everything the first pass extracts, keyed by id rather than by position.

    A scan rather than a fold: the chains are linked by id, so a journal read back from the
    database in a different order produces the same report.
    """

    snapshots: dict[str, tuple[str, ...]] = field(default_factory=dict)
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    decision_order: list[str] = field(default_factory=list)
    intents: dict[str, str] = field(default_factory=dict)
    intent_payload: dict[str, dict[str, Any]] = field(default_factory=dict)
    risk: dict[str, dict[str, Any]] = field(default_factory=dict)
    order_events: dict[str, list[str]] = field(default_factory=dict)
    broker_ids: dict[str, str] = field(default_factory=dict)
    fills: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    ledger: dict[str, Any] | None = None


def _scan(events: Sequence[EventBase]) -> _Scan:
    """Read every event once, into id-keyed maps."""
    scan = _Scan()
    for event in events:
        kind = type(event).__name__
        payload = _payload(event)

        if kind == "SnapshotCreated":
            scan.snapshots[str(payload.get("snapshot_id", ""))] = tuple(
                payload.get("fact_ids") or ()
            )
        elif kind == "DecisionProposed":
            _scan_decision(scan, event, payload)
        elif kind == "RiskEvaluated":
            intent_id = str(payload.get("intent_id", ""))
            scan.risk[intent_id] = {
                "outcome": str(payload.get("outcome", "")),
                "reason_codes": tuple(str(code) for code in (payload.get("reason_codes") or ())),
                "checks_run": tuple(str(check) for check in (payload.get("checks_run") or ())),
                "policy_hash": str(payload.get("policy_hash", "")),
            }
        elif kind == "OrderIntentCreated":
            intent_id = str(payload.get("intent_id", ""))
            scan.intents[intent_id] = str(payload.get("decision_id", ""))
            scan.intent_payload[intent_id] = dict(payload)
            scan.order_events.setdefault(intent_id, []).append(kind)
        elif kind in _FILL_EVENTS:
            intent_id = str(payload.get("intent_id", ""))
            scan.order_events.setdefault(intent_id, []).append(kind)
            scan.fills.setdefault(intent_id, []).append(
                {
                    "exec_id": str(payload.get("exec_id", "")),
                    "quantity": str(payload.get("quantity", 0)),
                    "price": str(payload.get("price", 0)),
                    "commission": str(payload.get("commission", 0)),
                }
            )
        elif kind in _ORDER_STATES:
            intent_id = str(payload.get("intent_id", ""))
            scan.order_events.setdefault(intent_id, []).append(kind)
            if payload.get("broker_order_id"):
                scan.broker_ids[intent_id] = str(payload["broker_order_id"])
        elif kind == "LedgerMarked":
            scan.ledger = {
                "cash": q_cash(Decimal(str(payload.get("cash", 0)))),
                "equity": q_cash(Decimal(str(payload.get("equity", 0)))),
            }
    return scan


def _scan_decision(scan: _Scan, event: EventBase, payload: dict[str, Any]) -> None:
    decision_id = str(payload.get("decision_id", ""))
    if not decision_id:
        return
    if decision_id not in scan.decisions:
        scan.decision_order.append(decision_id)
    raw_weight = payload.get("target_weight")
    scan.decisions[decision_id] = {
        "snapshot_id": str(payload.get("snapshot_id", "")),
        "model_version": str(payload.get("model_version", "")),
        "prompt_version": str(payload.get("prompt_version", "")),
        "action": str(payload.get("action", "")),
        "instrument": payload.get("instrument"),
        "target_weight": None if raw_weight is None else Decimal(str(raw_weight)),
        "thesis": str(payload.get("thesis", "")),
        "evidence": tuple(payload.get("evidence_ids") or ()),
        "decided_at": event.occurred_at.isoformat(),
    }


def build_lineage(events: Iterable[EventBase], *, mode: str) -> LineageReport:
    """Assemble the lineage of every decision in a journal.

    Events may arrive in any order; the chains are linked by id rather than by position, so a
    journal read back from the database in a different order produces the same report.
    """
    ordered = tuple(events)
    scan = _scan(ordered)

    records: list[LineageRecord] = []
    linked_intents: set[str] = set()
    for decision_id in scan.decision_order:
        record, intent_id = _assemble(decision_id, scan)
        records.append(record)
        if intent_id:
            linked_intents.add(intent_id)

    unlinked_orders = tuple(sorted(set(scan.intents) - linked_intents))
    unlinked_fills = tuple(
        sorted(intent_id for intent_id in scan.fills if intent_id not in linked_intents)
    )

    return LineageReport(
        mode=mode,
        records=tuple(records),
        facts=_isolation_facts(mode, tuple(records), events_seen=len(ordered)),
        unlinked_orders=unlinked_orders,
        unlinked_fills=unlinked_fills,
    )


def _assemble(decision_id: str, scan: _Scan) -> tuple[LineageRecord, str | None]:
    """Build one decision's record from the scan."""
    entry = scan.decisions[decision_id]
    available = scan.snapshots.get(entry["snapshot_id"], ())
    cited = entry["evidence"]
    intent_id = next(
        (candidate for candidate, owner in scan.intents.items() if owner == decision_id), None
    )
    verdict = scan.risk.get(intent_id or "", {})
    ledger = scan.ledger

    record = LineageRecord(
        decision_id=decision_id,
        snapshot_id=entry["snapshot_id"],
        model_version=entry["model_version"],
        prompt_version=entry["prompt_version"],
        action=entry["action"],
        instrument=entry["instrument"],
        target_weight=entry["target_weight"],
        thesis=entry["thesis"],
        evidence_cited=cited,
        evidence_available=available,
        evidence_not_shown=tuple(sorted(set(cited) - set(available))),
        risk_outcome=verdict.get("outcome"),
        risk_reason_codes=verdict.get("reason_codes", ()),
        risk_checks_run=verdict.get("checks_run", ()),
        policy_hash=verdict.get("policy_hash") or None,
        intent_id=intent_id,
        client_order_id=str(scan.intent_payload.get(intent_id or "", {}).get("client_order_id", ""))
        or None,
        broker_order_id=scan.broker_ids.get(intent_id or "") or None,
        order_states=tuple(scan.order_events.get(intent_id or "", ())),
        fills=tuple(scan.fills.get(intent_id or "", ())),
        cash_after=ledger["cash"] if ledger else None,
        equity_after=ledger["equity"] if ledger else None,
        decided_at=entry["decided_at"],
    )
    return record, intent_id


def _isolation_facts(
    mode: str, records: tuple[LineageRecord, ...], *, events_seen: int
) -> dict[str, str]:
    """The facts an operator must see to trust the numbers, stated rather than implied.

    The gate requires isolation to be "shown distinctly". A dashboard that displayed two modes
    side by side without labelling which journal each came from would invite exactly the
    confusion the mode partition exists to prevent.
    """
    return {
        "journal_mode": mode,
        "events_read": str(events_seen),
        "decisions": str(len(records)),
        "reached_the_venue": str(sum(1 for record in records if record.reached_the_venue)),
        "refused_by_risk": str(sum(1 for record in records if record.was_refused)),
        "abstentions": str(sum(1 for record in records if record.action.upper() == "NO_TRADE")),
        "note": (
            "This view is built from ONE journal. Every fact above belongs to mode "
            f"{mode!r} alone; nothing from another mode is merged into it."
        ),
    }


def _payload(event: EventBase) -> dict[str, Any]:
    raw = getattr(event, "payload", None)
    if callable(raw):
        raw = raw()
    return dict(raw or {})


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------
def render_json(report: LineageReport, *, indent: int = 2) -> str:
    """The full report as JSON. The stable form for tooling."""
    return json.dumps(report.as_record(), indent=indent, sort_keys=True)


def render_text(report: LineageReport) -> str:
    """A human-readable lineage, decision by decision."""
    lines = [report.summary(), ""]
    for key, value in sorted(report.facts.items()):
        lines.append(f"  {key}: {value}")
    lines.append("")

    for record in report.records:
        lines.append(f"decision {record.decision_id}  [{record.action}]")
        lines.append(f"  decided_at      {record.decided_at}")
        lines.append(f"  model/prompt    {record.model_version} / {record.prompt_version}")
        lines.append(f"  snapshot        {record.snapshot_id}")
        lines.append(
            f"  evidence        cited {len(record.evidence_cited)}, "
            f"available {len(record.evidence_available)}"
        )
        if record.evidence_not_shown:
            lines.append(
                f"  ** FABRICATED    cited but never shown: {list(record.evidence_not_shown)}"
            )
        if record.risk_outcome is not None:
            reasons = ", ".join(record.risk_reason_codes) or "-"
            lines.append(
                f"  risk            {record.risk_outcome} ({len(record.risk_checks_run)} checks) "
                f"reasons: {reasons}"
            )
        if record.intent_id:
            lines.append(
                f"  order           {record.intent_id} "
                f"client={record.client_order_id} venue={record.broker_order_id or '-'}"
            )
            lines.append(f"  states          {' -> '.join(record.order_states) or '-'}")
        for fill in record.fills:
            lines.append(
                f"  fill            {fill['exec_id']} {fill['quantity']} @ {fill['price']} "
                f"(commission {fill['commission']})"
            )
        if record.cash_after is not None:
            lines.append(f"  ledger after    cash {record.cash_after} equity {record.equity_after}")
        lines.append("")

    if report.unlinked_orders:
        lines.append(f"UNLINKED ORDERS (no decision): {list(report.unlinked_orders)}")
    if report.unlinked_fills:
        lines.append(f"UNLINKED FILLS (no decision): {list(report.unlinked_fills)}")
    return "\n".join(lines)


_CSV_COLUMNS: tuple[str, ...] = (
    "decision_id",
    "decided_at",
    "action",
    "instrument",
    "model_version",
    "snapshot_id",
    "evidence_cited",
    "evidence_available",
    "evidence_not_shown",
    "risk_outcome",
    "risk_reason_codes",
    "intent_id",
    "client_order_id",
    "broker_order_id",
    "order_states",
    "fills",
    "filled_quantity",
    "traded_notional",
    "cash_after",
    "equity_after",
)


def render_csv(report: LineageReport) -> str:
    """One row per decision, for a spreadsheet.

    Values are flattened to text and lists are pipe-joined, so the output opens cleanly in
    Excel rather than producing a nested column nobody can filter on.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(_CSV_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for record in report.records:
        row = record.as_record()
        writer.writerow(
            {
                key: (
                    "|".join(str(item) for item in row[key])
                    if isinstance(row.get(key), list)
                    else ("" if row.get(key) is None else row[key])
                )
                for key in _CSV_COLUMNS
            }
        )
    return buffer.getvalue()


def export(report: LineageReport, fmt: str) -> str:
    """Render in one of the supported formats."""
    renderers = {"json": render_json, "text": render_text, "csv": render_csv}
    try:
        renderer = renderers[fmt]
    except KeyError:
        msg = f"unsupported format {fmt!r}; choose one of {sorted(renderers)}"
        raise ValueError(msg) from None
    return renderer(report)
