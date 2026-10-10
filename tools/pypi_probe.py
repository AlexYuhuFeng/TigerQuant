"""Read-only PyPI metadata probe used during CR-00 dependency discovery.

Network access is required. No credentials are used and nothing is installed.
Usage: python tools/pypi_probe.py <package> [<package> ...]
"""

from __future__ import annotations

import json
import sys
import urllib.request

TARGETS = [
    "nautilus_trader",
    "tigeropen",
    "litellm",
    "pydantic",
    "jsonschema",
    "pytest",
    "ruff",
    "mypy",
    "pandas",
    "numpy",
    "structlog",
    "typer",
    "pyyaml",
    "httpx",
    "hypothesis",
    "pytest-asyncio",
    "pytest-cov",
    "pip-audit",
    "bandit",
    "detect-secrets",
    "uvicorn",
    "fastapi",
    "jinja2",
]


def probe(pkg: str) -> None:
    url = f"https://pypi.org/pypi/{pkg}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed https host
            data = json.load(resp)
    except Exception as exc:  # pragma: no cover - network diagnostic path
        print(f"{pkg:20s} FAILED {type(exc).__name__}: {exc}")
        return

    version = data["info"]["version"]
    requires_python = data["info"].get("requires_python")
    files = data["releases"].get(version, [])
    wheels = sorted({f["filename"] for f in files if f["packagetype"] == "bdist_wheel"})
    cp314_win = [w for w in wheels if "cp314" in w and "win_amd64" in w]
    pure = [w for w in wheels if w.endswith("py3-none-any.whl")]
    sdist = any(f["packagetype"] == "sdist" for f in files)

    print(f"{pkg:20s} latest={version:14s} requires_python={requires_python}")
    print(f"{'':20s}   cp314-win wheels : {cp314_win[:2] or 'NONE'}")
    print(f"{'':20s}   pure-py3 wheels  : {pure[:2] or 'NONE'}")
    print(f"{'':20s}   sdist available  : {sdist}")


if __name__ == "__main__":
    args = sys.argv[1:] or TARGETS
    for target in args:
        probe(target)
