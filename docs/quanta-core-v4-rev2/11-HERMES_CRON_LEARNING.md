# 11 — Hermes Cron Learning Loop (quanta-core v4 rev2)

> **Status:** design draft, branch `feat/quanta-core-v4-rev2-r11`.
> **Reads first:** `docs/quanta-core-v4/06-ARCHITECTURE.md`, `docs/quanta-core-v4-rev2/02-RESEARCH-CONTINUOUS_LORA.md`,
> `~/.hermes/scripts/`, `~/.hermes/config/gpu_reservation.yaml`, `~/.hermes/cron/jobs.json`.
> **Scope:** the closed-loop scheduler that turns *time* into a *training signal*.
> This module is **not** present in the v4 doc set (v4-06 mentions cron once, in §6
> migration: `stocks/shark/phases/*.py → quanta_core/scripts/phases/*.py` "invoked by
> cron"). Operator surfaced the gap on 2026-05-12: **without Hermes the bot has no
> memory; with Hermes it compounds**.

---

## 1. Why Hermes is the learning heartbeat

A trading bot that only reacts to the candle clock is amnesic. Every fill it
generates is a fact; every fact it forgets is a missed lesson; every lesson it
forgets is a re-paid mistake. Hermes Agent + the 31-job cron registry is the
piece of infrastructure that prevents that forgetting.

Read every cron in `~/.hermes/scripts/` and a pattern emerges — they all fall
into one of four categories of **state mutation**:

| Category | Cadence shape | What it does | Why it matters for V4 |
|---|---|---|---|
| **state capture** | 1–30 min | snapshot live truth into a JSON/DB row | feeds the next training run |
| **lesson apply** | nightly / weekly | read N hours of state + write reflections | turns trades into rules |
| **memory update** | weekly | retrain models, promote adapters, version bump | turns rules into weights |
| **publish** | weekly | author Markdown drops + Slack briefings | turns weights into operator intent |

V4 inherits every one of those categories. The reason this doc exists is that
v4-06 only describes the *modules that get scheduled* (`scripts/phases/*.py`),
not the *scheduler itself*. In rev2 we name the scheduler `quanta_core.hermes`
and treat its modules as first-class architecture, on equal footing with
`quanta_core.live`, `quanta_core.lora`, and `quanta_core.observability`.

The closed loop is:

```
   Monday trade   ─►  state capture (every 15 min)
                      ↓
                      ledger row
                      ↓
   Friday lesson  ◄── reflector (nightly)            ─►  decisions.md append
                      ↓
                      curated dataset                 ─►  modelforge_curate
                      ↓
   Sunday weight ◄── lora_promoter (weekly)          ─►  adapter version bump
                      ↓
                      stable symlink swap             ─►  vLLM hot-reload
                      ↓
   next Monday   ◄── briefer (Mon 08:30)             ─►  dashboard banner
   debate
```

Hermes is the only component that **closes** that loop. Strip it out and V4 is
a fast bot that re-learns the same mistake every day.

---

## 2. Inventory — existing crons in `~/.hermes/scripts/`

Snapshot of `~/.hermes/cron/jobs.json` + `~/.hermes/scripts/*.sh` as of
2026-05-12. The `quanta_core role` column tags each script to the module it
will fold into post-cutover (see §3 + §6).

| # | Script | Cadence (cron) | What it does today | Quanta-core role |
|---|---|---|---|---|
| 1 | `risk_monitor_15min.sh` | `*/15 * * * *` | reads `unified_risk.get_combined_risk_status()`, posts Slack on dd / breaker / stale-data flips, dedupes via `state-snapshots/risk_monitor_last.json` | `quanta_core.hermes.healthcheck` (risk sub-probe) |
| 2 | `daily_pnl_report.sh` | `0 0 * * *` | reads `trade_journal` + `regime_log` for prior ET day, posts P&L card (icon, Δ vs prev day, 4-question framing) | `quanta_core.hermes.briefer` (daily PnL section) |
| 3 | `weekly_evolution_report.sh` | `0 0 * * 0` | EPT champion + leaderboard top-3 from `evolution/<gen>/*.json`, week-over-week Δ via `weekly_evolution_last.json` | `quanta_core.hermes.weekly_publisher` (legacy until ModelForge swap completes) |
| 4 | `sentiment_accuracy_audit.sh` | `0 6 * * *` | joins `trade_journal.sentiment_score` × `pnl` over last 3 days, computes directional accuracy, severity ladder | `quanta_core.hermes.reflector` (sentiment lesson) |
| 5 | `post_mortem_weekly.sh` | `0 1 * * 0` | clusters losses by `(regime, exit_reason)` over last 7 days, top-3 loss buckets, manual-review recommendations | `quanta_core.hermes.post_mortem` |
| 6 | `market_research_30min.sh` | `*/30 * * * *` | cross-source divergence (LLM vs F&G vs Reddit) from `sentiment_log`; posts only when actionable | `quanta_core.hermes.briefer` (intraday divergence sub-probe) |
| 7 | `shark_kb_update.sh` | `30 21 * * 1-5` | `python shark/run.py kb-update` — daily price + earnings refresh, local commit | folded into `quanta_core.data.knowledge_base` cron entry |
| 8 | `shark_kb_refresh.sh` | `0 11 * * 6` | weekly full S&P 500 bar rebuild + pattern regen | folded into `quanta_core.data.knowledge_base` weekly entry |
| 9 | `wheel_snapshot.sh` | `*/1 9-16 * * 1-5` | refreshes `stocks/wheel/state/account_snapshot.json` via `python -m wheel.cli snapshot` | absorbed into `quanta_core.strategy.wheel_csp` heartbeat (Strategy ABC owns its snapshots) |
| 10 | `wheel_candles.sh` | `*/5 9-16 * * 1-5` | refreshes per-(symbol,timeframe) JSON candles from Alpaca for dashboard | absorbed into `quanta_core.data.universe` warm cache |
| 11 | `wheel_sell_csps.sh` | `0 11 * * 1-5` | Fri-morning CSP entry runner — delta 0.25-0.35, DTE 7-10 | absorbed into `quanta_core.strategy.wheel_csp` Strategy decision hook |
| 12 | `wheel_profit_take.sh` | `0 10,14 * * 1-5` | BTC any short put at ≤50% credit | absorbed into `quanta_core.strategy.wheel_csp` exit hook |
| 13 | `wheel_sell_covered_calls.sh` | `0 11 * * 1` | sells 30-Δ CC against assigned shares Mon AM | absorbed into `quanta_core.strategy.wheel_csp` exit hook |
| 14 | `shark_pre_market.sh` | `0 9 * * 1-5` | `shark/run.py pre-market` phase | `quanta_core.scripts.phases.pre_market` (per v4-06 §6) |
| 15 | `shark_market_open.sh` | `35 9 * * 1-5` | `shark/run.py market-open` | `quanta_core.scripts.phases.market_open` |
| 16 | `shark_midday.sh` | `0 13 * * 1-5` | `shark/run.py midday` | `quanta_core.scripts.phases.midday` |
| 17 | `shark_pre_execute.sh` | `30 9 * * 1-5` | `shark/run.py pre-execute` | `quanta_core.scripts.phases.pre_execute` |
| 18 | `shark_daily_summary.sh` | `30 17 * * 1-5` | `shark/run.py daily-summary` | `quanta_core.scripts.phases.daily_summary` |
| 19 | `shark_weekly_review.sh` | `0 10 * * 6` | `shark/run.py weekly-review` | `quanta_core.scripts.phases.weekly_review` |
| 20 | `shark_briefing_alerts.sh` | `15 9 * * 1-5` | parses latest phase block in `DAILY-HANDOFF.md`, Slack alert on BEAR / extreme macro | `quanta_core.hermes.briefer` (BEAR override sub-probe) |
| 21 | `shark_override_verify.sh` | `45 9 * * 1-5` | parses shark_market_open cron output, counts override fires, escalates after 3 stalled runs | `quanta_core.hermes.healthcheck` (override sub-probe) |
| 22 | `ollama_health.sh` | `*/5 * * * *` | `/api/tags` + tiny generate probe → `/tmp/ollama-health.json` | `quanta_core.hermes.healthcheck` (Ollama sub-probe) |
| 23 | `stocks_tft_smoke.sh` | `30 8 * * 1-5` | daily TFT inference smoke (SPY/NVDA/SOFI) before open | `quanta_core.hermes.healthcheck` (model sub-probe) |
| 24 | `stocks_ml_train.sh` | `0 23 * * 0` | weekly TFT training, detached worker, status JSON | `quanta_core.hermes.lora_promoter` (stocks TFT track) |
| 25 | `nightly_reflector.sh` | `0 21 * * *` | runs `scripts/nightly_reflector.py` — per-trade 2–4 sentence post-mortem to `stocks/memory/decisions.md` via Qwen3-30B | `quanta_core.hermes.reflector` (primary) |
| 26 | `modelforge_ingest.sh` | `30 21 * * *` | pulls reflections + LLM-calls into `~/.dgx-train/raw/<role>/*.jsonl` | `quanta_core.hermes.lora_promoter` (ingest stage) |
| 27 | `modelforge_curate.sh` | `0 22 * * *` | filters + transforms raw JSONL → HF Arrow curated set | `quanta_core.hermes.lora_promoter` (curate stage) |
| 28 | `resample_4h.sh` | `5 */4 * * *` | Coinbase 1h→4h resampler for NFI X6 informative pair | `quanta_core.data.universe` (timeframe synthesiser) |
| 29 | `refresh_sentiment.sh` | `*/30 9-16 * * 1-5` | per-ticker StockTwits/Reddit/Yahoo sentiment refresh | folded into `quanta_core.data.knowledge_base` warm cache |
| 30 | `rebalance_capital.sh` | `every 20160m` (14d) | runs `scripts/rebalance_capital.py` — crypto/stocks split rebalance | unchanged — script-level only |
| 31 | `llm_log_rotate.sh` | `0 3 * * *` (host crontab) | gzip-rotates `stocks/memory/llm-calls.jsonl` at >50 MB / >30 d | unchanged — operations |
| — | `gpu_gate.sh` | (library, not cron) | reads `gpu_reservation.yaml`; exit-code contract for any caller | `quanta_core.hermes.gpu_yield` library |
| — | `gpu_yield_now.sh` | `55 13 * * 0` | evicts Ollama models 5 min before LoRA training window | `quanta_core.hermes.gpu_yield` |
| — | `gpu_resume.sh` | end-of-window | clears yield state, pre-warms `hermes3:8b` | `quanta_core.hermes.gpu_yield` |
| — | `ept_training_daily.sh` | `0 2 * * *` (paused) | RETIRED 2026-05-12 — mock-mode loop; succeeded by ModelForge | dropped |
| — | `ept_eval_breeding.sh` | every 2160 min (paused) | RETIRED 2026-05-12 — depends on retired EPT loop | dropped |

That is the universe. 31 scheduled jobs, 3 GPU-coordination helpers, 2 retired.
Almost every learning surface the operator actually uses is in this table.

---

## 3. Quanta-core `quanta_core.hermes.*` modules (new)

Seven modules. Each is a single Python file under `quanta_core/hermes/<name>.py`
with a `def run() -> int` entry point so `python -m quanta_core.hermes.<name>`
is the only invocation contract. All shell wrappers in §6 then become 3-line
trampolines.

| Module | Cadence (cron) | Purpose | Output |
|---|---|---|---|
| `quanta_core.hermes.reflector` | `0 23 * * 1-5` (23:00 ET, weekday) | Read closed trades from `ledger.trades` for the just-ended trading day, ask `hermes3:8b` (resident) for 2–4 sentence per-trade post-mortems, append to `stocks/memory/decisions.md` | append `decisions.md` + `~/.quanta/state/last_reflection.json` |
| `quanta_core.hermes.lora_promoter` | `0 14 * * 0` (Sun 14:00 ET) | Train the week's role LoRAs against the curated set, run Pareto promotion gates (see rev2-02 §1.2 state machine), swap the `-current` symlink on champion change, version-bump `~/data/lora-adapters/<role>/stable/`. Held off until `gpu_gate.sh acquire modelforge-weekly-lora-training` succeeds | adapter version bump + `~/.quanta/state/last_lora_promotion.json` |
| `quanta_core.hermes.weekly_publisher` | `0 16 * * 5` (Fri 16:00 ET, after market close) | Generate `docs/weekly/YYYY-WW.md` from the week's `ledger.trades`, `decisions.md` deltas, `regime_log` mix, and adapter promotion log. Markdown drop is the public artefact | `docs/weekly/<YYYY-WW>.md` + `~/.quanta/state/weekly_publish_state.json` |
| `quanta_core.hermes.briefer` | `30 8 * * 1` (Mon 08:30 ET, pre-market) | Pre-market briefing: regime forecast for the week, top-3 trades from prior week, open positions, calendar of earnings + macro events from `quanta_core.data.calendar`. Pushes to dashboard banner + Slack | dashboard banner JSON + Slack post |
| `quanta_core.hermes.post_mortem` | `0 10 * * 6` (Sat 10:00 ET) | Weekly cluster analysis: losses by `(regime, exit_reason)`, top-3 loss buckets, manual-review recommendations. NO auto-applies | `decisions.md` append + Slack |
| `quanta_core.hermes.healthcheck` | `*/15 * * * *` | Probe Ollama (`/api/tags` + tiny generate), Postgres (`SELECT 1`), Alpaca (`/v2/account`), Coinbase (`/api/v3/brokerage/accounts`). Slack on fail. Aggregates the legacy `ollama_health` + `risk_monitor_15min` + `shark_override_verify` + `stocks_tft_smoke` sub-probes behind one entry point | `~/.quanta/state/healthcheck_last.json` |
| `quanta_core.hermes.gpu_yield` | `55 13 * * 0` (Sun 13:55 ET) + end-of-window resume | Wraps the existing `gpu_yield_now.sh` + `gpu_resume.sh` → Python; same contract on `gpu_reservation.yaml`. Already exists in shell, V4 brings it into the package so test coverage applies | unchanged Slack + `~/.hermes/state-snapshots/gpu_yielded_at.ts` |

A few **non-goals** that are deliberately *not* in the table:

- **No fold of `stocks_ml_train`** into `lora_promoter`. The stocks TFT trains
  independently and has its own status file; `lora_promoter` only touches the
  prose LoRA adapters (reflector / bull / bear / arbiter, per rev2-02 §1.1).
  These run in disjoint GPU windows.
- **No fold of `wheel_*` crons** into Hermes modules. Per v4-06 §6 the wheel
  becomes a Strategy class on the live event loop; once that lands, wheel
  scheduling collapses into `live.engine`'s tick clock and the 6 wheel crons
  go away entirely.
- **No "weekly retraining ladder"**. The cadence is `lora_promoter` Sun 14:00,
  full stop. Sub-week retraining ladders are deferred to a future research
  doc; today's evidence (rev2-02) is that a single weekly Pareto-gated
  promotion is sufficient.

### 3.1 What each module imports

The dependency rules from v4-06 §7 apply unchanged. `quanta_core.hermes` sits
at **Layer 8** (alongside `live/` and `backtest/`) — it depends on everything
below and nothing above. Specifically:

- `reflector` → `ledger.postgres`, `models.registry`, `agents.reflector` (the agent, not the cron)
- `lora_promoter` → `lora.online`, `models.registry`, `ledger.postgres`
- `weekly_publisher` → `ledger.postgres`, `observability.metrics`
- `briefer` → `data.calendar`, `data.universe`, `models.registry`, `ledger.postgres`
- `post_mortem` → `ledger.postgres`
- `healthcheck` → `exchanges.alpaca`, `exchanges.coinbase`, `ledger.postgres`, `models.registry`
- `gpu_yield` → no quanta-core deps (it operates *on* `models.registry` from outside)

Critically: **no `hermes.*` module imports `strategy/` or `execution/`**. Crons
read the ledger; they never re-decide and never re-execute.

---

## 4. The closed-loop diagram — Mon → Sun walkthrough

The point of the loop is that **a lesson learned on Monday changes the
adapter Sunday, which changes the debate Monday-next**. Concrete trace:

```
MON 08:30 ET    quanta_core.hermes.briefer
                ├─ pulls calendar.this_week() → earnings/FOMC/CPI dates
                ├─ pulls last week's docs/weekly/YYYY-WW.md
                ├─ pulls open positions from ledger.positions
                └─ writes dashboard_banner.json + Slack pre-market post
                                ▼
MON 09:30-16:00 ET   live.engine fires (candle-driven)
                ├─ on each candle: Strategy.on_candle()
                │    └─ ctx.predict("arbiter-current", payload) ─┐
                │                                                │
                │   ◄── adapter loaded from last Sunday's promotion
                ├─ OrderProposal → execution.engine → fill
                └─ fill → ledger.trades  (state capture)
                                ▼
MON 23:00 ET    quanta_core.hermes.reflector
                ├─ SELECT * FROM ledger.trades WHERE closed_today
                ├─ for each: prompt hermes3:8b "what would you have done diff?"
                ├─ append per-trade 2-4 sentences to stocks/memory/decisions.md
                └─ write ~/.quanta/state/last_reflection.json (count + summary)
                                ▼
MON 23:30 ET    modelforge_ingest  (legacy shell, replaced post-cutover)
                └─ ingest reflections + llm-calls → ~/.dgx-train/raw/<role>/MON.jsonl
                                ▼
MON 22:00 ET    modelforge_curate  (legacy shell, replaced post-cutover)
                └─ filter + transform → ~/.dgx-train/datasets/<role>/curated/
                                ▼
TUE … FRI      same loop, each day appending decisions.md + curated rows
                                ▼
FRI 16:00 ET   quanta_core.hermes.weekly_publisher
                ├─ ledger.trades over the week
                ├─ decisions.md deltas since last Friday
                ├─ regime_log mix
                ├─ adapter version log (which adapter served which trade)
                └─ writes docs/weekly/YYYY-WW.md  (public artefact)
                                ▼
SAT 10:00 ET   quanta_core.hermes.post_mortem
                ├─ cluster losses by (regime, exit_reason) over 7 days
                ├─ top-3 buckets
                └─ append recommendations to decisions.md (review-only)
                                ▼
SUN 13:55 ET   quanta_core.hermes.gpu_yield
                ├─ list /api/ps resident models
                ├─ keep_alive=0 + empty prompt to evict each
                ├─ retry via `ollama stop` on stragglers
                └─ Slack ":zzz: GPU yielded — ModelForge training begins"
                                ▼
SUN 14:00 ET   quanta_core.hermes.lora_promoter
                ├─ acquire gpu_gate (caller=modelforge-weekly-lora-training)
                ├─ for role in [reflector, bull, bear, arbiter]:
                │    ├─ TRL DPOTrainer + PEFT LoRA over the week's curated rows
                │    ├─ produce shadow adapter at ./data/lora-adapters/<role>/shadow-<ts>/
                │    ├─ Pareto-gate (faithfulness, hit-rate, latency)  ─── rev2-02 §1.2
                │    ├─ if PASS:
                │    │     swap <role>-current symlink → shadow path
                │    │     vLLM POST /v1/load_lora_adapter (load_inplace=True)
                │    │     bump stable/ version
                │    └─ else: leave champion, keep shadow for next iteration
                ├─ release gpu_gate
                └─ write ~/.quanta/state/last_lora_promotion.json
                                ▼
SUN 18:00 ET   gpu_gate window closes → quanta_core.hermes.gpu_yield (resume mode)
                └─ pre-warm hermes3:8b
                                ▼
MON 08:30 ET   briefer fires AGAIN — but ctx.predict() now resolves to the
                NEW arbiter adapter that was Pareto-promoted Sunday
                (closed loop)
```

The key invariant: **the adapter that decides Monday's first trade saw last
Monday's loss in its training set**. If reflector skips, that lesson never
makes it into the curated set; if lora_promoter skips, the lesson never makes
it into the weights. Both must run for the loop to close.

---

## 5. State files — write spec

All state lives under `~/.quanta/state/` (Hermes Agent's existing
`~/.hermes/state-snapshots/` is preserved for shell-era compatibility; the
Python modules write to `~/.quanta/state/` to keep namespaces clean).

### 5.1 Atomic write contract

Every state file is written via the same idiom — borrowed from
`shark_override_verify.sh`'s `STATE_PATH.write_text(...)` pattern but
strengthened with `os.replace`:

```python
def write_state_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, path)   # POSIX atomic rename
```

Why: a healthcheck cron that crashes mid-write leaves the previous run's state
intact, not a zero-byte file the next run can't parse. `os.replace` is atomic
on POSIX (same-filesystem move).

### 5.2 File schemas

#### `~/.quanta/state/last_reflection.json`
```json
{
  "ts": "2026-05-12T23:00:14Z",
  "trading_day": "2026-05-12",
  "trades_reviewed": 4,
  "summary": "1 winner, 3 losers; common thread = mean-rev entry into BEAR regime",
  "decisions_md_lines_appended": 9,
  "model": "hermes3:8b",
  "duration_seconds": 41.2
}
```

#### `~/.quanta/state/last_lora_promotion.json`
```json
{
  "ts": "2026-05-10T15:31:08Z",
  "training_window": ["2026-05-04", "2026-05-10"],
  "rows_per_role": {"reflector": 87, "bull": 142, "bear": 138, "arbiter": 142},
  "promotions": [
    {
      "role": "arbiter",
      "from": "shadow-20260510T150221",
      "to": "stable/v17",
      "pareto_pass": true,
      "metrics": {"faithfulness": 0.91, "hit_rate": 0.62, "latency_ms": 312}
    },
    {
      "role": "reflector",
      "pareto_pass": false,
      "metrics_delta": {"latency_ms": "+0.6σ (regression)"},
      "kept_champion": "stable/v9"
    }
  ],
  "gpu_window_seconds": 5430
}
```

#### `~/.quanta/state/weekly_publish_state.json`
```json
{
  "ts": "2026-05-09T16:00:42Z",
  "iso_week": "2026-W19",
  "markdown_path": "docs/weekly/2026-W19.md",
  "trades_in_week": 17,
  "pnl_week": -42.18,
  "adapter_changes": ["arbiter: v16→v17", "bear: v11 (unchanged)"],
  "regime_mix": {"BEAR_VOLATILE": 12, "BULL_QUIET": 3, "unknown": 2},
  "slack_status": 200
}
```

#### `~/.quanta/state/healthcheck_last.json`
```json
{
  "ts": "2026-05-12T16:30:01Z",
  "ollama": {"ok": true, "latency_ms": 38, "resident_models": ["hermes3:8b"]},
  "postgres": {"ok": true, "latency_ms": 6},
  "alpaca": {"ok": true, "latency_ms": 220, "account_status": "ACTIVE"},
  "coinbase": {"ok": true, "latency_ms": 410},
  "tft_smoke": {"ok": true, "tickers_checked": ["SPY", "NVDA", "SOFI"]},
  "any_failure": false,
  "consecutive_failures": 0
}
```

### 5.3 Read contract (for the dashboard)

`quanta_core.observability.dashboard` reads these files only — never writes —
and renders them as cards on the SPA. The dashboard is a *consumer* of state;
the crons are the *producers*. This keeps the read path purely synchronous
file-IO with no cross-service coupling.

---

## 6. Migration path — shell → Python

### 6.1 Phase 0 — V4 ships, V3 still owns the schedule (today)

Every `~/.hermes/scripts/*.sh` file stays as-is. V4 boots in **shadow mode**:
`quanta_core.hermes.*` modules are written and tested *but not yet wired to
cron*. The legacy shell wrappers continue to fire on their existing crons in
`~/.hermes/cron/jobs.json`.

This buys the operator a continuous-running V3 while V4 is validated against
the same database.

### 6.2 Phase 1 — Parity test (per script)

For each `<script>.sh` in §2 with a `quanta_core role`:

1. **Run both in parallel for one full cadence cycle.**
   - Shell version writes to its existing log + Slack channel.
   - Python version writes to a `*-quanta` mirror log + a *separate* Slack
     channel (`#hermes-parity` rather than `#hermes`).
2. **Diff the outputs.** Each module ships a `tests/parity/test_<name>.py`
   that asserts:
   - State file JSON is byte-equivalent (excluding `ts` field).
   - Slack message body is character-equivalent (excluding timestamps).
3. **Promote** by flipping the cron's `script:` field from
   `<name>.sh` → `<name>_quanta.sh` (a 3-line trampoline that calls
   `python -m quanta_core.hermes.<name>`).

The 3-line trampoline is intentional — it preserves Hermes Agent's existing
script-discovery + delivery + `no_agent=true` machinery, which is well-tested
infrastructure that V4 has no reason to re-implement.

Example trampoline (`~/.hermes/scripts/nightly_reflector_quanta.sh`):

```bash
#!/usr/bin/env bash
set -uo pipefail
cd /home/saijayanthai/Documents/trading-bot
exec python -m quanta_core.hermes.reflector "$@"
```

### 6.3 Phase 2 — Cutover (per script)

Once parity holds for 7 consecutive runs (≥1 week for daily, ≥4 weeks for
weekly): rename the legacy `.sh` to `<name>.sh.legacy` and delete from
`~/.hermes/cron/jobs.json`. The Python module owns the schedule.

### 6.4 Per-script disposition

The table in §2 already names the target module. The full migration matrix:

| Shell script | Phase 0 (now) | Phase 1 (parity) | Phase 2 (cutover) |
|---|---|---|---|
| `nightly_reflector.sh` | keep | `_quanta.sh` trampoline → `hermes.reflector` | legacy → `.legacy` |
| `post_mortem_weekly.sh` | keep | → `hermes.post_mortem` | legacy → `.legacy` |
| `daily_pnl_report.sh` | keep | → `hermes.briefer` (daily-pnl section) | legacy → `.legacy` |
| `market_research_30min.sh` | keep | → `hermes.briefer` (intraday-divergence section) | legacy → `.legacy` |
| `risk_monitor_15min.sh` | keep | → `hermes.healthcheck` (risk sub-probe) | legacy → `.legacy` |
| `ollama_health.sh` | keep | → `hermes.healthcheck` (ollama sub-probe) | legacy → `.legacy` |
| `stocks_tft_smoke.sh` | keep | → `hermes.healthcheck` (tft sub-probe) | legacy → `.legacy` |
| `shark_override_verify.sh` | keep | → `hermes.healthcheck` (override sub-probe) | legacy → `.legacy` |
| `stocks_ml_train.sh` | keep | → `hermes.lora_promoter` (stocks-tft track) | legacy → `.legacy` |
| `modelforge_ingest.sh` | keep | → `hermes.lora_promoter` (ingest stage) | legacy → `.legacy` |
| `modelforge_curate.sh` | keep | → `hermes.lora_promoter` (curate stage) | legacy → `.legacy` |
| `weekly_evolution_report.sh` | keep | → `hermes.weekly_publisher` | legacy → `.legacy` |
| `sentiment_accuracy_audit.sh` | keep | → `hermes.reflector` (sentiment lesson) | legacy → `.legacy` |
| `shark_briefing_alerts.sh` | keep | → `hermes.briefer` (BEAR override sub-probe) | legacy → `.legacy` |
| `shark_pre_market.sh` ... `shark_weekly_review.sh` (6 scripts) | keep | → `scripts/phases/*.py` per v4-06 §6 | legacy → `.legacy` |
| `wheel_*.sh` (5 scripts) | keep | folded into `strategy.wheel_csp` Strategy hooks | legacy → `.legacy` |
| `refresh_sentiment.sh` | keep | folded into `data.knowledge_base` cron | legacy → `.legacy` |
| `shark_kb_update.sh`, `shark_kb_refresh.sh` | keep | folded into `data.knowledge_base` cron | legacy → `.legacy` |
| `resample_4h.sh` | keep | folded into `data.universe` timeframe synth | legacy → `.legacy` |
| `rebalance_capital.sh` | keep | **unchanged** — script-level operations | keep |
| `llm_log_rotate.sh` | keep | **unchanged** — operations | keep |
| `gpu_gate.sh` + `gpu_yield_now.sh` + `gpu_resume.sh` | keep | → `hermes.gpu_yield` (Python library, shell remains as CLI) | keep CLI |

### 6.5 Parity test format

Each module has a paired test file `tests/parity/test_<module>.py` that:

1. Captures the shell script's last 7 runs (Slack mirror + state file).
2. Runs the Python module against the same database snapshot.
3. Asserts the outputs match modulo timestamp + UUID fields.

Failure of the parity test **blocks promotion to Phase 2**. The shell version
keeps running until the diff is fixed. The operator's standing rule —
*reviews before pushing* — applies: no auto-cutover.

---

## 7. Failure modes

Each module has a documented failure → detection → alert → rollback path. The
governing principle is **fail-open on cron infrastructure, fail-loud on data**:
a crashed cron is a problem for tomorrow; a silent wrong number is a problem
forever.

### 7.1 `reflector` skipped (no run for a day)

- **Detection:** `healthcheck` reads `~/.quanta/state/last_reflection.json` →
  if `ts < now() - 26h` (margin over the 24h cadence), set
  `reflector_stale=true` in its output.
- **Alert:** `:warning: *[healthcheck]* reflector last ran 41h ago — no
  lessons added to decisions.md`. Posts on first detection then **once per
  24h** until resolved (not every 15min — that would spam).
- **Rollback:** none needed; next-night reflector picks up the previous day's
  trades on its own (the trade query is `closed_at >= now() - 1d`, missed
  rows are simply lost). Operator can manually run
  `python -m quanta_core.hermes.reflector --backfill 3d` to recover up to
  3 days of missed lessons.
- **Cost of one miss:** ~5 trades × 4 sentences each = ~20 lines of
  `decisions.md` lost. Adapter quality next Sunday degrades by an unknown
  amount; this is *the* reason the cron must be monitored.

### 7.2 `lora_promoter` fails on Sunday

Three sub-modes:

#### 7.2a — GPU gate refuses

- **Cause:** `gpu_gate.sh acquire modelforge-weekly-lora-training` returns
  `2` (config parse error) or `1` (another holder).
- **Detection:** `lora_promoter` exits with code 2; `healthcheck` reads
  `~/.quanta/state/last_lora_promotion.json` → if `ts < now() - 8d`, alert.
- **Alert:** `:rotating_light: *[lora_promoter]* GPU gate blocked for 8+
  days — no adapter promotions in 2 weeks`.
- **Rollback:** automatic. The previous champion adapter remains symlinked.
  `vLLM` never reloads. **Decision quality holds steady at last-good** — this
  is by design (rev2-02 §1.2 "stable" state).

#### 7.2b — Training crashes mid-run

- **Cause:** OOM, CUDA error, dataset schema drift.
- **Detection:** module catches the exception, writes
  `last_lora_promotion.json` with `error: "..."` and `promotions: []`.
- **Alert:** `:rotating_light:` immediate.
- **Rollback:** automatic. Shadow adapter directory is left in-place for
  forensic inspection; champion symlink untouched.
- **Operator action:** review error, fix, rerun manually via
  `python -m quanta_core.hermes.lora_promoter --force`.

#### 7.2c — Pareto gate fails

- **Cause:** the new adapter regressed on ≥1 metric by >0.5σ.
- **Detection:** module logs `kept_champion: stable/vN` for that role in
  `last_lora_promotion.json`. **This is not a failure — it's the gate
  working.**
- **Alert:** `:bell:` informational only. Slack body lists which roles
  promoted and which were kept.
- **Rollback:** nothing to roll back.

### 7.3 `weekly_publisher` fails

- **Detection:** Missing `docs/weekly/YYYY-WW.md` for last week → grep cron at
  Mon 08:00 ET (just before `briefer`) and re-emit if missing.
- **Alert:** `:warning: weekly publish missed for week YYYY-WW — regenerating`.
- **Rollback:** none — the regenerator pulls from the same ledger so the
  artefact is reconstructible. Markdown files are checked into git so any
  divergence is caught at commit time.

### 7.4 `briefer` fails

- **Detection:** dashboard banner JSON timestamp older than 24h on Monday
  morning → banner card shows red.
- **Alert:** `:warning:` to Slack; no automated retry (operator-time-sensitive).
- **Rollback:** prior week's banner stays visible; operator can run manually.

### 7.5 `post_mortem` fails

- **Detection:** `decisions.md` has no Saturday-dated append entry.
- **Alert:** `:bell:` informational at end-of-day Saturday.
- **Rollback:** none — recommendation-only, no state mutated.

### 7.6 `healthcheck` itself fails

- **Detection:** missing `~/.quanta/state/healthcheck_last.json` for >30 min →
  *this* is what cascade-monitors: a Hermes-Agent-level cron-job-success
  metric. If healthcheck has been dead for >30 min the **operator** is
  notified, not the bot (the bot has lost its own watchdog).
- **Alert:** Hermes Agent's built-in `last_run_at` tracking is the canary;
  Hermes already alerts on cron last-run staleness via its own daemon.
- **Rollback:** restart Hermes Agent.

### 7.7 `gpu_yield` fails to evict

- **Already handled by `gpu_yield_now.sh`:** exit code 1 + Slack
  `:warning: GPU yield partial — could not evict <model>`. The training cron
  will still try to acquire; if VRAM is insufficient, training crashes → falls
  under 7.2b.

### 7.8 Catastrophic — multiple modules miss for >1 week

Operator-only state. Detection is manual via dashboard "Hermes Loop Health"
card (shows last-run-age for each of the 7 modules). Above 7 days on
`reflector` or `lora_promoter` the dashboard turns red and the bot is **not
compounding** — V4 is in pure-reaction mode. The fix is operator intervention,
not automation; the loop being broken is itself the alarm.

---

## 8. Build cost

Approximately **3 dev-days** for the 7 modules + parity tests + 21 shell
trampolines. Breakdown:

| Module | Effort | Notes |
|---|---|---|
| `quanta_core.hermes.reflector` | 0.5 day | port `scripts/nightly_reflector.py` core into module form; existing logic is ~200 lines |
| `quanta_core.hermes.lora_promoter` | 1.0 day | the heaviest — wires `lora.online`, gpu_gate, Pareto gate, symlink swap, vLLM `/v1/load_lora_adapter`. Largely new code |
| `quanta_core.hermes.weekly_publisher` | 0.5 day | Markdown templating + ledger queries; mostly Jinja2 + SQL |
| `quanta_core.hermes.briefer` | 0.5 day | calendar + last-week summary + dashboard JSON write |
| `quanta_core.hermes.post_mortem` | 0.25 day | direct port of `post_mortem_weekly.sh`'s embedded Python |
| `quanta_core.hermes.healthcheck` | 0.25 day | aggregates 4 existing probes |
| `quanta_core.hermes.gpu_yield` | 0 (skip) | keep shell scripts; just wrap the CLI in a Python `subprocess.run` for tests |
| Parity tests (21 modules) | 0.25 day | one test per shell→python migration, mostly fixture-driven |

Total: 3.25 dev-days. Round to **3 days** with focused execution; **5 days**
with operator review checkpoints between phases (which is the operator's
standing preference — *reviews before pushing*).

A useful side-effect of the migration: **every cron becomes unit-testable**.
Today the only way to verify `risk_monitor_15min` works is to wait 15 minutes
and read Slack. After Phase 2 there's a `tests/test_healthcheck.py` that
takes 200 ms.

---

## 9. Open questions for the operator

1. **Channel split.** Today every cron posts to one Slack webhook. Should
   V4's `briefer` get its own channel (`#bot-briefings`) so the daily P&L
   noise doesn't drown out the Monday pre-market post?
2. **Sub-second precision.** `os.replace` is atomic on POSIX but on macOS-NFS
   it's not guaranteed. The operator runs Linux only (per env probe); confirm
   no NFS state directories before relying on atomic rename.
3. **Adapter rollback UI.** The dashboard currently has no "revert to prior
   adapter" button. After `lora_promoter` Phase-2 cutover, do we need one, or
   is `ln -sfn data/lora-adapters/<role>/stable/v16 <role>-current` on the
   command line sufficient?
4. **Multi-region.** All Hermes state assumes one machine. If we ever shard,
   the state files become a coordination problem. Not in scope for rev2 but
   worth flagging.

---

## 10. Acceptance criteria

This module is "done" when:

- [ ] All 7 `quanta_core.hermes.*` modules ship in V4 with `def run() -> int`.
- [ ] `~/.quanta/state/*.json` schemas in §5.2 are implemented and validated
      by a JSON schema test.
- [ ] All 21 parity tests in `tests/parity/test_<module>.py` pass against
      seeded ledger fixtures.
- [ ] 7 consecutive successful runs of each `_quanta.sh` trampoline before
      Phase-2 cutover.
- [ ] Dashboard renders a "Hermes Loop Health" card driven by the 4 state
      files in §5.2 with last-run-age + last-failure-cause per module.
- [ ] §7 failure modes are reproducible via fault-injection tests
      (e.g. `tests/fault/test_reflector_skipped.py`).

When all six bullets are green, the closed loop in §4 runs unattended. The
bot remembers Monday on Sunday and trades differently on Monday-next. That is
quanta-core v4's definition of "learning".
