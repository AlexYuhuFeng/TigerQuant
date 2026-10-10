# TigerQuant — DeepSeek Harness build starter

TigerQuant is a proposed autonomous trading platform. This repository initially contains **design and agent instructions, not a functioning trader**. There is no enabled live order path.

## Mission
Build a paper-first, evidence-grounded AI-driven trader on NautilusTrader, Tiger OpenAPI, and a provider-neutral LLM adapter (starting with `deepseek-flash`). Historical simulation, Tiger paper, shadow, and eventually explicitly authorized Tiger live operation must be segregated.

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
