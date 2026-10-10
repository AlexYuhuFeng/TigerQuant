"""Cross-process determinism.

Acceptance criterion AC-01.5 is stronger than "two runs in one process agree": the same
fixture must produce a byte-identical journal from two *fresh interpreters*. That catches
hash-seed-dependent iteration order (`PYTHONHASHSEED` randomisation) and any accidental
dependence on process state.

The tests invoke the installed console entry point in a subprocess, so they also exercise
the CLI path end to end.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = REPO_ROOT / "tests" / "fixtures" / "scenarios"


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    merged["PYTHONHASHSEED"] = "random"
    if env:
        merged.update(env)
    return subprocess.run(  # noqa: S603 - fixed executable, no shell
        [sys.executable, "-m", "tigerquant", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=merged,
        check=False,
    )


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.integration
def test_fresh_interpreters_produce_byte_identical_journals(tmp_path: Path) -> None:
    fixture = SCENARIOS / "trend_up.yaml"
    first = tmp_path / "a.jsonl"
    second = tmp_path / "b.jsonl"

    for target in (first, second):
        result = run_cli("replay", "--fixture", str(fixture), "--journal", str(target), "--quiet")
        assert result.returncode == 0, result.stderr

    assert sha256_file(first) == sha256_file(second)


@pytest.mark.integration
def test_hash_seed_does_not_change_the_journal(tmp_path: Path) -> None:
    """Explicitly run with two different ``PYTHONHASHSEED`` values."""
    fixture = SCENARIOS / "trend_up.yaml"
    outputs = []
    for seed in ("0", "12345"):
        target = tmp_path / f"seed-{seed}.jsonl"
        result = run_cli(
            "replay",
            "--fixture",
            str(fixture),
            "--journal",
            str(target),
            "--quiet",
            env={"PYTHONHASHSEED": seed},
        )
        assert result.returncode == 0, result.stderr
        outputs.append(sha256_file(target))
    assert outputs[0] == outputs[1]


@pytest.mark.integration
def test_journal_written_by_one_process_verifies_in_another(tmp_path: Path) -> None:
    fixture = SCENARIOS / "trend_up.yaml"
    target = tmp_path / "audit.jsonl"
    assert (
        run_cli("replay", "--fixture", str(fixture), "--journal", str(target), "--quiet").returncode
        == 0
    )
    verify = run_cli("verify-journal", str(target))
    assert verify.returncode == 0, verify.stderr
    assert "OK:" in verify.stdout


@pytest.mark.integration
def test_replay_needs_no_network_and_no_broker_sdk(tmp_path: Path) -> None:
    """The fast lane must run with only the core dependencies importable."""
    script = (
        "import socket\n"
        "def boom(*a, **k):\n"
        "    raise AssertionError('network access attempted')\n"
        "socket.socket = boom\n"
        "socket.create_connection = boom\n"
        "from tigerquant.market.fixtures import load_scenario\n"
        "from tigerquant.pipeline.replay import run_replay\n"
        "fixture = load_scenario(r'{fixture}')\n"
        "report = run_replay(fixture)\n"
        "assert report.orders_filled == 1, report\n"
        "assert not any(m.startswith(('tigeropen', 'nautilus_trader', 'litellm')) "
        "for m in __import__('sys').modules), 'an integration SDK was imported'\n"
        "print('OFFLINE-OK')\n"
    ).format(fixture=SCENARIOS / "trend_up.yaml")

    result = subprocess.run(  # noqa: S603 - fixed executable, no shell
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "OFFLINE-OK" in result.stdout
