"""Full git-history secret scan before publishing anything publicly."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

NEEDLES: dict[str, str] = {}

# The live paper account number, if a secret file exists locally.
account_file = REPO / ".secrets" / "tiger" / "account"
if account_file.is_file():
    account = account_file.read_text(encoding="utf-8").strip()
    NEEDLES["paper account number"] = account
    NEEDLES["paper account suffix"] = account[-6:]

# Anything in the token directory.
for path in (REPO / ".secrets").rglob("*.json"):
    text = path.read_text(encoding="utf-8", errors="replace")
    if text.strip():
        NEEDLES[f"token file {path.name}"] = text.strip()[:60]

# Any key held locally. Read from the secret store rather than written here: a scanner that
# hardcodes the key it is looking for would commit the very thing it exists to find.
_llm_key = REPO / ".secrets" / "llm" / "deepseek_api_key"
if _llm_key.is_file():
    NEEDLES["local LLM API key"] = _llm_key.read_text(encoding="utf-8").strip()

# Generic patterns.
PATTERNS = {
    "generic sk- key": re.compile(r"sk-[A-Za-z0-9]{20,}"),
    # A REAL key block has a long base64 body. A short one is a test fixture, and the
    # difference matters: the first scan flagged this repository's own redaction test, which
    # uses "MIIabc" precisely because it must not be mistaken for a key.
    "private key block": re.compile(r"BEGIN [A-Z ]*PRIVATE KEY-----[A-Za-z0-9+/=\s]{200,}-----END"),
    "aws-style key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "github token": re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    "bearer token literal": re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{30,}"),
}


def history_blobs() -> list[tuple[str, str]]:
    """Every blob in every commit, as (description, text)."""
    revs = subprocess.run(
        ["git", "rev-list", "--all"], cwd=REPO, capture_output=True, text=True, encoding="utf-8"
    ).stdout.split()
    seen: set[str] = set()
    blobs: list[tuple[str, str]] = []
    for rev in revs:
        listing = subprocess.run(
            ["git", "ls-tree", "-r", rev],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
        for line in listing.splitlines():
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            _mode, kind, sha, path = parts
            if kind != "blob" or sha in seen:
                continue
            seen.add(sha)
            content = subprocess.run(
                ["git", "cat-file", "-p", sha],
                cwd=REPO,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).stdout
            blobs.append((f"{sha[:10]}:{path}", content))
    return blobs


def main() -> int:
    blobs = history_blobs()
    print(f"scanned {len(blobs)} distinct blob(s) across all commits and branches")
    print()

    findings: list[str] = []

    for needle, value in NEEDLES.items():
        if not value:
            continue
        hits = [name for name, text in blobs if value in text]
        if hits:
            findings.append(f"{needle}: found in {hits}")

    for label, pattern in PATTERNS.items():
        hits = sorted({name for name, text in blobs if pattern.search(text)})
        if hits:
            findings.append(f"{label}: matched in {hits}")

    # The documented development password is expected and is not a secret; report it as such.
    dev_hits = sum(1 for _n, text in blobs if "tq_local_development_only" in text)
    print(f"documented dev-only DB password appears in {dev_hits} blob(s) (expected, not secret)")
    print()

    if findings:
        print("SECRETS FOUND - DO NOT PUBLISH")
        for finding in findings:
            print("  " + finding)
        return 1

    print("clean: no account number, token, key material or generic key pattern in history")
    return 0


if __name__ == "__main__":
    sys.exit(main())
