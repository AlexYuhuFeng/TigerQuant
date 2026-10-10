# ADR-0009 — Model provider neutrality and the tool boundary

- Status: Accepted
- Date: 2026-10-10
- CR: CR-00 (interface in CR-01, DeepSeek/LiteLLM in CR-06)

## Context

PRODUCT_SPEC: *LLM adapter: start with DeepSeek V4.1 Flash model ID `deepseek-flash` but abstract model/provider via model aliases; other providers later without changing strategy interfaces.* ARCHITECTURE: `LiteLLM or validated equivalent`. Acceptance gate 2: *The LLM cannot call, import or access `TradeClient.place_order()` through its tool boundary.* Gate 11: provider outage/latency/invalid output/provider version change never bypasses risk.

## Decision

1. **`ModelService` is the only interface strategies see.** It exposes exactly one operation for decisions:
   ```python
   def propose(self, request: DecisionRequest) -> ModelResponse
   ```
   where `DecisionRequest` contains the frozen evidence snapshot, the portfolio snapshot, the instrument allowlist and the prompt version — and where `ModelResponse` contains raw text, parsed candidate JSON, token/latency/cost telemetry, and the *resolved* provider release string.
2. **Model aliases, not model names, in strategy code.** `config.model.alias` (e.g. `trader-primary`) resolves through a versioned provider registry to `(provider, model_id, settings)` and is recorded in the snapshot. Strategies never name a vendor.
3. **No tools, no function calling, no broker handle.** The decision call is a single completion with a strict JSON contract. There is no tool registry, so "the model cannot call `place_order`" is a structural property, not a prompt instruction. The model service is constructed with no broker client in its object graph, and an automated test walks the object graph reachable from `ModelService` to prove no broker write method is reachable (gate 2).
4. **Output is untrusted until Stage A + Stage B validation pass** (ADR-0006). Parsing is tolerant of prose wrapping (extract the first balanced JSON object), but validation is not.
5. **Provider failure semantics.** Timeout, HTTP error, malformed output, schema violation, or a resolved-release change versus the approved release ⇒ `NO_TRADE` with a reason code, an alert, and **no** fallback to another model for live/paper decisions. A pre-approved alternate provider may only be used after it has separately passed an equivalence gate recorded in `docs/`; automatic transparent fallback is forbidden.
6. **Deterministic offline model for tests.** A `ScriptedModelService` and a `DeterministicHeuristicModel` are provided so the whole pipeline can be tested without network. These are labelled `SIMULATED` and are never enabled in a paper/live deployment profile.
7. **Telemetry.** Every call records prompt version, snapshot digest, resolved release, latency, token counts and cost estimate into the journal, so a decision can be reconstructed and a model change can be benchmarked on a frozen decision corpus.

## Consequences

- Swapping DeepSeek for another provider is a config + provider-module change, provably invisible to strategy code.
- The absence of a tool/function-calling layer removes an entire class of prompt-injection impact: injected text can at worst produce a proposal that fails validation.
- `litellm` is an implementation detail behind one module; CR-01 does not require it, so the offline slice has no heavyweight dependency.
