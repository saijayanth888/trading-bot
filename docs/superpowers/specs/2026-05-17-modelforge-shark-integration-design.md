# ModelForge ↔ Shark Integration — Structural Fix

**Date**: 2026-05-17  
**Status**: Design accepted, implementation in progress  
**Triggering audit**: Discovery on 2026-05-17 that Sunday 14:00 ET ModelForge training
hadn't fired in 5+ weeks and that any trained adapters wouldn't be consumed
even if they did.

## Problem statement

ModelForge is meant to train LoRA adapters for the 6 shark LLM roles
(reflector, bull, bear, arbiter, regime-tagger, indicator-selector). The
trading bot is meant to consume those adapters automatically as they
promote. Today **zero adapters are reaching production** for four
compounding reasons:

| # | Break | Effect |
|---|---|---|
| B1 | All 6 MF tracks pin `base_model=qwen3:30b` (~60 GB fp16) | OOM-kills training on a 121 GB host whose mf-api cgroup limit is 88 GB |
| B2 | mf-api event bus is in-process, no persistence | `run-d4dac705` promotion at 17:57 on 2026-05-13 fell into a container-restart window at 17:45-17:58; the `track.promoted` event was silently dropped |
| B3 | `publish_adapter_to_ollama` action assumes a `.gguf` file exists at the adapter dir; uses Ollama `/api/create` with a `adapters:` field to chain a safetensors adapter onto a Q4 base | The adapter dir holds only safetensors. `convert_lora_to_gguf.py` is absent from the container (all 3 hardcoded paths missing, env vars unset). Action silently returns `status="skipped"`. Also: Ollama docs warn against chaining safetensors adapters onto already-quantized bases ("erratic results"). |
| B4 | Shark agents call `chat_json(tier="fast")` against plain base names | Even if MF published adapters tagged `hermes3:8b-bull-current`, no code path probes for them. `model_tiers.json:llm/` has plain `hermes3:8b` entries. The `routing` block in `model_tiers.json:shark/` does support adapters but routes 4 of 6 trading roles to vLLM which is unreachable / has crashed the GB10 once. |

## Goals (in priority order)

1. **Right-size training** so the worker cannot OOM-kill itself or the host.
2. **Fail-fast invariants** on every long-running boundary — training,
   publish, adapter consumption.
3. **Self-healing event delivery** — a container restart during a
   promotion must not lose the adapter.
4. **Single canonical path** for shark to consume adapters; no parallel
   tier-vs-role-vs-vLLM routing systems competing for the same role.
5. **Reversible** — if any adapter degrades a role, the fallback to the
   base model must be automatic and observable.

## Non-goals

- Keeping qwen3:30b LoRA training on-host. The math doesn't work.
- Keeping vLLM as the serving path. It has crashed this host once; Ollama
  has the proven custom-model pattern (`hermes3:8b-trader`).
- Big-bang refactor of shark agent code. Per-agent migration is welcome
  but must keep tier-tier callers working in parallel.

## Design

### Single base model: `NousResearch/Hermes-3-Llama-3.1-8B`

The HF fp16 source of Ollama's `hermes3:8b`. Production shark already
uses this as the fast-tier model for 7+ agent call sites. An adapter
trained here applies directly to production after merge+requantize.
Memory: 20 GB peak training vs 76 GB free → 56 GB headroom.

Six tracks, one base. Reflector and arbiter currently use larger
production models (qwen3:30b and hermes3:70b); the trade is intentional —
unifying the base lets one adapter slot serve multiple roles and avoids
GPU contention. If higher-capability deep-tier training is desired later,
it lives off-host on a cloud GPU; not on this machine.

### Adapter delivery pipeline

PEFT merge-and-quantize (not safetensors-chain):

```
1. evolution worker downloads NousResearch/Hermes-3-Llama-3.1-8B fp16
2. trains LoRA adapter (rank=16, alpha=32, lr=2e-4)
3. PEFT model.merge_and_unload() → merged fp16 dir
4. ollama create <tag> --quantize q4_K_M -f Modelfile
   where Modelfile FROM <merged_dir>
5. tag = "hermes3:8b-<role>-v<YYYYMMDD>"; alias swung to "hermes3:8b-<role>-current"
```

Step 4 uses `ollama create` subprocess against a fresh Modelfile — the
official supported path. No `/api/create` with `adapters:` field, no
runtime safetensors loading.

### Reliable event delivery

Replace `bus.publish_nowait("track.promoted", ...)` with a write to a
new `champion_promotions_outbox` table (status=pending). The
"Publish Promoted Adapter to Ollama" workflow gets a cron trigger
(`*/2 * * * *`) that drains the outbox by claiming rows under a row-
level lock, calling the publish action, and marking rows
`published` / `failed`. Outcome: a container restart can't lose
promotions; failed publishes are retried automatically next tick.

### Fail-fast invariants

| Boundary | Old behavior | New invariant |
|---|---|---|
| `evolution.start` action | starts on any base, OOMs at weight-load | precheck: refuse if `free_ram_gb < params_B * 2 * 1.3 + 10` |
| Publish workflow | claims `success` when GGUF missing | hard error if expected merged dir is missing |
| Sequential orchestrator (host shell) | declares ✓ on `is_running=false` | only ✓ if `status="success"` AND adapter tag exists in Ollama post-run |
| Shark role lookup | reads plain base name from tier map | `resolve_role_route` probes Ollama for `{base}-{role}-current`; falls back to base with WARN log |

### Shark consumption

Single canonical surface: `chat_by_role(role="trading-bull", ...)`
backed by `resolve_role_route`. Routing block in
`stocks/shark/model_tiers.json` is updated to:

```json
"routing": {
  "trading-reflector":          {"backend": "ollama", "model": "hermes3:8b-reflector-current",          "fallback": "hermes3:8b"},
  "trading-bull":               {"backend": "ollama", "model": "hermes3:8b-bull-current",               "fallback": "hermes3:8b"},
  "trading-bear":               {"backend": "ollama", "model": "hermes3:8b-bear-current",               "fallback": "hermes3:8b"},
  "trading-arbiter":            {"backend": "ollama", "model": "hermes3:8b-arbiter-current",            "fallback": "hermes3:8b"},
  "trading-regime-tagger":      {"backend": "ollama", "model": "hermes3:8b-regime-tagger-current",      "fallback": "hermes3:8b-trader"},
  "trading-indicator-selector": {"backend": "ollama", "model": "hermes3:8b-indicator-selector-current", "fallback": "hermes3:8b-trader"}
}
```

`resolve_role_route` (`client.py:788`):
1. Read the role's routing entry.
2. Probe `GET /api/tags` for the configured `model`. Cache the probe for 60s.
3. If present → return it.
4. If absent → log WARN once, return `fallback`.

Tier-based agents (analyst_bull, analyst_bear, debate_orchestrator,
decision_arbiter, risk_debate) are migrated to `chat_by_role` in this
campaign. Other tier-based agents (market_analyst, trade_reviewer,
sentiment, outcome_resolver) stay on `chat_json(tier="fast")` for now —
they will eventually move but are not gated on adapter training.

## File touchpoints

### model-forge repo (`/home/saijayanthai/Documents/spark/workspace/model-forge`)

- `apps/api/src/agents/actions/evolution_start.py` (or wherever `EvolutionStart` lives) — add RAM precheck
- `apps/api/src/agents/actions/publish_adapter_to_ollama.py` — switch to merge-then-quantize via `ollama create` subprocess
- `apps/api/src/services/automation_engine/seeds.py` — change publish workflow trigger from `event:track.promoted` to `cron:*/2 * * * *` + outbox drain
- `apps/api/src/agents/runner.py` (line ~163) — write to `champion_promotions_outbox` instead of `bus.publish_nowait`
- New migration: `champion_promotions_outbox` table
- `apps/api/Dockerfile` — install `peft`, `transformers`, `sentencepiece` if missing; ensure `ollama` CLI present in container OR open a UNIX socket to the host Ollama daemon
- Tests: `apps/api/tests/test_evolution_start_precheck.py`, `apps/api/tests/test_publish_pipeline.py`

### trading-bot repo (`/home/saijayanthai/Documents/trading-bot`)

- `stocks/shark/model_tiers.json` — rewrite routing block per above
- `stocks/shark/llm/client.py` — add Ollama-tag probe in `resolve_role_route`
- `stocks/shark/agents/analyst_bull.py` — switch from `chat_json(tier="fast")` to `chat_by_role(role="trading-bull")`
- `stocks/shark/agents/analyst_bear.py` — same, `role="trading-bear"`
- `stocks/shark/agents/decision_arbiter.py` — `role="trading-arbiter"`
- `stocks/shark/agents/risk_debate.py` — keep on tier (deep tier doesn't have a hermes3:8b-class adapter yet); explicit comment why
- `stocks/shark/agents/debate_orchestrator.py` — bull/bear/arbiter rounds switch to `chat_by_role`
- Tests: `stocks/shark/tests/test_route_resolution.py` — new file covering routing block + tag probe + fallback

### Runtime DB writes (no source commits)

- PUT `base_model=NousResearch/Hermes-3-Llama-3.1-8B` into all 6 trading-* workflows via `/api/automation/workflows/{id}`
- PUT `enabled=true` on all 6 (they were disabled during last night's failed sequential)

### Orchestrator script

- `/tmp/mf_sequential_training_20260518.sh` — successor to last night's
  script. `wait_idle` now polls `/api/evolve/status` for `status="success"`
  not just `is_running=false`, and verifies the expected Ollama tag exists
  via `curl /api/tags | jq` before marking ✓.

## Order of operations

1. **shark routing + alias probe + tier→role migration** (trading-bot code) — can ship before training works because the fallback path keeps everything working
2. **model-forge precheck + publish merge-and-quantize + outbox** (model-forge code)
3. **Migration**: create outbox table; update workflow seeds
4. **Container rebuild**: mf-api with peft/transformers/ollama-CLI/sentencepiece
5. **DB updates**: PUT new base_model + re-enable workflows
6. **Orchestrator v2**: with hard `status=success` check
7. **Sequential training run** (1 track first, observe, then queue rest)
8. **Verification**: shark E2E call → adapter route resolves → response back
9. **Commit + push** both repos

## Risk register

| Risk | Mitigation |
|---|---|
| Ollama CLI not in mf-api container | Use the Ollama REST API to do `POST /api/create` from a Modelfile pointing at the merged dir. No CLI dependency. |
| Merging on the same machine that runs ollama/shark depletes RAM | Sequential training queue + precheck. Worker exits before next starts. |
| Adapter degrades a role vs base | `chat_by_role` logs WARN on every fallback. Operator can disable a route by deleting its routing entry → automatic fallback. |
| Outbox introduces lag for promotion → ollama-tag | 2-min cron is acceptable (training takes 15-20 min anyway). |
| Tests pass while production drift | Add a smoke test in `stocks/shark/tests/test_route_resolution.py` that hits a live mock Ollama and asserts the tag-probe path resolves correctly. |

## Out of scope

- Training deep-tier (70B) adapters — needs off-host GPU
- vLLM serving path — removed from routing block, code stays for future re-enable
- Adapter quality evaluation — separate concern; MF has `/api/eval/*` for that
- Auto-promotion criteria — keep MF's existing `promote_or_discard` logic untouched

## Success criteria

- Sequential training of trading-reflector + trading-bull + trading-bear
  completes tonight without OOM and without any orchestrator falsely
  reporting success on an errored run.
- After training, `curl /api/tags` shows at least one new
  `hermes3:8b-<role>-current` tag.
- A live shark debate call to `chat_by_role(role="trading-bull")` returns
  successfully and the response comes from the new adapter (verified via
  `data.model` field).
- 290+ trading-bot tests + new contract tests all green.
- model-forge tests for precheck + publish merge-flow green.

---

Designer: main agent (synthesizing scouts H1, H2, H3)
