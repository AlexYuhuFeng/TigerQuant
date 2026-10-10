"""Diagnostic script: print a replay journal as readable rows.

Usage: python tools/dump_journal.py <journal.jsonl> [--mode BACKTEST]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    path = Path(argv[0])
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        payload = record.pop("payload", {})
        summary = {
            key: value
            for key, value in payload.items()
            if key
            in {
                "instrument",
                "action",
                "stage",
                "reason_code",
                "detail",
                "outcome",
                "quantity",
                "exec_id",
                "price",
                "side",
                "intent_id",
                "completeness",
                "message",
                "status",
            }
        }
        detail = json.dumps(summary, sort_keys=True)
        print(f"{record['seq']:4d} {record['event_type']:<24s} {record['occurred_at']} {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
