# ADR-0001 — Runtime, layering and dependency pinning

- Status: Accepted
- Date: 2026-10-10
- CR: CR-00
- Supersedes: none

## Context

TigerQuant must be reproducible across sessions and machines, must run offline for the whole CR-01 slice, and must integrate three third-party surfaces with different release cadences: NautilusTrader (compiled Rust/Cython extension, tight Python-version window), Tiger OpenAPI (`tigeropen`, pure Python), and an LLM gateway (`litellm`). The development host runs Python 3.14.7 with `uv` 0.11.14 available and general HTTPS egress through Python's OpenSSL stack (PowerShell/SChannel egress is broken in this sandbox — see *Consequences*).

Versions observed on PyPI on 2026-10-10 (evidence: `docs/evidence/cr00_pypi_probe.json`):

| Package | Latest | `requires_python` | cp314 win_amd64 wheel |
|---|---|---|---|
| nautilus_trader | 1.231.0 | `>=3.12,<3.15` | yes |
| tigeropen | 3.8.0 | `>=3.8` | pure Python |
| litellm | 1.104.2 | `>=3.10,<3.15` | pure Python |
| pydantic | 2.14.0 | `>=3.10` | pure Python (+ `pydantic-core` binary) |
| jsonschema | 4.26.0 | `>=3.10` | pure Python |
| pandas | 3.0.6 | `>=3.11` | yes |
| numpy | 2.5.3 | `>=3.12` | yes |

## Decision

1. **Runtime**: Python. Supported range `>=3.12,<3.15` (intersection of our own needs and `nautilus_trader`'s constraint). Development and CI pin the *minor* version; the interpreter is recorded in `docs/DEPENDENCIES.md`. The local development interpreter is CPython 3.14.7.
2. **Build backend**: `hatchling`, `src/` layout, one installable distribution `tigerquant` publishing the `tigerquant` console script.
3. **Dependency pinning**: every direct dependency is pinned to an exact `==` version in `pyproject.toml`. Transitive dependencies are frozen in a hash-carrying `uv.lock` committed to the repository and refreshed only through a recorded change. CI installs with `--frozen`.
4. **Optional dependency groups** keep the offline core small and make each integration's blast radius explicit:
   - `dev` — pytest, hypothesis, ruff, mypy, coverage, secret scanning (always installed in CI).
   - `broker` — `tigeropen` (CR-02/CR-03).
   - `engine` — `nautilus_trader` (CR-04; large compiled wheel).
   - `llm` — `litellm` (CR-06; large transitive tree).
   - `dashboard` — FastAPI/uvicorn/Jinja2 (CR-08).
5. **Layering rule**: `core` → `events` → domain services (`market`, `evidence`, `decision`, `risk`, `portfolio`, `execution`) → `adapters/*` → `pipeline`/`cli`. A lower layer must never import a higher one, and no domain module may import `tigeropen`, `nautilus_trader` or `litellm` at module import time. Adapters import domain; domain never imports adapters. This is enforced by an automated import-boundary test, not by convention.
6. **Optional integrations must degrade explicitly.** Any module that needs an optional dependency imports it lazily inside the function that uses it and raises `IntegrationUnavailable` with the extra name. It must never fake the integration.

## Consequences

- The CR-01 slice runs on `core` + `dev` only: fully offline, no vendor SDK required.
- CI can run the fast lane without the ~100 MB `nautilus_trader` wheel and the heavy `litellm` tree; those are exercised in a separate, slower job.
- Python 3.14 keeps working only while all four ecosystems publish cp314 wheels. If a future pin has no cp314 wheel, the fallback is an explicitly recorded interpreter downgrade to 3.13 (still inside the supported range), not a floating dependency.
- **Sandbox constraint (recorded, not a product limitation):** in this environment `curl`/`Invoke-WebRequest` fail with `SEC_E_NO_CREDENTIALS`, so all package installation and PyPI metadata access must go through Python (`python -m pip`, `uv`, `tools/pypi_probe.py`). `uv` additionally needs `UV_CACHE_DIR` pointed at a writable location because the default `%LOCALAPPDATA%\uv\cache` is outside the workspace sandbox. Both are documented in `docs/DEPENDENCIES.md` so a resumed session does not rediscover them.
- No production dependency floats: a compromised or yanked upstream release cannot silently change behaviour between two runs of the same commit.
