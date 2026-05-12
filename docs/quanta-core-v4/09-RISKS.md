# Quanta Core V4 — Risks & Rollback

Branch: `feat/quanta-core-v4-design-r9` · Author: risk+rollback agent · Date: 2026-05-12

**This document is paranoid by design.** The operator has $19k in Coinbase Advanced,
a $5k DGX Spark already on the desk, four weeks of dev runway, and a history of
self-acknowledged scope drift. Sugar-coating costs money. Read every row.

> If you want the executive answer: V4 should **not** receive a single dollar of
> real capital until shadow-mode parity (defined in §2) has held for 14
> consecutive trading days. Anything earlier is gambling, not deployment.

---

## 0 · Top 5 risks at a glance

| Rank | Risk | Why it's #1-#5 |
|------|------|-----------------|
| 1 | **Cutover blast radius — double-orders / paper-vs-live confusion** | Single config flip routes the same signal through two engines that both think they're the source of truth. Outcome: real $ trades execute on Coinbase while paper ledger thinks it's still dry-run. Recovery cost: cash. |
| 2 | **Shadow-mode divergence with no oracle** | When V4 and Freqtrade disagree on 30% of trades, there is **no ground truth** that says which is correct until the trade settles. The natural human bias is to trust whichever lost less yesterday. That's not science. |
| 3 | **Operator burnout / single-bus-factor on a 4-week sprint** | This is one of multiple jobs. Sprint cadence is the operator's #1 silent risk. A burned-out operator ships bugs into production at 23:00 on day 22. |
| 4 | **Continuous LoRA training overfits on recent regime, then regime breaks** | Weekly adapter refresh trained on the last 30 days of trending data → drops 8% predictive hit-rate the day vol spikes. The pre-commit promote/rollback gate must catch this BEFORE it goes live, not after. |
| 5 | **ARM/Blackwell software stack still bleeding edge** | vLLM-on-Blackwell, TensorRT-LLM-on-GB10, PyTorch ARM wheels — each has been the source of an outage in the last 60 days of community reports. Migration cannot proceed if the inference plane crashes weekly. |

If any one of those five is unresolved, V4 stays in shadow mode.

---

## 1 · Risk register

Likelihood × Impact rubric:

- **Likelihood:** Low (<10% over migration window) · Medium (10-40%) · High (>40%)
- **Impact:** Low (debug-session cost) · Medium (1-3 days lost) · High (capital loss or week+ lost) · Critical (account-wipe-class)

### 1.1 Migration risks

| ID | Description | Likelihood | Impact | Mitigation | Rollback |
|----|-------------|------------|--------|------------|----------|
| M1 | **Shadow-mode divergence with no oracle.** V4 and Freqtrade disagree on >30% of trade decisions; no ground truth says which is right until weeks of P&L compound. Operator bias: trust whichever lost less yesterday. | High | High | (a) Define a *static* shadow-eval set: 90 days of historic ticks, replay through both engines, compare deterministically. (b) Live shadow: only count divergence on signals where *both* engines would have entered (Jaccard intersection, not union). (c) Publish a `/ops` shadow-diff card with per-symbol breakdown. | Stay on Freqtrade; do not promote V4 until parity gate (§3) clears. |
| M2 | **Cutover blast radius — double-orders.** Stale Freqtrade process keeps placing orders after V4 cutover; same signal hits Coinbase twice. | Medium | Critical | (a) Hard kill of `freqtrade` container before V4 takes the API key. (b) Coinbase API key rotation at cutover (V4 uses a NEW key; old key revoked). (c) Pre-cutover dry-run: V4 in shadow + Freqtrade live, verify ZERO order overlap for 48h. | See §3 Rollback Runbook step C-2 ("freeze new key, re-enable old key, restart Freqtrade"). |
| M3 | **Paper-vs-live ledger confusion.** During transition, V4 thinks it's paper, Freqtrade thinks it's live, or vice versa. Operator looks at one dashboard, real money is moving elsewhere. | Medium | Critical | (a) `runmode` env var must be sourced from a SINGLE file `runmode.env` consumed by both stacks. (b) Dashboard header shows runmode for V4 AND Freqtrade side-by-side until Freqtrade is removed. (c) Slack pre-cutover summary: "V4=live, Freqtrade=stopped" must be hand-acked. | Restore `runmode.env` from `setup/backups/`; restart both stacks; reconcile. |
| M4 | **Data lineage split — trades in two ledgers.** V4 writes to one TimescaleDB schema, Freqtrade writes to its own SQLite `tradesv3.sqlite`. Reconciliation requires a custom join. P&L numbers drift. | High | Medium | Build a **reconciliation cron** (hourly) that pulls from both, writes to `quanta_trades_unified` view, alerts on any mismatch. Run for the whole shadow window. | Disable V4 writer; revert to Freqtrade-only ledger; cron continues for forensics. |
| M5 | **Loss of Freqtrade ecosystem.** Telegram bot, FreqUI, strategy recipes from /r/freqtrade, FreqAI tooling — all gone if V4 is a full rewrite. Operator already uses Telegram /status. | High | Medium | (a) V4 ships with a Telegram-compat shim that responds to the same commands. (b) FreqUI alternative is *already* the in-house dashboard — verify feature parity before retiring FreqUI. (c) Keep `freqtrade-cli` available for one-off backtests. | Keep Freqtrade container in `--profile legacy`, available for spot-checks. |
| M6 | **In-flight trade lost during cutover.** Freqtrade has an open position; V4 boots, doesn't see it, opens a contradicting one. | Medium | Critical | (a) Cutover only when zero open positions (script-checked, dashboard-confirmed). (b) If positions exist, V4 imports them from Freqtrade DB on first boot and treats them as read-only until manual ack. | If V4 doubled up: market-close the V4-opened position; resume on Freqtrade. |
| M7 | **Schema drift.** V4's `decisions` table has columns Freqtrade ledger doesn't have; merging historic P&L for fitness calculations breaks. | Medium | Low | Migration script ships in `docs/quanta-core-v4/migrations/` with versioned up/down. Test on a copy of TimescaleDB before any prod touch. | `psql -f down_v4_001.sql` — see §3-C5. |
| M8 | **"Freqtrade-stack-still-runs" flag silently drifts.** We promise a 30-day fallback. Reality: nobody patches Freqtrade dependencies for 30 days, and on day 25 an exchange API change breaks it. The fallback is gone when we need it. | High | High | (a) Daily cron pings Freqtrade `/api/v1/ping`. (b) Slack alert if Freqtrade hasn't placed a paper trade in 24h. (c) Weekly `pip-audit` against the legacy container's requirements. | If fallback dies, *that's the decision gate to stop migrating*: §2-DG-4. |
| M9 | **Config sprawl during V4 phase.** Operator already has `regime_gating`, `risk_management`, `execution` in config.json. V4 will add `meta_blender`, `lora_adapters`, `mc_calibrator` blocks. Operator can't reason about it. | Medium | Medium | Single `/ops` Config Overview card showing EVERY tunable knob and current value (already exists; extend it). | N/A — purely a UX risk. |
| M10 | **Migration timeline slips past 6 weeks.** Operator's other jobs steal hours; the sprint stretches to 10 weeks; meanwhile $19k sits in a paper account earning nothing. | High | Medium | Decision gate §2-DG-5: at week 4 day 5, if Phase 2 (shadow) hasn't even started, stop, lock V1+revert, and harvest learnings. | Restore Freqtrade as primary; archive V4 work to a `feat/` branch with no merge timeline. |

### 1.2 Operational risks

| ID | Description | Likelihood | Impact | Mitigation | Rollback |
|----|-------------|------------|--------|------------|----------|
| O1 | **Continuous LoRA training overfits on recent regime, regime breaks the next day.** Weekly adapter trained on 30d of trending → regime shift → 8% hit-rate drop → live capital bleeds for a week. | High | High | (a) Out-of-sample eval on the OLDEST 7-day window the new adapter has NEVER seen — promote only if hit-rate ≥ previous adapter on that. (b) The locked rule (project-modelforge-decisions §3): promote/rollback gate fires automatically; >5% hit-rate regression = auto-rollback. (c) Hold-out always includes a *known-volatile* week (Apr 2025 bond-rate-spike day, Mar 2020 covid week) in the eval. | Auto-rollback via ModelForge promote-step. If auto-rollback also broken: pin `qwen3:30b-reflector-current` alias to last known-good tag manually (§3-C7). |
| O2 | **Sub-second decision loop → operator can't intervene → fat-finger compounds.** V4's design targets sub-1s response. If a runaway loop sends 200 buy orders before kill-switch fires, $$ gone. | Medium | Critical | (a) **Rate-limit at the execution layer, not the decision layer.** Max 1 order / pair / 60s, max 10 orders / minute across all pairs. Hard-coded floor. (b) Kill switch on `/ops` button + Coinbase API key revocation (also on /ops). (c) Slack alert on any minute with >5 orders. | `/ops → KILL` revokes Coinbase keys; positions go untouched until ack. |
| O3 | **Real-time Monte Carlo blocks legitimate trades when calibration drifts.** MC says "this trade has 12% blow-up probability" because the empirical vol regime changed and MC priors are stale. Operator watches profitable signals get rejected for a week. | Medium | High | (a) MC is **advisory only** for first 30 days of V4 live — logged, dashboarded, NOT a hard block. (b) MC recalibration every 24h on rolling 90d of realized returns. (c) Dashboard panel: "MC reject reasons" — operator can spot when it's misbehaving. | Set `mc_calibrator.mode = "log_only"` in config; restart V4. |
| O4 | **Multi-model residency — one model crashes, all evict (Ollama bug).** Documented Ollama behavior: `--keep-alive` failures cascade. We plan 3-4 adapters resident concurrently. | Medium | Medium | (a) Single-base + adapters (project-drop-vllm.md decision) means there's really only ONE model in memory; adapters are lightweight. Risk is already partially mitigated. (b) Health-check cron pings Ollama every 60s; alert on any 500. (c) Restart cooldown: Ollama restart limited to once per 5 min. | `systemctl restart ollama` (operator level). Trading-bot side: `chat_json` retries already exist. |
| O5 | **DGX Spark single-machine reliability.** One box, one fan, one PSU, one Ubuntu install. No redundancy, no failover. A disk failure stops trading entirely. | High | High | (a) Hourly snapshot to `~/Documents/setup/backups/` (already in place; reference-backup-system memory). (b) Cold-restore drill MONTHLY — do not skip; an untested backup is no backup. (c) If a hardware failure happens during a trade: Coinbase API key is **time-locked** — V4 cannot place orders without the box being up. Risk converts from "wrong trade" to "missed opportunity." Acceptable. | If box dies mid-trade: log into Coinbase web, manually close open positions, revoke API key. |
| O6 | **Operator on vacation / sick / asleep during a regime break.** Bot keeps trading; LoRA refresh promotes a bad adapter at 03:00; nobody notices for 18h. | Medium | High | (a) Slack alert on Reflector hit-rate drop >5% triggers IMMEDIATE pause (not just rollback). (b) `/ops` "Pause for N hours" button — operator presets a pause window before known absence. (c) Hard daily-loss limit (3% existing) is a backstop. | Hard daily-loss limit kicks in; circuit breaker stops trading; operator wakes up to a flat day, not a wiped day. |
| O7 | **Coinbase rate limit on V4's higher decision cadence.** V4 polls ticks faster, hits 429s, retries hammer the API harder, account flagged. | Medium | Medium | (a) Token-bucket limiter in `execution_engine`. (b) Coinbase Advanced has documented per-account quotas — V4 budget = 50% of quota, leaving headroom. (c) Backoff on any 429. | Drop poll cadence to 5s; revert to Freqtrade cadence as needed. |
| O8 | **Decision log fills disk.** Sub-second decisions × 10 symbols × 30 days = ~30M rows in `llm-calls.jsonl` + decisions table. Disk fills → freqtrade or postgres crashes. | Medium | Medium | (a) Logrotate on `llm-calls.jsonl` (daily, compress, keep 30d). (b) TimescaleDB compression on chunks >7d old (built-in). (c) Disk alert at 70% (already configured in grafana). | Truncate oldest decisions table partition; rotate logs manually. |
| O9 | **Cron stack overlap during V4 transition.** Both Freqtrade nightly + V4 nightly fire at 23:00; same Ollama model serves both; latency blows up; both write to overlapping tables. | Medium | Medium | (a) Stagger cron windows — V4 nightly at 22:00, Freqtrade at 23:30. (b) Ollama queue is FIFO; just slower, not crashing. (c) One source of truth for "what runs at 23:00" — single `crontab.txt` checked into repo, no ad-hoc additions. | Disable Freqtrade nightly cron during V4 shadow phase; re-enable on rollback. |

### 1.3 Software / hardware risks

| ID | Description | Likelihood | Impact | Mitigation | Rollback |
|----|-------------|------------|--------|------------|----------|
| S1 | **vLLM / NIM / TensorRT-LLM on Blackwell GB10.** Bleeding edge — community reports show vLLM still has open issues on Blackwell as of late 2025/early 2026. We *already* hit this 2026-05-12 (vLLM ate 95 GB RAM). | High | High | **Decision already made in [[project-drop-vllm]]: Ollama-only.** Do NOT re-introduce vLLM/NIM/TRT-LLM into the V4 hot path without an explicit operator authorization AND a 24h soak test on a separate user. | Already on Ollama; if Ollama itself breaks on Blackwell, fallback is CPU inference (slow but works). |
| S2 | **alpaca-py + coinbase-advanced-py async bugs.** Less mature than CCXT, less community testing. The bots we've seen use CCXT for a reason. | Medium | High | (a) **Use CCXT for V4** unless there is a hard reason not to. The Freqtrade stack uses CCXT successfully today. (b) If alpaca-py is genuinely needed for stocks: pin a specific version, run a 7-day shadow against the official Alpaca SDK, only then promote. | Revert to CCXT-only in `execution_engine`; remove alpaca-py from requirements. |
| S3 | **PyTorch ARM wheel gotchas.** GB10 is ARM. Some wheels (bitsandbytes, flash-attn, certain CUDA ops) ship x86-only or break on aarch64. | High | Medium | (a) Pin PyTorch + transformers + unsloth to the EXACT versions known to work on aarch64 (already in `requirements-extra.txt` historically). (b) New deps must pass a `pip install --dry-run` on the box BEFORE merging. (c) Lockfile committed. | `pip install -r requirements-extra.txt.lock.old` from `setup/backups/`. |
| S4 | **Unsloth update breaks LoRA training.** Unsloth ships rapid updates; one of them changes the export-to-GGUF path and ModelForge promote step fails silently. | High | Medium | (a) Pin Unsloth version in ModelForge requirements. (b) Promote-step smoke test: generate a 100-step adapter on a fake dataset, verify GGUF round-trip every cron run. (c) Slack alert on any promote-step exception. | Roll Unsloth back to last known version; ModelForge image rebuild from `requirements.lock`. |
| S5 | **Ollama version pinning.** Ollama updates can change the `/api/create` Modelfile schema. Adapter publishing breaks. | Medium | Medium | Pin Ollama version (`apt-mark hold ollama` or use the .deb URL). Test before any upgrade. | Downgrade Ollama via the prior `.deb`. Kept in `~/Documents/setup/installers/`. |
| S6 | **TimescaleDB chunk-compression mid-trade.** Compression runs background, locks the chunk, V4 write blocks for 30s, retry storm. | Low | Medium | (a) Compression policy runs at 04:00 UTC (lowest trade activity). (b) `lock_timeout = 5s` on V4 writes; retry to next 1m candle. | Disable compression policy until off-hours-only proven safe. |
| S7 | **Disk SMART failure on the box.** ARM box, single NVMe, no RAID. | Low | Critical | Hourly rsync to a USB external + remote-to-laptop. SMART monitoring weekly. | Restore from backup; this is where the cold-restore drill pays off. |
| S8 | **NVIDIA driver / CUDA update breaks Ollama mid-week.** Operator hits "apt upgrade" out of habit. | Medium | High | (a) `apt-mark hold` on nvidia-driver + cuda-toolkit. (b) Documented upgrade procedure with snapshot first. (c) Slack on apt-mark drift (cron). | Boot to previous kernel via grub; `apt downgrade` the driver. |

### 1.4 Financial risks

| ID | Description | Likelihood | Impact | Mitigation | Rollback |
|----|-------------|------------|--------|------------|----------|
| F1 | **V4 goes live before parity proven → real money loss.** The temptation: "we've waited 3 weeks, let's just turn it on." | Medium | Critical | **Hard rule:** V4 cannot be promoted from `dry_run=true` to live until: (a) 14 consecutive trading days of shadow with <10% trade-decision divergence vs Freqtrade, (b) shadow P&L within 20% of Freqtrade P&L, (c) operator explicit ack via a `/ops` button that requires typing "PROMOTE V4" (not a checkbox). | `/ops` PROMOTE button is the ONE way; if the button isn't clicked, V4 stays paper. No env-var flip, no CLI override. |
| F2 | **Sunk cost on $5k DGX → operator over-commits to V4.** "I bought the box, I have to make V4 work." | High | High | **This document is the mitigation.** The DGX is useful for ModelForge / inference / research regardless of V4. Freqtrade-on-DGX is fine; V4 is not required to justify the hardware. Operator should re-read this row monthly. | If V4 fails parity at week 6: archive V4, return to Freqtrade-on-DGX with ModelForge as the only addition. DGX is still earning its keep. |
| F3 | **Premium-collected on options pilot evaporates in a margin call.** Wheel pilot is already live ($629 premium per session memory). V4 adds option-strategy logic; a bug sells uncovered puts. | Low | Critical | (a) Coinbase Advanced doesn't even sell options — this risk is in the **stocks side**, not crypto. (b) Option-selling code path must require a `covered=true` flag derived from current holdings, not config. (c) Margin trading disabled at the broker level until V4 has 60d of live stocks track record. | Close all open option positions immediately; revert option-strategy code to last known-good commit. |
| F4 | **Slippage on V4's higher-frequency entries.** V4 enters faster than Freqtrade; market makers see the flow; slippage doubles. | Medium | High | (a) Slippage gate already 0.30% (project-trading-bot.md). (b) Track realized vs expected slippage per trade; if 95th-percentile slippage doubles, throttle entry rate. (c) Limit-orders only (not market orders) is the existing policy — keep it. | Revert to Freqtrade entry cadence; market-maker behavior reverts within hours. |
| F5 | **Forex tail risk on stocks side.** Crypto is USD-denominated; stocks are too, but if V4 ever touches a non-USD pair via Alpaca's international stocks, FX moves blow up the P&L attribution. | Low | Medium | Hard config: `allowed_currencies = ["USD"]` only. Reject any pair that crosses. | Trivial — config change. |

### 1.5 Personal / operator risks

| ID | Description | Likelihood | Impact | Mitigation | Rollback |
|----|-------------|------------|--------|------------|----------|
| P1 | **Burnout from sprint length.** 4-6 weeks of evening + weekend work on top of other jobs. The pattern after burnout: 23:00 commits with low rigor, "I'll just turn it live and watch it." | High | Critical | (a) **Hard checkpoint every Friday.** Operator reviews progress + sleep / energy honestly. Skip a week's work if the answer is "I'm fried." (b) Sprint length is a budget, not a deadline. If week 4 isn't ready, week 8 is fine; the bot trades fine on Freqtrade. (c) NO live-promotion commits after 22:00 local — enforced socially. | Pause the sprint; Freqtrade keeps running on auto; operator returns when refreshed. |
| P2 | **Trading bot is one of multiple jobs — sustainable cadence?** "Side project" + "this is the main one"  — pick one. | High | High | Honest answer to write down: **trading-bot is currently making $0/month and the operator goal is $1k/month**. Until that proves out, treat it as a side project. Don't reorganize life around it. | Cut session frequency to 2x/week; Freqtrade keeps running. |
| P3 | **Single-developer bus factor.** If the operator is hit by a bus, the bot keeps trading until the daily-loss limit hits. Family doesn't know how to stop it. | Medium | High | (a) `docs/EMERGENCY_STOP.md` — printed and put in the operator's filing cabinet. 3 steps: revoke Coinbase API key (URL + steps), shut down the box, manually close positions on Coinbase web. (b) Family briefed once (1 hour). | EMERGENCY_STOP.md procedure. |
| P4 | **Operator over-commits in this session.** Post-frustration energy → "let's design V5 too while we're at it." | High | Medium | (a) Worktree-per-agent already isolates blast radius. (b) Decision gate §2-DG-1 forces a written go/no-go *before* code changes. (c) `superpowers:brainstorming` skill must run before any "new feature" suggestion in this sprint. | Discard worktree; no harm done. |
| P5 | **Operator skips the parity gate "just this once."** Pressure to show results to whoever (self, partner, internet). | Medium | Critical | This row exists so future-operator can read it. **If you are reading this and tempted to skip the parity gate: don't.** The bot trades fine on Freqtrade. There is no external deadline. Re-read row F2 (sunk cost). | N/A — preventive only. |

---

## 2 · Decision gates

Each phase has a numbered gate. If the gate's *stop signal* fires, do not proceed to the next phase. Re-assess.

### Phase 0 — Design (current phase)

**DG-0 · Design completeness gate** — Before any code is written.

| Signal | Threshold | Action |
|--------|-----------|--------|
| All 9 design docs (`01-OVERVIEW` … `09-RISKS`) exist on `feat/quanta-core-v4-design-r9` | Binary | If missing, finish design first. No code. |
| Operator has read this risks doc and acked in HANDOFF.md | Binary checkmark | If not acked, do not start Phase 1. |
| Rollback runbook (§3) is copy-pasteable; operator can execute each step in <5 min | Operator dry-run | If any step needs investigation, fix the runbook before Phase 1. |

### Phase 1 — Foundation (V4 scaffolding, no live trades)

**DG-1 · Foundation gate.**

| Signal | Threshold | Action |
|--------|-----------|--------|
| V4 container boots and connects to TimescaleDB + Ollama + Coinbase (paper) | All 3 green | If <3 green, stop. Debug before Phase 2. |
| Reconciliation cron (M4) writes to `quanta_trades_unified` view with 0 mismatches | 7 days clean | If mismatches > 0, stop and find the schema drift. |
| Backup cold-restore drill executed; box restored from yesterday's snapshot in <30 min | Operator-timed | If drill fails, fix backups before any further work. |

### Phase 2 — Shadow mode (V4 decisions logged, NOT executed; Freqtrade is live)

**DG-2 · Shadow parity gate.** *This is the critical one.*

| Signal | Threshold | Action |
|--------|-----------|--------|
| Trade-decision divergence between V4 and Freqtrade | <10% on intersection signals | If 10-30%: investigate divergence reasons; do not proceed but do not roll back. If >30%: stop, write up the disagreement, decide which engine to trust. |
| Shadow P&L correlation | r > 0.85 on daily-bar returns | If r < 0.85, V4 and Freqtrade are different strategies, not two implementations of the same. Re-design or pick one. |
| Number of consecutive trading days at parity | ≥14 | If a divergence event resets the counter, the 14d window starts over. **No grace.** |
| Operator has reviewed shadow-diff card weekly | 2 of 2 weekends | If not reviewed, parity isn't really validated. Operator must engage. |

### Phase 3 — Single-symbol live (V4 trades 1 pair real money; rest stay on Freqtrade)

**DG-3 · Single-symbol live gate.**

| Signal | Threshold | Action |
|--------|-----------|--------|
| 14-day shadow parity (DG-2) cleared | Binary | If not cleared, no live trading. |
| Operator typed "PROMOTE V4" in the `/ops` confirmation field | Binary | If not typed, stay paper. |
| Pair selected has lowest divergence in shadow | Pick from data | Default to BTC/USD if all equal — most liquid, smallest slippage. |
| Capital exposure cap | <$2,000 (10% of $19k) | Hard cap in config; reject sizes above. |
| Daily P&L vs shadow prediction | Within ±50% for 7 days | If real P&L deviates >50% from shadow prediction, slippage / execution issue; stop and investigate. |

### Phase 4 — Full live cutover

**DG-4 · Cutover gate.**

| Signal | Threshold | Action |
|--------|-----------|--------|
| 30 days single-symbol live with positive Sharpe | Sharpe > 0.5 (lower than Freqtrade's 1.5 target *for the bar to flip*) | If Sharpe ≤ 0.5, V4 is worse than Freqtrade on real money. Roll back. |
| Freqtrade fallback is still healthy | M8 alerts clean for last 7 days | If fallback is broken, do not cut over until restored. |
| Operator answered "ready?" yes in last 24h, not stale | Fresh ack | Operator state is part of the gate. Tired = not ready. |

### Phase 5 — Decommission Freqtrade (only if everything above held)

**DG-5 · Decommission gate.**

| Signal | Threshold | Action |
|--------|-----------|--------|
| V4 live > 60 days at full pair set | Calendar | If not 60d, keep Freqtrade alive in `--profile legacy`. |
| Two regime transitions observed and survived | Manual confirmation from regime_detector logs | If only one regime seen, V4 hasn't been stress-tested. Wait. |
| Operator has felt no urge to manually intervene in 30d | Self-report | If you needed to override V4 in the last month, it isn't trustworthy yet. |

### Universal STOP gate — fires at any phase

| Signal | Threshold | Action |
|--------|-----------|--------|
| Single-day loss > 3% of capital | Existing risk_governor | Auto-pause. Operator + this doc reviewed before resume. |
| Two consecutive days of -1%+ loss | New rule for V4 phases | Auto-pause. Even if individual days don't hit -3%, drift kills accounts. |
| Box CPU >90% for >5 min | Grafana | Auto-pause trading writes; Ollama could be wedged. |
| Ollama API 500 error rate >5/min | Grafana | Auto-pause; decisions can't be made cleanly. |
| Operator hasn't logged in to /ops in 72h during a sprint week | Dashboard heartbeat | Slack ping; if no response in 24h more, auto-pause. |

---

## 3 · Rollback runbook

Every step is copy-pasteable. Test each one in a paper environment before relying on it in live.

> **Time budget per step:** Each step ≤ 5 minutes. If it takes longer, the runbook is wrong — fix it.

### 3.A · Emergency stop (any phase, any time)

The "something is on fire" path. Run from any shell on the box.

```bash
# A1 · Revoke Coinbase API key from Coinbase web UI:
#     https://www.coinbase.com/settings/api → revoke "trading-bot-prod"
#     (browser action — no shell command can do this; do it FIRST)

# A2 · Stop all trading containers
cd ~/Documents/trading-bot
docker compose stop freqtrade quanta-core dashboard

# A3 · Confirm nothing is running
docker compose ps | grep -E '(freqtrade|quanta-core)'
#     Expect empty / Exited

# A4 · Snapshot current state for forensics
mkdir -p ~/Documents/setup/incident-snapshots/$(date -u +%Y%m%dT%H%M%SZ)
cd ~/Documents/setup/incident-snapshots/$(date -u +%Y%m%dT%H%M%SZ)
docker compose -f ~/Documents/trading-bot/docker-compose.yml logs --tail 5000 \
  freqtrade quanta-core dashboard > docker-logs.txt 2>&1
cp ~/Documents/trading-bot/user_data/config.json config.json
psql -h localhost -p 5434 -U trader -d tradebot \
  -c "\copy (SELECT * FROM decisions ORDER BY ts DESC LIMIT 1000) TO 'recent-decisions.csv' CSV HEADER"

# A5 · Manually close open positions on Coinbase web (browser)
#     The API key is dead; this is now hand-only.
#     Document each close in incident-snapshots/<ts>/closes.md
```

### 3.B · Phase rollbacks

#### B-Phase1 · Roll back V4 foundation

```bash
# Phase 1 = scaffolding only; nothing is live. Safe to revert.
cd ~/Documents/trading-bot
git checkout main
docker compose stop quanta-core
docker compose rm -f quanta-core
docker volume rm trading-bot_quanta-core-data  # only if you want a clean DB
# Freqtrade was never stopped during Phase 1. No further action.
```

#### B-Phase2 · Exit shadow mode (V4 was logging-only; trivial)

```bash
cd ~/Documents/trading-bot
docker compose stop quanta-core
# Freqtrade still running, untouched
# Optional: keep V4 in /ops as "off"; do not delete the container — preserve forensics
```

#### B-Phase3 · Single-symbol live → revert to Freqtrade

```bash
# Step C-1 — disable V4 trading writer (config flip, no restart needed if hot-reload works)
cd ~/Documents/trading-bot
jq '.runmode = "dry_run" | .execution.engine = "freqtrade"' \
  user_data/config.json > user_data/config.json.new
mv user_data/config.json.new user_data/config.json

# Step C-2 — rotate Coinbase API keys (V4's key out, Freqtrade's key in)
#   Browser: revoke "quanta-core-v4-prod"
#   Browser: re-enable "freqtrade-prod" (or create new + paste into secrets)
#   Update secrets/coinbase.env if you regenerated

# Step C-3 — restart Freqtrade with the active key
docker compose restart freqtrade
docker compose logs --tail 100 freqtrade | grep -i 'exchange.*ok'

# Step C-4 — stop V4 cleanly
docker compose stop quanta-core

# Step C-5 — (optional) revert any V4 schema migrations
psql -h localhost -p 5434 -U trader -d tradebot \
  -f docs/quanta-core-v4/migrations/down_v4_001.sql

# Step C-6 — reconciliation sweep
python3 scripts/reconcile_v4_vs_freqtrade.py --since "$(date -u -d 'today 00:00' +%Y-%m-%dT%H:%M:%SZ)"
#   Investigate any unmatched trade IDs before declaring the rollback clean.
```

#### B-Phase4 · Full cutover → revert to Freqtrade

Same as B-Phase3 plus restoring Freqtrade-managed pairs that had been migrated.

```bash
# Step D-1 — re-enable all pairs in Freqtrade config
cd ~/Documents/trading-bot
git diff main..feat/quanta-core-v4-design-r9 -- user_data/config.json
#   Manually revert the pair_whitelist block to its main-branch state, OR:
git checkout main -- user_data/config.json
#   (loses any non-pair changes; review the diff first)

# Step D-2 — restart Freqtrade
docker compose restart freqtrade

# Step D-3 — confirm trade resumption on Slack + /ops
```

#### B-Phase5 · Re-resurrect Freqtrade after decommission

This is the painful one — only relevant if you went through with §3-DG-5 and now regret it. The runbook exists because the operator's "freqtrade-stack-still-runs flag preserved for N days" is N=60.

```bash
# Step E-1 — Freqtrade was archived to feat/legacy-freqtrade. Check out:
cd ~/Documents/trading-bot
git fetch origin feat/legacy-freqtrade
git worktree add ../trading-bot-legacy feat/legacy-freqtrade

# Step E-2 — rebuild image (deps may have drifted)
cd ../trading-bot-legacy
docker compose --profile legacy build freqtrade

# Step E-3 — pip-audit on the legacy requirements
docker compose run --rm freqtrade pip-audit
#   Resolve any HIGH-severity findings before bringing it up.

# Step E-4 — point Freqtrade at the existing TimescaleDB (or sqlite)
#   Edit docker-compose.yml's freqtrade.depends_on and env: DATABASE_URL
#   Ensure no overlap with quanta_trades_unified writes.

# Step E-5 — start in dry_run first; verify 24h; then promote.
docker compose --profile legacy up -d freqtrade
```

### 3.C · Cross-cutting recoveries

#### C7 · Pin Ollama adapter alias to last known-good

```bash
# When auto-rollback in ModelForge fails and an adapter is misbehaving.
ssh dgx  # if not already on the box
ollama list | grep reflector
#   Find the last tag that worked, e.g. qwen3:30b-reflector-v20260501

ollama cp qwen3:30b-reflector-v20260501 qwen3:30b-reflector-current
#   Now :current points back to the v20260501 tag.

# Verify
curl -s http://localhost:11434/api/show -d '{"name":"qwen3:30b-reflector-current"}' \
  | jq '.modelfile' | head
```

#### C8 · Restore from backup (the cold-restore drill)

```bash
# Use case: disk failure, accidental rm -rf, or "the box won't boot."
# The drill MUST be run monthly per O5. This is the sequence.
ssh recovery-host  # or boot from USB
cd /mnt/replacement-disk
rsync -avh user@dgx-host:~/Documents/setup/backups/trading-bot/latest/ ./trading-bot/
cd trading-bot
docker compose up -d postgres
sleep 30
# Verify the latest backup actually has today's data
psql -h localhost -p 5434 -U trader -d tradebot \
  -c "SELECT max(ts) FROM decisions"
#   If max(ts) is stale, the backup is broken — do NOT trade on this restore.
```

#### C9 · Hot-pause without restart

```bash
# Operator's "I don't trust the bot for the next 2 hours" path.
curl -X POST http://localhost:8081/api/ops/pause \
  -H 'Content-Type: application/json' \
  -d '{"duration_minutes": 120, "reason": "manual"}'

# Resume
curl -X POST http://localhost:8081/api/ops/resume
```

---

## 4 · Worst-case scenarios

Three scenarios, ranked by realism. Each is handled.

### 4.1 · "V4 places 50 orders in 30 seconds because of a bad LoRA + Coinbase rate-limits us"

**Sequence:**
1. Adapter `reflector-v20260605` ships with a bug — every signal returns `confidence=0.99`.
2. V4's signal threshold passes; risk_governor is bypassed because confidence is "high."
3. Order-rate-limiter (O2) caps at 10 orders/minute; first 10 succeed.
4. Coinbase 429s the rest.
5. Slack fires "high decision rate" alert at the 5-order/minute threshold (O2-c).
6. Operator clicks `/ops → PAUSE`. Trading stops.
7. Rollback per §3-C7 — pin alias back to last good adapter.
8. **Damage:** ~10 contradictory open positions, ~$200 in slippage + fees. Not account-wiping.

**Why this is handled:** Order rate limiter is at the **execution layer**, not the decision layer. The decision can be wrong; the execution still throttles.

### 4.2 · "V4 and Freqtrade both place orders for 30 minutes during cutover because of a missed step"

**Sequence:**
1. Operator runs cutover at 23:00 (already too late — see P1).
2. Forgets to stop Freqtrade container.
3. Both bots send buy orders on BTC/USD; one with old API key, one with new.
4. Coinbase fills both — net position is 2× intended.
5. Reconciliation cron (M4) flags mismatch on hourly run.
6. Slack alert at 23:32. Operator wakes up.
7. `/ops → KILL` revokes V4 key. Freqtrade keeps running with old key.
8. Operator manually closes the over-position on Coinbase web.
9. **Damage:** Slippage + fees on the 2× round-trip, plus any adverse market move during the 30 min. Estimate $300-$800 on a $19k account on a normal-vol day.

**Why this is handled:** Reconciliation runs hourly. The damage window is bounded.

**Why this is NOT fully handled:** If the cutover happens during a high-vol event, the 30-minute window could be expensive. **Mitigation: cutover ONLY during low-vol hours (Sat/Sun morning UTC), never during NY market open.**

### 4.3 · "Box dies in the middle of a trade, no backup is readable, no manual close happens for 8 hours"

**Sequence:**
1. NVMe failure, 03:00 local. Box won't boot.
2. Open position: 0.1 BTC long at $52,000.
3. BTC drops to $48,000 over 8 hours.
4. Operator wakes at 11:00, can't SSH, drives to box.
5. Pulls drive; tries USB-mount → unreadable.
6. Loads laptop, logs into Coinbase web, closes position manually at $48,200.
7. **Damage:** ~$380 loss on a $5,200 position. Box repair: ~$200-$500.

**Why this is handled (sort of):** Daily-loss limit doesn't help here (box is dead). But:
- The position size was capped at ~$5k per pair by existing risk_management. Loss is bounded.
- Manual close from web works.
- Cold-restore drill (O5) gets the bot back up within a day.

**Why this is NOT fully handled:** Long open positions during box downtime is the **single largest residual risk** in this design. Possible future mitigation: a **dead-man-switch** at the broker that auto-closes if no heartbeat from the box in 60 min. Coinbase doesn't natively support this; would need a heartbeat container on a $5/mo VPS that monitors and revokes the key. Out of scope for V4, but flag for V5.

---

## 5 · Operator wellbeing note

Read this section once per Friday during the sprint. It is not optional.

### Cadence expectations

- **Sprint length is a budget, not a deadline.** 4-6 weeks was the original plan. If it takes 10, that is fine. The Freqtrade stack continues to trade on auto; nothing breaks if V4 ships late.
- **No live-promotion commits after 22:00 local.** Self-enforced. Tired decisions on live trading are the single most common cause of preventable loss across the algo-trading community.
- **Friday checkpoint, 1 hour, no code.** Just read this doc, look at the week's actual hours spent, and answer honestly:
  1. Am I sleeping <6h on average?
  2. Am I irritated at the bot more than I'm proud of it?
  3. Did I have to redo work this week because of a mistake I'd never normally make?
  4. Did I skip exercise / family / partner time more than once?

  If 2 or more answers are yes → take the next week off the bot. Freqtrade keeps trading.

### Multiple jobs reality check

The operator has explicitly framed this as "one of multiple jobs." That implies a budget. Suggested:

- **Default cadence:** 8-12 hours / week on V4 work. More is fine if energy permits; less is fine if not.
- **One full off-week per month.** Pre-scheduled. Bot runs in dry-run during the off-week.
- **Cap of one major architectural pivot per month.** "Let's also rewrite the dashboard" mid-sprint is the path to burnout.

### Single-developer bus factor

Already addressed in P3 (EMERGENCY_STOP.md). Add to operator calendar:

- **Annual review with family member / trusted partner** — walk them through the 3-step shutdown. 1 hour.
- **Print EMERGENCY_STOP.md.** Paper survives Wi-Fi outages and dead laptops.

### "Is this making me money yet?" honesty check

Monthly numbers to track in the dashboard:

| Metric | This month | Goal |
|--------|------------|------|
| P&L (post-fees, post-slippage) | $— | $1,000 (operator goal) |
| Hours spent | — | <40 |
| Effective hourly | $P&L / hours | should beat $25/h or the side-project framing is wrong |

If after 3 months of running V4 live the effective hourly is below $25, the sprint is not paying off and the time would be better spent elsewhere. **This is not a failure — it's information.** Freqtrade can keep running on auto in the background while attention moves elsewhere.

### What "done" looks like

V4 is "done" when:

1. It has traded live for 60 consecutive days at full pair set.
2. Sharpe ≥ 1.0 over those 60 days (not the 1.5 ideal, but better than Freqtrade's known floor).
3. Operator has not needed to manually intervene for 30 days.
4. Freqtrade has been retired to `--profile legacy` and the legacy profile cron is green.

Until all four are true, V4 is not done, and "ship-it" pressure is misplaced.

---

## Appendix · Cross-references

- `[[project-trading-bot]]` — current stack architecture
- `[[project-drop-vllm]]` — Ollama-only inference decision (S1 mitigation source)
- `[[project-modelforge-decisions]]` — LoRA promote/rollback gate (O1 mitigation source)
- `[[feedback-no-heavy-containers-without-explicit-ok]]` — container safety rule (S1, S4 origin)
- `[[feedback-session-lessons]]` — UI > CLI, verify before claim, push-only-when-asked
- `[[reference-backup-system]]` — backup mechanics (S7, O5 mitigation source)
- `[[feedback-anthropic-routing]]` — cost-averse stance (F1 framing)

---

*End of 09-RISKS.md*
