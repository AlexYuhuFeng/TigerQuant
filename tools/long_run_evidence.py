"""Produce CR-09's long-run stability evidence from the repository.

**THE GAP THIS CLOSES.** `run_long_sessions` and `write_report` both existed and **neither was called by
anything but tests** -- the sweep in `docs/evidence/dead_code_sweep.txt` found `write_report`, and
looking further showed the whole long-run apparatus is exercised only by tests.

So CR-09's long-run evidence was **not reproducible from the repository.** The apparatus was there, the
report type was there, the function that persists it was there, and nothing ran them. Anyone wanting to
regenerate `cr09_long_run.txt` had to know how to call two functions that no command exposed.

**A TOOL RATHER THAN A FIXTURE**, because this produces EVIDENCE rather than asserting a property: it
belongs beside `tools/db_migrate.py` and `tools/scan_history_for_secrets.py`, run deliberately and
recorded, not run on every commit.

Usage:

    python tools/long_run_evidence.py
    python tools/long_run_evidence.py --sessions 10 --out docs/evidence/cr09_long_run.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tigerquant.pipeline.long_run import LongRunConfig, run_long_sessions, write_report

DEFAULT_OUT = Path("out/cr09_long_run.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sessions", type=int, default=None, help="trading sessions to simulate")
    parser.add_argument("--bars-per-session", type=int, default=None, help="bars in each session")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="where to write the report. Defaults under out/, which is untracked: evidence promoted to "
        "docs/evidence/ should be a deliberate copy rather than an accident of running this.",
    )
    args = parser.parse_args(argv)

    overrides: dict[str, int] = {}
    if args.sessions is not None:
        overrides["sessions"] = args.sessions
    if args.bars_per_session is not None:
        overrides["bars_per_session"] = args.bars_per_session
    config = LongRunConfig(**overrides)  # type: ignore[arg-type]

    print(f"simulating {config.sessions} session(s) x {config.bars_per_session} bars")
    # WORKDIR INSIDE THE WORKSPACE. The default writes a fixture into a temporary directory, and the
    # DSH sandbox denies the OS temp dir -- so this failed with a write error rather than a bad
    # report. A tool that only works outside the environment it is run in is not evidence.
    out_dir = args.out.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    report = run_long_sessions(config, workdir=out_dir)
    written = write_report(report, args.out)

    print(f"  simulated span    {report.simulated_span_hours} hours")
    print(f"  wall seconds      {report.wall_seconds}")
    print(f"  events            {report.events}")
    print(f"  verdict           {'STABLE' if report.ok else 'NOT STABLE'}")
    print(f"  risk denials      {report.risk_denials}")
    print(f"  report            {written}")
    if not report.ok:
        # A non-zero exit rather than a printed line, so this is usable in a check.
        for violation in report.violations:
            print(f"  VIOLATION         {violation}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
