# Quanta Core V4 — DESIGN LOCK · 2026-05-12

This document **locks** the V4 design after a full day of agent-driven research +
validator review + operator philosophy alignment. The 17 sibling docs in
`docs/quanta-core-v4/` (v1) and `docs/quanta-core-v4-rev2/` (validator
revisions) are authoritative; this file is the executive summary that all
**build agents** read first.

---

## 1. Operator-locked philosophy

| Decision | Value |
|---|---|
| Goal priority | **Money first**, viral writes itself if honest |
| Trade frequency | 2-3 / week TOTAL across all assets |
| Hold duration | 3-10 days, hard 14-day max |
| Universe | 27 names screening · 1-3 active trades / week |
| Serving stack | **Ollama** (vLLM is OUT per `project_drop_vllm.md`) |
| Debate model | hermes3:70b Q4 for bull/bear/arbiter · 8b for pre-screen |
| Debate budget | 30 seconds (NOT 500ms) |
| LoRA cadence | Weekly Sunday **14:00 ET** (workflow now `0 18 * * 0` UTC) |
| Reflection cadence | Nightly 23:30 ET → `decisions.md` |
| Publishing | Friday 16:00 ET → `docs/weekly/YYYY-WW.md` regardless W/L |
| Week-4 gate | Sharpe > 1.0 + DD < 5% + WR > 50% → live with $5K |

## 2. Three-way ownership contract (HARD)

```
QUANTA-CORE            MODELFORGE             HERMES (Layer 8)
─────────────          ─────────────          ─────────────────
• sample producer      • trainer              • scheduler
• adapter consumer     • Pareto promoter      • cron glue
• Ollama Modelfile     • adapter registry     • Slack alerts
• model registry       • HF Hub mirror        • state-file
• trade execution      • workflow engine        orchestrator
• risk engine          • dashboard :3000
• live + backtest      • mf-api :8000
```

**Rules enforced:**

1. ModelForge **never** imports quanta_core. Reads `~/.dgx-train/`.
2. quanta_core **never** imports ModelForge. Calls REST + reads `champions.json`.
3. Hermes **never** imports `strategy/` or `execution/`. Schedules + state files only.
4. Strategy **never** imports `exchanges/` or `ledger/` directly. `Context` mediates.
5. Backtest = **same Strategy class**, swapped venue + clock. Parity is a TEST.

## 3. Verdict

| Metric | Value |
|---|---|
| Feasibility (doc 08 rev2) | **FEASIBLE** |
| Memory budget | 40-45 GB steady · 85-95 GB peak (within 121 GiB) |
| Build estimate | **5-6 wall-weeks** · ~23 wall-days at 3-parallel dispatch |
| Code reuse from existing | **70%** of audited 5,598 lines |
| Library blockers | 1 (torch cu130 aarch64 community wheel — NGC PyTorch as fallback) |
| Vendor lock-in | Acceptable (Ollama is MIT/GGUF/portable) |

## 4. Critical migration discipline

- **Shadow-mode for 14 days** before any live capital
- Live promotion requires typing **`PROMOTE V4`** in `/ops` (doc 09 hard rule)
- DG-2 gate: 14 consecutive days at <10% backtest-vs-live divergence
- Rollback: `systemctl stop quanta-core` (< 60s)
- Freqtrade fallback preserved for 30 days post-cutover (M8 daily ping)

## 5. What's locked vs. open

**Locked:**
- All 4 operator philosophy decisions (Q1-Q4 + serving stack)
- Three-way ownership contract
- 7 Hermes Layer 8 modules + cadences
- Strategy ABC hooks (`on_candle` mandatory, `on_tick`/`on_fill`/`on_start`/`on_stop`/`train_hook` optional)
- `client_order_id` = SHA256 → UUID5
- One TOML flag `runtime.mode` flips live↔paper

**Open (resolve during build week 1):**
- NGC PyTorch container vs community torch cu130 aarch64 wheel
- Whether to expand ModelForge cron to all 6 tracks (currently only `trading-reflector`)
- Polygon options subscription ($99/mo) vs deferring options data

## 6. Authoritative doc references

| # | Path | Topic |
|---|------|-------|
| 01 | `docs/quanta-core-v4-rev2/01-RESEARCH-MULTI_MODEL_RESIDENCY.md` | Ollama-only serving |
| 02 | `docs/quanta-core-v4-rev2/02-RESEARCH-CONTINUOUS_LORA.md` | Weekly LoRA via Modelfile |
| 03 | `docs/quanta-core-v4/03-RESEARCH-RISK_MONTE_CARLO.md` | CuPy + CUDA Graphs |
| 04 | `docs/quanta-core-v4/04-RESEARCH-EXCHANGE_CONNECTIVITY.md` | alpaca-py + coinbase-advanced-py |
| 05 | `docs/quanta-core-v4-rev2/05-RESEARCH-PARALLEL_AGENTS.md` | 30s deliberate debate |
| 06 | `docs/quanta-core-v4/06-ARCHITECTURE.md` | File tree + module APIs |
| 07 | `docs/quanta-core-v4/07-VALIDATOR_REPORT.md` | Cross-doc validation |
| 08 | `docs/quanta-core-v4-rev2/08-FEASIBILITY.md` | FEASIBLE verdict + measured hardware |
| 09 | `docs/quanta-core-v4/09-RISKS.md` | 35-row risk register + decision gates |
| 10 | `docs/quanta-core-v4/10-CODE_PATTERNS.md` | uv/ruff/mypy/pytest discipline |
| 11 | `docs/quanta-core-v4-rev2/11-HERMES_CRON_LEARNING.md` | Layer 8 scheduler |
| 12 | `docs/quanta-core-v4-rev2/12-WEEKLY_PUBLISHER.md` | Friday Markdown discipline |
| 13 | `docs/quanta-core-v4-rev2/13-MODELFORGE_INTEGRATION.md` | Real mf-api endpoints |

## 7. Build greenlight

**Locked 2026-05-12 ~21:30 ET by operator.** Build proceeds on branch
`feat/v4-build`. 6 parallel build agents fire first wave. Auto-merge into
`feat/v4-build` as each agent lands. Operator reviews tomorrow morning.

Main branch stays at current freqtrade-era state. V4 work is additive to
`quanta_core/`, never touches `user_data/` or `stocks/` (which run live).
