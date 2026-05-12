# Nightly Reflector — `stage/12-nightly-reflector` HANDOFF

## What this branch ships

A nightly Hermes cron that turns each closed trade into a 2-4 sentence
post-mortem and promotes the matching `pending` line in
`stocks/memory/decisions.md` to its realised form. Reflections are written
by **Qwen3-30B-A3B-Instruct** (a MoE: 30B total / 3B active per token,
~19 GB VRAM at Q4_K_M, Apache-2.0) running locally via Ollama — zero $/call.

| Deliverable | Path |
|---|---|
| Cron script | `scripts/nightly_reflector.py` |
| Cron wrapper | `.hermes/scripts/nightly_reflector.sh` |
| Cron registration patch | `.hermes/cron/nightly_reflector.job.json` |
| Model-tier config | `stocks/shark/llm/model_tiers.json` (`reflector → qwen3:30b`) |
| Tests | `tests/test_nightly_reflector.py` (10 tests, all green) |

## Prerequisites

### One-time: pull the model (~19 GB)
```bash
ollama pull qwen3:30b
ollama list | grep qwen3   # confirm it's resident
```
Model card: <https://ollama.com/library/qwen3>. The 30B-A3B variant is the
small-active-parameter MoE — token throughput is closer to a 3B dense
model. Apache-2.0 license. Already in the public Ollama library; no
custom Modelfile needed.

### Sibling-branch dependencies (defensive — script handles missing)

| Sibling branch | Provides | Imported as |
|---|---|---|
| `stage/9-pydantic-schemas` | `chat_structured(provider, tier, system, user, schema, max_retries)` | `shark.llm.structured.chat_structured` |
| `stage/11-13-reflection-log` | `update_with_outcome(date, ticker, pnl_pct, alpha_pct, holding_days, reflection)` | `shark.memory.update_with_outcome` |

If either sibling has not been merged when this branch lands, the script
logs a clear `ImportError` line, posts a SOFT Slack warning, and exits 0
so the cron does not alarm. As soon as both siblings merge, the next
21:30 ET firing picks up the work.

## Install (one-time, after Ollama + siblings are ready)

```bash
# 1. Copy wrapper into Hermes' script directory
cp .hermes/scripts/nightly_reflector.sh ~/.hermes/scripts/
chmod +x ~/.hermes/scripts/nightly_reflector.sh

# 2. Append the registration JSON
python3 - <<'PY'
import json, pathlib, time
src = pathlib.Path('.hermes/cron/nightly_reflector.job.json').read_text()
new_job = {k: v for k, v in json.loads(src).items() if not k.startswith('_')}
jobs_path = pathlib.Path.home() / '.hermes' / 'cron' / 'jobs.json'
data = json.loads(jobs_path.read_text())
# Idempotency: replace the entry if its id is already present
data['jobs'] = [j for j in data['jobs'] if j.get('id') != new_job['id']]
data['jobs'].append(new_job)
data['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
backup = jobs_path.with_suffix(f'.json.backup-pre-reflector-{time.strftime("%Y%m%dT%H%M%SZ")}')
backup.write_text(jobs_path.read_text())
jobs_path.write_text(json.dumps(data, indent=2))
print('jobs.json patched; backup at', backup)
PY

# 3. Reload Hermes
hermes cron reload   # or `systemctl reload hermes` per install convention

# 4. Verify
hermes cron list | grep nightly_reflector
```

## Schedule and rationale

`30 21 * * 1-5` — 21:30 ET on weekdays.

- After **stocks** close (16:00 ET) plus a 5h buffer for late prints / corrections.
- After **crypto** evening settling (most volatility-driven trade closes
  cluster pre-21:00 ET on US-trader-led pairs).
- Before **midnight UTC** so it lands on the *correct* "yesterday" for
  the 00:00 UTC `daily_pnl_report` cron, which reports the same trade
  set.

⚠️ Note: `shark_kb_update` already runs at the same minute. The two
crons touch disjoint files (`decisions.md` vs the kb tree) and neither
holds long DB locks; concurrency is fine. If the operator sees disk-IO
contention on the spinning drive, bump the reflector to `35 21` —
single-line edit in `~/.hermes/cron/jobs.json`.

## The Reflector prompt — verbatim

### System
```
You are a trading analyst reviewing your own past decision now that the outcome is known.

Write exactly 2-4 sentences of plain prose answering, in this order:
1. Was the directional call correct? Cite the alpha figure as +X.X% or -X.X%.
2. Which part of the investment thesis held or failed?
3. One concrete lesson to apply to the next similar analysis.

Constraints:
- 2-4 sentences. Not 1, not 5.
- Plain prose. No bullet lists, no headers, no markdown.
- Cite the alpha figure at least once with one decimal place.
- Reference only tags and entities present in the trade ledger below. Do not invent strategies, regimes, or tickers.
```

### User template
```
Trade ledger:
- Ticker: {ticker}
- Entry tag: {entry_tag}
- Exit reason: {exit_reason}
- Entry: ${entry_price} on {open_date}
- Exit: ${exit_price} on {close_date}
- Holding: {holding_days} days
- P&L: {pnl_usd:+.2f} ({pnl_pct:+.2f}%)
- Alpha vs {benchmark}: {alpha_pct:+.2f}%
- Regime at entry: {regime_in}
- Regime at exit: {regime_out}
- Original thesis (if recorded): {thesis_or_NA}

Write the reflection.
```

Source: ported with attribution from
[TradingAgents](https://github.com/TauricResearch/TradingAgents)
(Apache-2.0). See the file-header comment in `scripts/nightly_reflector.py`.

### Schema
```python
class ReflectionOut(BaseModel):
    text: str = Field(..., min_length=80, max_length=600)
    alpha_cited: bool = Field(..., description="True if alpha appears in text")

    @model_validator(mode="after")
    def alpha_must_appear(self):
        if not self.alpha_cited:
            raise ValueError("reflection must cite the alpha figure")
        return self
```
After the LLM returns, the script ALSO checks deterministically with a
signed-percent regex (1dp tolerance). The model's `alpha_cited=True`
self-report is necessary but not sufficient — the regex is the
authority. Mismatch → one targeted retry that quotes the missing figure;
second mismatch → mark errored and skip that trade.

## Sample input → sample output

### Input row (from `trade_journal`)
```json
{
  "pair":         "AAPL",
  "opened_at":    "2026-05-08T13:30:00Z",
  "closed_at":    "2026-05-10T19:55:00Z",
  "entry_price":  178.20,
  "exit_price":   184.50,
  "pnl":          6.30,
  "pnl_pct":      3.54,
  "exit_reason":  "trail_stop",
  "regime":       "trending_up",
  "reasoning":    "breakout above 50d w/ vol confirmation"
}
```
Benchmark math: SPY moved +1.40% over the same window, so
`alpha = 3.54 - 1.40 = +2.14%`.

### User prompt fed to Qwen3-30B
```
Trade ledger:
- Ticker: AAPL
- Entry tag: breakout above 50d w/ vol confirmation
- Exit reason: trail_stop
- Entry: $178.2 on 2026-05-08
- Exit: $184.5 on 2026-05-10
- Holding: 2 days
- P&L: +6.30 (+3.54%)
- Alpha vs SPY: +2.14%
- Regime at entry: trending_up
- Regime at exit: trending_up
- Original thesis (if recorded): breakout above 50d w/ vol confirmation

Write the reflection.
```

### Expected reflection (illustrative)
```
The directional call was correct: AAPL beat the bench by +2.1% over the
two-day hold. The breakout-above-50d thesis held — vol confirmation on
entry was the actual trigger and the trail_stop took us out at strength
rather than on a reversal. Lesson: when an entry has both a clean level
break and confirming volume in a trending_up regime, give the trail
slightly more room (e.g. 7% instead of 5%) so a winner can run further.
```
This text is written into `stocks/memory/decisions.md` via
`shark.memory.update_with_outcome(...)` — that helper owns the exact
on-disk format (the reflector is format-agnostic).

## Eval plan — how to know reflections are useful

The reflector has no objective per-call accuracy metric. The KPI is
**downstream debate quality**: the next time a similar set-up reaches
the bull-vs-bear debate, does the debater reach for the prior reflection
and cite it correctly?

Concrete checkpoints over the first 4 weeks:
1. **Week 1** — eyeball 5 random reflections per night; flag any that
   invent strategies, regimes, or tickers not in the ledger
   (the prompt's "do not invent" rule). Target: 0 hallucinations / week.
2. **Week 2** — count how many reflections are cited by the next
   trading-day's `pre-execute` debate transcripts. Baseline expectation:
   ≥30% of debate rounds cite at least one prior reflection by week 2.
3. **Week 3** — A/B compare debate quality on tickers WITH vs WITHOUT a
   prior reflection on file. Quality measured by Sharpe of subsequent
   trades on those tickers. Target: positive delta, n>=20.
4. **Week 4** — fine-tune readiness check (see Phase B note below). If
   the citation rate from week 2 is healthy, the corpus of ~80-150
   reflections is large enough for a LoRA pass.

## Phase B (week 4) — model swap

When the fine-tuned `qwen3:30b` adapter is ready, swap with one edit:

```jsonc
// stocks/shark/llm/model_tiers.json
{
  "tiers": {
    "reflector": "qwen3:30b-shark-reflections-v1"  // ← was "qwen3:30b"
  }
}
```
No code change. No cron change. The next 21:30 firing uses the new
model. Roll back by reverting the JSON. This is the whole point of the
`tier="reflector"` indirection.

## Test command

```bash
# From repo root
python3 -m pytest tests/test_nightly_reflector.py -v
# Expected: 10 passed
```

The test suite covers (1) end-to-end three-trade write, (2) alpha-not-cited
retry-then-skip, (3) idempotency on already-reflected days, (4)
alpha-vs-benchmark math (mocked yfinance), and (5) sibling-import-missing
exits-zero / Ollama-unreachable exits-zero.

## Manual smoke test (after install)

```bash
# Yesterday's closes, dry — touches DB only, no LLM calls or writes
POSTGRES_HOST=localhost POSTGRES_PORT=5434 \
  python3 scripts/nightly_reflector.py --dry

# Backfill ALL closed trades (one-off catch-up; not in cron)
POSTGRES_HOST=localhost POSTGRES_PORT=5434 \
  python3 scripts/nightly_reflector.py --backfill

# Force a specific date
python3 scripts/nightly_reflector.py --date 2026-05-09
```

## Operational notes

- **Cron exit code is always 0.** Failures go to Slack via
  `SLACK_WEBHOOK_URL`. The Hermes scheduler will keep firing even after
  a series of failed runs — by design (matches the convention of the 8
  other deterministic crons).
- **Logs:** `stocks/memory/cron-reflector.log` (append-only,
  rotate manually if it grows unbounded — quarterly is plenty at <1KB / run).
- **State / idempotency** lives in `decisions.md` itself. There is no
  separate state file. Re-running the same day is safe.
- **Concurrent runs:** the wrapper does not take a lock. Cron firing
  every 24h on a single host means concurrency is impossible in practice.
  If the operator manually fires while a 21:30 run is in flight, the
  worst case is a duplicate `update_with_outcome` call — the sibling
  helper is responsible for handling that idempotently.
