# Dependency inventory and pinning policy

- Policy: [ADR-0001](adr/ADR-0001-runtime-and-dependency-pinning.md)
- Machine-readable evidence: `docs/evidence/cr00_pypi_probe.json` (PyPI metadata, 2026-10-10)
- Lockfile: `uv.lock` (hash-carrying, resolves every optional extra)
- Verified: 2026-10-10

## Interpreter

| Item | Value |
|---|---|
| Supported range | `>=3.12,<3.15` (intersection with `nautilus_trader`'s own constraint) |
| Development interpreter | CPython **3.14.7** |
| CI matrix | 3.12 / 3.13 / 3.14 on Linux, 3.14 on Windows, plus a full-extras job on 3.12 |

## Direct dependencies (exact pins; no floating production dependency)

### Core runtime — installed by the fast lane

| Package | Pin | Why | cp314 win wheel |
|---|---|---|---|
| `pydantic` | 2.14.0 | typed events, config, contracts | pure Python (+ `pydantic-core` 2.50.0 binary) |
| `jsonschema` | 4.26.0 | validates proposals against `schemas/trade_decision.schema.json` | pure Python |
| `PyYAML` | 6.0.3 | scenario fixtures and config files | yes |
| `tzdata` | 2026.5 | Windows has no system IANA database; `zoneinfo` needs it | pure Python |

Deliberately **not** used: `structlog` (a small stdlib `logging` formatter plus
`core/redaction.py` covers it), `typer`/`click` (stdlib `argparse` is sufficient), `orjson`
(stdlib `json` is deterministic and enough), `tenacity` (order retries must be explicit policy,
never a generic retry decorator), `pandas`/`numpy` (not needed by the core; they arrive with the
engine extra). Fewer dependencies is fewer supply-chain and reproducibility risks.

### Optional extras — each isolated behind a lazy import (ADR-0001 §6)

| Extra | Package | Pin | Used by | Notes |
|---|---|---|---|---|
| `broker` | `tigeropen` | 3.8.0 | CR-02, CR-03 | pure Python; **no broker-side idempotency key** — see `integrations/tigeropen_api_notes.md` §6 |
| `engine` | `nautilus_trader` | 1.231.0 | CR-04 | cp314 win wheel exists (115 MB); pins `msgspec`, `pyarrow`, `pandas<4`, `fsspec==2026.2.0`, `tzdata>=2026.3` |
| `llm` | `litellm` | 1.104.2 | CR-06 | large transitive tree; never required by the offline slice |
| `dashboard` | `fastapi` 0.143.0, `uvicorn` 0.54.0, `jinja2` 3.1.6 | CR-08 | read-only operator surface |
| `dev` | `pytest` 9.1.1, `pytest-asyncio` 1.4.0, `pytest-cov` 7.1.0, `hypothesis` 6.168.5, `ruff` 0.17.0, `mypy` 2.4.0, `types-PyYAML`, `types-jsonschema` 4.26.0.20261006, `pip-audit` 2.10.1, `bandit` 1.9.4, `detect-secrets` 1.5.0 | always installed in CI |

## Reproducing the environment

```powershell
$env:UV_CACHE_DIR="$env:TEMP\tq_uvcache"   # the default uv cache is outside the DSH sandbox
uv sync --frozen --extra dev               # fast lane: core + dev only
uv sync --frozen --extra dev --extra broker --extra engine --extra llm --extra dashboard
```

`--frozen` means the lockfile is authoritative: if `pyproject.toml` and `uv.lock` disagree, the
command fails instead of silently resolving something new.

## Environment constraints discovered (recorded so they are not rediscovered)

| Constraint | Symptom | Workaround |
|---|---|---|
| PowerShell/SChannel TLS is disabled in the sandbox | `curl` and `Invoke-WebRequest` fail with `SEC_E_NO_CREDENTIALS` | use Python's OpenSSL stack: `uv`, `python -m pip`, `tools/pypi_probe.py` |
| `uv`'s default cache is outside the workspace | `Failed to initialize cache … os error 5` | `UV_CACHE_DIR` pointed at a writable directory |
| System temp is not writable for nested directories | pytest's `tmp_path` raises `PermissionError` | `tests/conftest.py` overrides `tmp_path` to a repository-local directory |
| `pip-audit` needs its remote advisory database | cannot run offline | wired into CI; **not** reported as passing here |

## Updating a pin

1. Change the exact version in `pyproject.toml`.
2. `uv lock` and commit the regenerated `uv.lock` in the **same** commit.
3. Re-run the full fast lane plus the `extras` import-compatibility job.
4. For `nautilus_trader`, additionally re-run the CR-04 interface-shape test: the published
   "latest" documentation describes v2, so a wheel bump can change the adapter surface without any
   obvious signal.
5. Record the change and its verification in `docs/PROGRESS.md`.
