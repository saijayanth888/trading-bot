# HANDOFF — Wave-2 · feat/v4-wave2-hermes

> **Build agent:** Hermes Layer-8 scheduler
> **Branch:** `feat/v4-wave2-hermes` (off main)
> **Layout:** ROOT — `src/quanta_core/hermes/`
> **Status:** all 7 modules implemented, 162 tests passing, ruff clean,
> mypy --strict clean, **90.13% coverage** (target ≥85%)
> **Commit:** `340d0eb` — *"Wave-2 Hermes: 7 Layer-8 modules + tests + pyproject"*

---

## 1 · What landed

```
src/quanta_core/
├── __init__.py
└── hermes/
    ├── __init__.py
    ├── _common.py             ← shared StateWriter / SlackNotifier / HermesConfig
    ├── _ledger.py             ← read-only Postgres helper (LedgerClient, TradeRow)
    ├── _ollama.py             ← thin Ollama HTTP wrapper (generate, ping, list_resident)
    ├── reflector.py           ← module 1
    ├── lora_promoter.py       ← module 2
    ├── weekly_publisher.py    ← module 3
    ├── briefer.py             ← module 4
    ├── post_mortem.py         ← module 5
    ├── healthcheck.py         ← module 6
    ├── gpu_yield_adapter.py   ← module 7
    └── templates/
        ├── __init__.py
        └── weekly_post.md.j2  ← Jinja2 template (doc 12 §2 spec)

tests/hermes/                  ← 162 tests, 90% coverage
├── conftest.py                ← FakeLedger / FakeOllama / FakeNotifier + state fixtures
├── test_common.py
├── test_ledger.py
├── test_ollama.py
├── test_reflector.py
├── test_lora_promoter.py
├── test_weekly_publisher.py
├── test_briefer.py
├── test_post_mortem.py
├── test_healthcheck.py
├── test_gpu_yield_adapter.py
└── test_layer8_boundary.py    ← static guard: no strategy/execution imports

pyproject.toml                 ← package + mypy --strict + ruff + pytest config
```

Total: **2,790 LOC** source · **1,986 LOC** tests · 52-line Jinja template.

## 2 · Module table

Each module exposes `def run(argv: list[str] | None = None) -> int` so the
invocation contract is `python -m quanta_core.hermes.<name>`.

| # | Module | Cadence (cron) | Inputs | Outputs | State file | Tests |
|---|---|---|---|---|---|---|
| 1 | `reflector` | `0 23 * * *` (≈23:30 ET nightly) | day's closed trades from ledger | per-trade 2-4 sentence markdown blocks; calls `hermes3:8b` via Ollama | appends `stocks/memory/decisions.md` (atomic) · writes `~/.quanta/state/last_reflection.json` | `test_reflector.py` (15 tests) |
| 2 | `lora_promoter` | `0 14 * * 0` (Sun 14:00 ET) | mf-api workflow id (env `MODELFORGE_WORKFLOW_ID`) | POSTs trigger, polls until terminal, reads `champions.json` | `~/.quanta/state/last_lora_promotion.json` | `test_lora_promoter.py` (16 tests) |
| 3 | `weekly_publisher` | `0 16 * * 5` (Fri 16:00 ET) | week's trades, lessons, adapter-promotion state | renders Jinja template; 3 advisory gates; **anti-cherry-pick** (no `--skip`, mandatory file creation, audit mode) | `docs/weekly/YYYY-WW.md` (atomic) · `~/.quanta/state/weekly_publish_state.json` | `test_weekly_publisher.py` (31 tests) |
| 4 | `briefer` | `30 8 * * 1` (Mon 08:30 ET) | regime + sentiment + calendar (HTTP or state fallback) · open positions | dashboard banner JSON + optional Slack post | `~/.quanta/state/briefing.json` | `test_briefer.py` (20 tests) |
| 5 | `post_mortem` | `0 10 * * 6` (Sat 10:00 ET) | last 7 days of trades; clusters losses by `(regime, exit_reason)`; calls `hermes3:70b` | appends `decisions.md` (review-only) | `~/.quanta/state/last_post_mortem.json` | `test_post_mortem.py` (9 tests) |
| 6 | `healthcheck` | `*/15 * * * *` | 5 probes (Ollama, Postgres, Alpaca, Coinbase, mf-api) | Slack alert on consecutive-failure threshold (default 3) | `~/.quanta/state/healthcheck_last.json` (with running `consecutive_failures`) | `test_healthcheck.py` (18 tests) |
| 7 | `gpu_yield_adapter` | `55 13 * * 0` (yield) + end-of-window (resume) | subprocess wrappers around `~/.hermes/scripts/gpu_yield_now.sh` and `gpu_resume.sh` | exit-code passthrough + Slack on failure | `~/.quanta/state/last_gpu_yield.json` and `last_gpu_resume.json` | `test_gpu_yield_adapter.py` (10 tests) |

## 3 · How to invoke (per doc §6.2 trampoline pattern)

Each shell wrapper is a 3-line trampoline:

```bash
#!/usr/bin/env bash
set -uo pipefail
cd /home/saijayanthai/Documents/trading-bot
exec python -m quanta_core.hermes.<name> "$@"
```

CLI flags worth knowing:

- `reflector` — `--day YYYY-MM-DD`, `--backfill N`, `--dry-run`.
- `lora_promoter` — `--workflow-id`, `--skip-trigger`, `--dry-run`.
- `weekly_publisher` — `--week current|previous|YYYY-WW`, `--force`, `--audit`.
- `briefer` — `--no-slack`.
- `post_mortem` — `--end YYYY-MM-DD`, `--dry-run`.
- `healthcheck` — `--no-alert`.
- `gpu_yield_adapter` — `yield|resume` (positional).

## 4 · Constraint compliance

| Constraint | Status | Evidence |
|---|---|---|
| Branch `feat/v4-wave2-hermes` | done | `git branch --show-current` |
| Layer-8 boundary (no `strategy`/`execution` imports) | enforced statically | `tests/hermes/test_layer8_boundary.py` |
| Fail-open on infra · fail-loud on data | done | `HermesError` raised only on bad data (e.g. ledger DSN missing returns 1 in reflector with explicit log); network errors return 0 and write degraded state |
| Atomic state writes (`tmp` + `os.replace`) | done | `StateWriter.write` + `StateWriter.append_text_atomic` (`_common.py`) |
| mypy --strict | clean | `mypy --strict src/quanta_core/hermes` — 12 files, 0 errors |
| ruff | clean | `ruff check src/quanta_core/hermes/ tests/hermes/` — all checks passed |
| Coverage ≥ 85% | **90.13%** | per-module range: post_mortem 99% / lora_promoter 85% (rest 88-99%) |
| ROOT layout | done | `src/quanta_core/hermes/` |
| NO push | held | local commits only |
| ~1500-2000 LOC + tests | 2,790 LOC src + 1,986 LOC tests | source on the high side; driven by 7 distinct modules + shared helpers |

## 5 · Test execution

```bash
# from repo root
PYTHONPATH=src:. python3 -m pytest tests/hermes/ -q
# → 162 passed in <1s

PYTHONPATH=src:. python3 -m pytest tests/hermes/ \
  --cov=src/quanta_core/hermes --cov-report=term
# → 90.13% (fail_under=85)

ruff check src/quanta_core/hermes/ tests/hermes/
# → All checks passed!

PYTHONPATH=src mypy --strict src/quanta_core/hermes/
# → Success: no issues found in 12 source files
```

## 6 · Env-var contract (config-over-hardcoded)

All knobs are env-var driven via `HermesConfig.load_config()` — defaults match
paper-mode reality on the operator's box.

| Env var | Default | Used by |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | reflector, post_mortem, healthcheck |
| `HERMES_REFLECTOR_MODEL` | `hermes3:8b` | reflector |
| `HERMES_POST_MORTEM_MODEL` | `hermes3:70b` | post_mortem |
| `POSTGRES_DSN` | (unset) | reflector, post_mortem, weekly_publisher, briefer, healthcheck |
| `MODELFORGE_API_URL` | `http://localhost:8000` | lora_promoter, briefer, healthcheck |
| `MODELFORGE_API_KEY` | (unset) | lora_promoter, healthcheck |
| `MODELFORGE_WORKFLOW_ID` | (unset — required for non-dry lora_promoter) | lora_promoter |
| `ALPACA_KEY_ID` / `ALPACA_SECRET_KEY` | (unset) | healthcheck |
| `COINBASE_API_KEY` | (unset) | healthcheck |
| `SLACK_WEBHOOK_URL`, `SLACK_CHANNEL` | (unset) | every module |
| `HERMES_HEALTH_FAIL_THRESHOLD` | `3` | healthcheck |
| `QUANTA_STATE_DIR` | `~/.quanta/state` | every module (tests pin this to `tmp_path`) |
| `QUANTA_REPO_ROOT` | (auto-detected via `pyproject.toml`) | reflector, post_mortem, weekly_publisher, briefer |
| `QUANTA_RUN_MODE` | `paper` | weekly_publisher (template `run_mode` field) |

## 7 · Anti-cherry-pick discipline (doc 12 §5)

| Rule | Where enforced |
|---|---|
| 1. Mandatory file creation | `weekly_publisher.run()` always calls `write_post()`; only `--force` is needed to overwrite |
| 2. Losing weeks render unchanged | `weekly_post.md.j2` has no conditional branches based on P&L sign; gate banner is the only conditional content |
| 3. No `--skip` flag | argparser exposes only `--week`, `--force`, `--audit`, `--audit-since`; the test `test_anti_cherry_pick_no_skip_flag` asserts the absence |
| 4. Missed-week detection | `weekly_publisher --audit` scans `docs/weekly/*.md` against an iso-week window; Slack alert on gap; `test_missed_weeks_detects_gap` covers it |
| 5. Tone parity | template uses no adjective branches; `test_render_post_losing_week_no_apology` greps for forbidden words |

## 8 · Quality gates (doc 12 §6) — advisory only

| Gate | Pass condition | On fail |
|---|---|---|
| `reconciliation` | `sum(week.trades.pnl) ≈ broker_delta ± $0.01` | banner prepended; render continues |
| `reflector_daily` | one reflector run per weekday in window | banner prepended; render continues |
| `risk_anchor` | `risk_governor.anchor == expected_starting_equity` | banner prepended; render continues |

All three are surfaced via `GateResult` so a future cron-side enforcement
mode can flip from advisory → blocking without code changes outside the
caller.

## 9 · Known gaps / TODOs

These are flagged in the source via inline comments rather than blocked:

- **Brokerage reconciliation hook.** `gate_reconciliation` accepts a
  `broker_delta: float | None`; today every caller passes `None`. Wiring this
  to the Alpaca / Coinbase EOD report is a `weekly_publisher` follow-up.
- **Reflector history file.** `weekly_publisher._reflector_days_from_state`
  reads only the *latest* `last_reflection.json` — so the `reflector_daily`
  gate currently sees one day at most. Promoting to a history JSONL is a
  one-line schema bump in `reflector.run_for_day`.
- **Calendar / regime / sentiment HTTP endpoints** in `briefer` fall back to
  state files; the actual mf-api `/api/calendar?week=next` endpoint is not
  contracted yet, so behaviour is "best effort" until wave-3 lands those.
- **Population of `lessons` on trade view** in `weekly_publisher._trade_view`
  is empty; needs joining `reflector_lessons` table once the schema settles.

## 10 · Files & commits

Branch: `feat/v4-wave2-hermes` (3 commits, oldest → newest):

| SHA | Message |
|---|---|
| `340d0eb` | Wave-2 Hermes: 7 Layer-8 modules + tests + pyproject |
| `2163459` | HANDOFF.md: smoke-test exit-code matrix + module + commit summary |
| `7368335` | ruff: fix import order in reflector.py |

## 11 · Pre-merge checklist (operator review)

```bash
# 1 · run the test suite from a clean checkout
PYTHONPATH=src:. python3 -m pytest tests/hermes/ -q

# 2 · coverage
PYTHONPATH=src:. python3 -m pytest tests/hermes/ --cov=src/quanta_core/hermes

# 3 · ruff
ruff check src/quanta_core/hermes/ tests/hermes/

# 4 · mypy
PYTHONPATH=src mypy --strict src/quanta_core/hermes/

# 5 · smoke-test each module with --dry-run / --no-slack where supported
PYTHONPATH=src python -m quanta_core.hermes.reflector --dry-run --day 2026-05-11
PYTHONPATH=src python -m quanta_core.hermes.lora_promoter --dry-run
PYTHONPATH=src python -m quanta_core.hermes.weekly_publisher --audit
PYTHONPATH=src python -m quanta_core.hermes.briefer --no-slack
PYTHONPATH=src python -m quanta_core.hermes.post_mortem --dry-run
PYTHONPATH=src python -m quanta_core.hermes.healthcheck --no-alert
```

Expected behaviour with no creds set on the operator's box (verified
2026-05-12):

| Module | Exit | Why |
|---|---|---|
| `reflector --dry-run` | **1** | fail-loud on missing ledger (data fault per §7) |
| `lora_promoter --dry-run` | 0 | dry-run bypasses mf-api entirely |
| `weekly_publisher --audit` | 0 | filesystem scan only — no creds needed |
| `briefer --no-slack` | 0 | falls back to state-file defaults when APIs unavailable |
| `post_mortem --dry-run` | 0 | empty-window OK, no LLM call needed |
| `healthcheck --no-alert` | 0 | per §7.6 healthcheck never returns non-zero |

Reflector's exit-1 is *intentional* — running it for real without a ledger
DSN should make noise, not silently pretend.
