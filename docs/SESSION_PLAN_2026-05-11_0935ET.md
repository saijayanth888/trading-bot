# Trading bot stabilization plan · 2026-05-11 09:35 ET

**Mode:** Paper trading. We can afford to be down 1-2 hours. **Fix root causes; no band-aids.**

**Author:** Claude Opus 4.7 (planning before multi-agent dispatch)

---

## Inventory — every known issue, with severity

### Tier S — system-wide, blocking trading

| ID | Issue | Evidence | Root cause |
|---|---|---|---|
| **S1** | **Hermes scheduler lock bug** | `~/.hermes/logs/agent.log` 08:05-09:24: LLM crons (`risk_monitor_15min`, `market_research_30min`) hold `.tick.lock` for 10-30 min each. While held, all other crons (12+ wheel/shark scripts) get fast-forwarded past their grace windows. | `~/.hermes/hermes-agent/cron/scheduler.py:1672` holds the file lock from `tick()` entry through `f.result()` blocking on every dispatched job. |
| **S2** | **Persistent-pool patch broke gateway shutdown** | After my patch, gateway entered clean-exit + restart loop every 60s. Code reverted. | My patch left ThreadPoolExecutor threads alive across tick() boundaries. Gateway shutdown path expects job execution to complete inside tick(). Need to coordinate with `gateway/run.py`'s `_start_cron_ticker` shutdown semantics. |

### Tier A — visible to operator, eroding trust

| ID | Issue | Where | What it should be |
|---|---|---|---|
| **A1** | Hermes gateway shows "down" in frontend | Dashboard somewhere | Backend says `up=True, age_s=1.2` — UI bug. Find which renderer is reading wrong field. |
| **A2** | /ops_spa topbar `SESSION 0h 0m` | qc_react.js Topbar | Should be real bot uptime (from /api/v1/uptime or freqtrade ping). Currently page-load time. |
| **A3** | /ops_spa topbar EQUITY odometer leaks digit columns (`$0123456789012...`) | NumberRoll component | Each digit cell shows all 0-9 stacked. Per-digit translateY needs proper clipping. |
| **A4** | Stocks chart x-axis lacks date stamps | TradingView x-axis | 4-day-old data appears like today — show "Fri 8 · 3:55 PM" not "3:55 PM" when data spans >24h. |
| **A5** | XRP/DOGE/AVAX/LINK still 0 sparkline closes | /api/ops/sparklines | Strategy int64 scrub only touches new candles; cached historical rows still tainted. Need full-frame scrub on startup OR a one-time strategy reload. |

### Tier B — incomplete features

| ID | Issue | Scope |
|---|---|---|
| **B1** | Per-stock sentiment via Perplexity | New /api/ops/stocks_sentiment endpoint + dashboard card + Perplexity API key |
| **B2** | SPA chart indicator overlays (BB/RSI/MACD) | Port from TradingView Lightweight Charts to /dashboard_spa's custom React canvas |
| **B3** | Stocks RSI/MACD subcharts (legacy /charts) wired | Currently hidden on stocks; should show stock-specific RSI/MACD if we want it |

### Tier C — observability + housekeeping

| ID | Issue |
|---|---|
| **C1** | Need dashboard-side **alerting** when a cron misses its window (e.g., wheel_candles last_run > 30 min ago during market hours) |
| **C2** | Need a *training observability* card that shows the current FreqAI training pair + epoch in real time |
| **C3** | Document the Hermes lock bug in `docs/` so the team has the trace |

---

## Root-cause discussion — the Hermes lock bug

### Why my first patch failed

```python
# my patch:
parallel_pool.submit(_ctx.run, _process_job, job)   # fire and forget
return len(due_jobs)
```

Problem: the ThreadPoolExecutor's worker threads outlive `tick()`. When the gateway is asked to shut down (e.g., systemd reload), it calls `stop_event.set()` and expects the cron ticker thread to exit. With persistent pools, those workers don't know about `stop_event` and keep running. **More importantly**: at process exit, `concurrent.futures.thread._python_exit` is called as an `atexit` handler, which blocks on `t.join()` for every worker thread. If a worker is mid-LLM call, that join hangs until Python's atexit timeout, which can manifest as the gateway "exiting cleanly" (status=0) after a long stall.

Or possibly: my patch broke a subtle invariant where the gateway expects `tick()` to RETURN AFTER ALL JOBS COMPLETE so that subsequent gateway state (channel directory refresh, cache cleanup) runs in the right order.

### Correct fix design

Three viable approaches:

**Approach 1 — split locks (preferred).** Add a per-job-id lock (or use the existing `_jobs_file_lock` from `cron/jobs.py`). Keep `tick()` blocking on job completion AS BEFORE, but release the *file* lock immediately after `advance_next_run`. This means subsequent ticks fire even while jobs are still running, but each job protects its own `mark_job_run` write via the per-job lock.

```python
# inside tick():
with _file_lock():                       # short critical section
    due_jobs = get_due_jobs()
    for j in due_jobs:
        advance_next_run(j["id"])
# lock released here

# Then run jobs in the same in-tick pool as the original code:
with ThreadPoolExecutor(max_workers=N) as pool:
    futures = [pool.submit(...) for j in due_jobs]
    results = [f.result() for f in futures]
return sum(results)
```

This keeps the gateway's invariant ("tick() blocks until done") intact. The lock-release-early is the surgical change.

But there's still a problem: the gateway's tick loop is `while not stop_event.is_set(): tick(); sleep(60)`. If tick() takes 30 min, the gateway tick thread doesn't fire again for 30 min. That re-creates the original problem.

**Approach 2 — true background workers.** The gateway should spawn a small set of *dedicated cron-job worker threads* at startup, each blocking on a queue. The tick() function pushes due jobs onto the queue and returns. Workers process jobs at their own pace. On shutdown, `stop_event.set()` signals workers to exit cleanly.

This requires modifying `gateway/run.py`'s `_start_cron_ticker` too. Slightly bigger change but cleanest semantically.

**Approach 3 — convert `tick()` to async.** Return job futures from tick(); gateway awaits them with a background task. Probably the cleanest but requires more familiarity with the gateway's asyncio setup.

**Recommendation:** Approach 1. Smallest surface change. Trade-off: a single in-flight LLM cron still blocks the *next* one of itself, but doesn't block UNRELATED crons (the stocks crons we care about).

---

## Multi-agent dispatch plan

### Phase 1 (30 min — solo, sequential, low risk)

Tasks I should do directly before fanning out:

1. **Fix A1** (Hermes "down" in front-end) — likely a Jinja conditional reading the wrong field. ~10 min.
2. **Fix A3** (NumberRoll odometer leak) — CSS / per-digit translateY math. ~10 min.
3. **Verify A2 = correct intent** — decide if "SESSION" should be bot uptime or page session; either way fix the label. ~5 min.

### Phase 2 (60 min — three agents in parallel, isolated scopes)

| Agent | Scope | Output |
|---|---|---|
| **Agent-H (Hermes)** | Approach 1 from above: surgical split-lock patch of `~/.hermes/hermes-agent/cron/scheduler.py`. Read gateway shutdown path first; ensure tick() still blocks-until-done so shutdown invariants hold. Test by killing the LLM crons temporarily, restarting gateway, watching whether wheel_candles fires at next */5 slot. | Tested patch + restart of gateway + 1 successful cron tick observed |
| **Agent-T (Telemetry heal)** | Fix A5: figure out why XRP/DOGE/AVAX/LINK pair_candles still 500s on limit=60 despite the int64 strategy scrub. May need to bounce freqtrade once more, or write a one-shot scrub that touches the entire cached DataFrame on first populate_indicators call after restart. | All 8 crypto pairs return 60 closes from /api/ops/sparklines |
| **Agent-S (Stocks chart + sentiment)** | Two-part: (a) Fix A4 — x-axis date stamps when data spans >24h; (b) Build B1 — /api/ops/stocks_sentiment endpoint stub + Perplexity fetcher + dashboard card. Sentiment data can be MOCK first; pipeline integration is the goal. | x-axis shows dates; new sentiment card renders (even with placeholder data initially) |

### Phase 3 (30 min — solo verification)

- Take screenshots of all four pages
- Verify Hermes gateway shows up in frontend
- Verify pair telemetry full
- Verify stocks chart x-axis dates
- Update `MORNING_BRIEFING_2026-05-11.md` with closure status
- Commit everything

---

## Decision points for the operator

Before spinning up Phase 2 agents, confirm:

1. **Approach 1 for Hermes (split-lock)** is the right call vs. Approach 2 (dedicated workers in gateway) — I prefer Approach 1 because it's smaller-surface, but Approach 2 is more correct long-term.
2. **Risk tolerance for restarting freqtrade again** to clear cached int64 cells (5 min downtime; bot stays in paper mode safely).
3. **Perplexity API key availability** for B1 — if no key, the sentiment card ships with placeholder data and a "wire your key here" config note.

---

## What this plan does NOT include

- DRL ensemble re-training (separate work)
- TFT live-training observability (already exists; just buried)
- Slack notification routing (separate from Hermes scheduler)
- Trading strategy changes (we're not touching strategy this week)

These remain on the backlog and don't block this morning's stabilization.
