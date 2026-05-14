# FIX-H — shark LLM debate pipeline revived

**Status:** fixed.
**Pre-fix:** `llm-calls.jsonl` last appended 2026-05-12T22:37 UTC (~64 h idle). Dashboard
courtroom (card 21a) showed all five canonical roles — REGIME_TAGGER, BULL, ARBITER,
BEAR, REFLECTOR — as "idle, no calls in 24 h."
**Post-fix:** ts=2026-05-14T17:52:53 UTC `agent=regime_tagger model=hermes3:8b-trader role=trading-regime-tagger`.
First brand-new line in the JSONL since the outage.

## Root cause (one paragraph)

The pipeline did **not** have a single root failure — it had **three compounding
structural gaps**, each invisible on its own. (1) The `regime_tagger` canonical
role was configured in `stocks/shark/model_tiers.json` (backend=ollama, model=`hermes3:8b-trader`)
but had **zero Python callers** — orphaned config that the dashboard's AgentFlow
courtroom was waiting on forever. (2) `chat_by_role()` set its env override using
`SHARK_<ROLE_WITH_UNDERSCORES>_LLM_MODEL`, but `_resolve_ollama_model()` read
`SHARK_<ROLE_WITH_HYPHENS>_LLM_MODEL` — so EVERY `trading-*` routed role silently
fell through to the generic `hermes3:8b` / `hermes3:70b` instead of the
role-specific model. (3) The `bull/bear/arbiter` debate pipeline IS wired (via
`combined_analyst → run_debate`) but had been **starved** since 2026-05-12 because
the catalyst gate at `phases/market_open.py:382-424` killed every candidate
(NVDA 2026-05-12, GOOGL 2026-05-13, NVDA 2026-05-14) before reaching
`analyze_symbol`. Likewise `trade_reviewer/outcome_resolver/reflector` were
starved because the only open positions since 2026-05-12 are options (CSPs
managed by the wheel, not shark — see `phases/midday.py` "non-equity skipping"
guards). The structural fix is to add **one guaranteed LLM call per phase** via
the never-wired `regime_tagger` role, which makes the LLM heartbeat visible to
the dashboard regardless of whether any candidate passes the catalyst gate or
any equity trade closes.

Ollama (port 11434) was **never down**. `hermes3:70b`, `hermes3:8b`, `hermes3:8b-trader`,
`qwen2.5:72b-instruct`, and `qwen3:30b` were all loaded the whole time. No
circuit breaker state files exist in `/tmp` (CB was not the gate). vLLM (port
8090) IS down, but per `chat_by_role()` semantics any role with `backend=vllm`
silently falls back to Ollama with the base model — vLLM being down only
disables LoRA adapter selection, not the call path.

## Code changes

1. **`stocks/shark/llm/client.py:441-475`** — `_resolve_ollama_model(role, tier)`
   now normalises hyphens to underscores when computing the env-key name. Pre-fix
   it read `SHARK_TRADING-REGIME-TAGGER_LLM_MODEL` (invalid POSIX env name, so
   never set); post-fix it reads `SHARK_TRADING_REGIME_TAGGER_LLM_MODEL` (the
   exact key `chat_by_role()` writes at line 914). This was a quiet bug:
   `chat_by_role(role="trading-regime-tagger")` returned `hermes3:8b` (the
   generic-fast-tier default) instead of `hermes3:8b-trader` (the role-specific
   model), so the tracker logged generic-looking calls and the dashboard's
   per-role courtroom cell couldn't tell role-specific telemetry from generic.

2. **`stocks/shark/data/market_regime.py:1-49`** (header + module-state
   cache) **and `:247-360`** (new `_llm_annotate_regime()` helper + return-dict
   `regime_tag`/`regime_narrative` keys). `detect_regime()` — called by every
   shark phase (pre_market, market_open, midday, pre_execute) — now does a
   best-effort LLM annotation via `chat_by_role("trading-regime-tagger", ...)`
   after the deterministic regime is classified. The LLM is **a commentator,
   never a gatekeeper**: any failure returns `("", "")` and the deterministic
   regime flows through unchanged. A 30-min in-process cache keyed on
   `regime + trend_score + atr_percentile_bucket` keeps Ollama load minimal
   (one call per phase, not one per `detect_regime()` invocation). Operator
   can disable via `SHARK_REGIME_TAGGER_DISABLED=1`.

## Pre-fix evidence

```
$ stat -c '%y' /home/saijayanthai/Documents/.dgx-train/shark/memory/llm-calls.jsonl
2026-05-12 18:37:42.009340254 -0400          # 64 h stale at investigation start

$ wc -l ...
14 entries total

$ tail -1 ...                                  # last call 2026-05-12T22:37 UTC
{"agent":"risk_debate.neutral","model":"hermes3:8b","timestamp":"2026-05-12T22:37:42.009464+00:00",...}

$ ls /tmp/shark-cb-*.json
ls: cannot access '/tmp/shark-cb-*.json': No such file or directory   # CB not the gate

$ curl http://127.0.0.1:11434/api/tags | head -1
{"models":[{"name":"qwen3:30b",...},{"name":"hermes3:8b-trader",...},{"name":"hermes3:70b",...},...]
                                                                       # Ollama healthy
$ curl http://127.0.0.1:8090/v1/models
curl: (7) Failed to connect to 127.0.0.1 port 8090                     # vLLM dead (irrelevant — fallback OK)
```

Probe pre-fix routing: `chat_by_role("trading-regime-tagger", ...)` returned
`model=hermes3:8b` (the generic fast-tier default), NOT `hermes3:8b-trader`
(the per-role config) — proving the hyphen/underscore mismatch was active.

## Post-fix evidence

```
$ PYTHONPATH=stocks python3 -c "
from shark.llm.client import chat_by_role
raw, usage, model = chat_by_role(role='trading-regime-tagger', ...)
print(model, raw[:80])
"
hermes3:8b-trader {"tag":"Bearish Volatile","narrative":"The market is experiencing..."}
                                          # POSTFIX: correct role-specific model selected.

$ PYTHONPATH=stocks python3 -c "from shark.data.market_regime import detect_regime; r=detect_regime(); print(r['regime'],'|tag=',r['regime_tag'],'|narrative=',r['regime_narrative'])"
MarketRegime.BEAR_VOLATILE |tag= Sell Volatile |narrative= The bearish trend
and high ATR suggest a volatile market environment, prompting long-only
investors to sell and wait for calmer conditions.
                                          # detect_regime now produces an LLM tag end-to-end.

$ tail -1 /home/saijayanthai/Documents/.dgx-train/shark/memory/llm-calls.jsonl | python3 -c '...'
ts=2026-05-14T17:52:53.142176+00:00  agent=regime_tagger  model=hermes3:8b-trader
role=trading-regime-tagger  lat=3.274s tok=...
                                          # Brand new line. JSONL is live again.

$ wc -l ...
26 entries total                          # was 14 pre-fix
```

The dashboard's `_AGENT_ROLE_MAP` in `user_data/dashboard/ops_routes.py:4985`
already maps `regime_tagger` → canonical `regime_tagger` role, so the courtroom
UI's REGIME_TAGGER cell will light up on the next refresh (no dashboard change
required).

## Tests

- `tests/test_chat_json_failover.py` — 6/6 passed (covers the env-override
  resolution path that I touched).
- `tests/test_vllm_client.py` — 16/16 passed.
- `tests/test_multi_agent.py::TestRiskDebate::test_no_api_key_skips` failed but
  is a **pre-existing failure**, unrelated to this fix. The test patches
  `ANTHROPIC_API_KEY=""` expecting risk_debate to skip, but the code now uses
  Ollama which works regardless. `git diff HEAD --` confirms I did not touch
  either `tests/test_multi_agent.py` or `shark/agents/risk_debate.py`. Side
  effect: this test run actually exercised the live debate pipeline, adding
  9 brand-new `risk_debate.{aggressive,conservative,neutral}` entries to the
  JSONL (the bull/bear/arbiter equivalents) — proving the broader debate
  pipeline is also healthy when something invokes it.

## Architectural recommendations (prevent recurrence)

1. **Every routed role in `model_tiers.json` MUST have a Python invocation site.**
   Add a test that asserts every key in the `routing` block is reachable from
   at least one `chat_by_role(role=...)` call site. Pre-fix, `trading-regime-tagger`
   was a phantom config — JSON declared a route but no caller existed.

2. **The catalyst gate is doing the right thing operationally but is currently
   the SOLE gate to LLM activity.** Operator should consider either (a) running
   a once-per-phase `regime_tagger` heartbeat (now done in this fix) so the
   dashboard distinguishes "LLM dead" from "LLM alive, nothing to debate";
   (b) periodically (e.g. weekly) running `bull/bear/arbiter` on the watchlist
   even without a trade trigger, so the LoRA adapters get exercised and the
   courtroom UI has a multi-role baseline to compare to. (b) is a Phase 3
   follow-up; (a) is shipping now.

3. **The hyphen/underscore mismatch in `_resolve_ollama_model` was a latent
   bug** that only surfaced because no test asserted that `chat_by_role(role=
   "trading-regime-tagger")` actually uses `hermes3:8b-trader`. Add a parametrised
   test over every `routing` key in `model_tiers.json` that confirms the
   tracker-logged `model` matches the JSON declaration.

4. **vLLM is down right now** (port 8090 connection refused). The router
   correctly falls back to Ollama with the base model, NO adapter. This is by
   design but means trading is currently happening with the un-fine-tuned
   `hermes3:70b` / `qwen3:30b` base. Standing up vLLM is out of scope for
   FIX-H but worth noting in the ops debrief.
