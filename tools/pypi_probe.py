"""Read-only PyPI metadata probe used during CR-00 dependency discovery.

Network access is required. No credentials are used and nothing is installed.
The probe is concurrent so a slow/hanging index request cannot stall discovery.

Usage:
    python tools/pypi_probe.py [--json out.json] [package ...]
"""

from __future__ import annotations

import concurrent.futures
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
    "orjson",
    "tenacity",
]


def probe(pkg: str) -> dict[str, object]:
    url = f"https://pypi.org/pypi/{pkg}/json"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.load(resp)
    except Exception as exc:
        return {"package": pkg, "error": f"{type(exc).__name__}: {exc}"}

    version = data["info"]["version"]
    files = data["releases"].get(version, [])
    wheels = sorted({f["filename"] for f in files if f["packagetype"] == "bdist_wheel"})
    cp314_win = [w for w in wheels if "cp314" in w and "win_amd64" in w]
    cp312_win = [w for w in wheels if "cp312" in w and "win_amd64" in w]
    pure = [w for w in wheels if w.endswith("py3-none-any.whl")]
    return {
        "package": pkg,
        "latest": version,
        "requires_python": data["info"].get("requires_python"),
        "cp314_win_wheel": bool(cp314_win),
        "cp312_win_wheel": bool(cp312_win),
        "pure_py3_wheel": bool(pure),
        "sdist": any(f["packagetype"] == "sdist" for f in files),
    }


def main(argv: list[str]) -> int:
    out_json = None
    args = []
    it = iter(argv)
    for arg in it:
        if arg == "--json":
            out_json = next(it, None)
        else:
            args.append(arg)
    targets = args or TARGETS

    results: list[dict[str, object]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for res in pool.map(probe, targets):
            results.append(res)
            if "error" in res:
                print(f"{res['package']:20s} FAILED {res['error']}", flush=True)
            else:
                print(
                    f"{res['package']:20s} latest={res['latest']:14s} "
                    f"py={res['requires_python']!s:14s} "
                    f"cp314win={res['cp314_win_wheel']!s:5s} "
                    f"cp312win={res['cp312_win_wheel']!s:5s} "
                    f"pure={res['pure_py3_wheel']!s:5s} "
                    f"sdist={res['sdist']!s:5s}",
                    flush=True,
                )

    if out_json:
        with open(out_json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, sort_keys=True)
        print(f"wrote {out_json}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
