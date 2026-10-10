# TigerQuant — DeepSeek Harness build starter

TigerQuant is an evidence-grounded, paper-first AI trading platform under construction. This
repository contains design documents, agent instructions **and a working offline vertical slice**.
There is no enabled live order path, and none can be created by a coding agent.

## Current status (honest)

| Milestone | State |
|---|---|
| CR-00 architecture, pinning, threat model | **complete** |
| CR-01 offline vertical slice, CI, tests | **complete** — 223 tests, strict typing, deterministic replay |
| CR-02/CR-03 Tiger paper integration | **BLOCKED** — no Tiger paper account exists in this environment |
| CR-04…CR-10 | not started |

**Implementation status: `SIMULATED`.** No external system has been contacted. The paper, engine
and model integrations are pinned and lock-resolved but not yet exercised.

## Try the offline slice

```powershell
$env:UV_CACHE_DIR="$env:TEMP\tq_uvcache"
uv sync --frozen --extra dev
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m tigerquant replay --fixture tests/fixtures/scenarios/trend_up.yaml --journal out/audit.jsonl
.\.venv\Scripts\python.exe -m tigerquant verify-journal out/audit.jsonl
.\.venv\Scripts\python.exe -m tigerquant doctor
```

The replay drives the whole production path offline: deterministic market feed → point-in-time
as-of view → evidence snapshot → decision contract → deterministic sizing → risk gateway → order
intent state machine → fake broker → ledger → append-only hash-chained audit journal. Two runs
produce a byte-identical journal.

## Where to read next

- `AGENTS.md` — binding invariants. Read before changing anything.
- `docs/adr/` — the decisions that are expensive to reverse.
- `docs/THREAT_MODEL.md`, `docs/COMPONENT_MAP.md` — assets, boundaries, component privileges.
- `docs/DEPENDENCIES.md` — pinned versions and how to reproduce the environment.
- `docs/PROGRESS.md` — durable state: what is done, what is verified, what is blocked.
- `docs/WORK_QUEUE.md` — the backlog and its current status.
- `docs/integrations/` — citation-backed vendor API notes for the pinned SDK versions.

## Mission
Build a paper-first, evidence-grounded AI-driven trader on NautilusTrader, Tiger OpenAPI, and a
provider-neutral LLM adapter (starting with `deepseek-flash`). Historical simulation, Tiger paper,
shadow, and eventually explicitly authorized Tiger live operation must be segregated.

## Start with DSH
1. Unzip this folder into a new Git repository and commit the baseline.
2. Install Node.js if needed; run `npx @deepseek-ai/dsh web` from this folder, or use the DSH Desktop application and select the folder as its workspace.
3. Configure DeepSeek in DSH Settings > Models. This is the **coding-agent model**, not a brokerage credential.
4. Paste `prompts/MASTER_DSH_PROMPT.md` into a new DSH session.
5. Ask DSH to create a durable goal. If the installed profile supports automatic goal continuation, enable it explicitly. If unavailable, resume from `docs/PROGRESS.md` and `docs/WORK_QUEUE.md`.
6. Review its milestone deliverables and automated evidence. Start with CR-00 and CR-01. Paper broker tests follow only after offline tests pass.

Do not supply Tiger private keys to DSH. Treat external links, market articles, and broker data as untrusted inputs. Never connect a real-money account in the development environment.

## Primary references
- NautilusTrader custom adapters: https://nautilustrader.io/docs/latest/concepts/adapters/
- Official Tiger Python SDK: https://github.com/tigerfintech/openapi-python-sdk
- Tiger account types: https://quant.itigerup.com/openapi/en/python/operation/trade/accountInfo.html
- Tiger paper vs sandbox: https://quant.itigerup.com/openapi/en/python/FAQ/other.html
- DeepSeek Harness: https://github.com/deepseek-ai/deepseek-harness
- DeepSeek model: https://api-docs.deepseek.com/updates/

## Legal & access
The user is responsible for applicable account terms, taxes, market-data entitlements and trading restrictions. API connectivity does not establish that an instrument, order type or strategy is authorized. All real execution must be separately enabled by the account owner following a documented capital-risk review.
