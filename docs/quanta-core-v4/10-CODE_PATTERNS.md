# 10 — Code Patterns & Integration Contract

**Branch:** `feat/quanta-core-v4-design-r10`
**Status:** design — no code, no push
**Audience:** future build agents who will materialise `quanta-core/` into runnable code

This doc defines **HOW** the build agents will write `quanta-core/`. Every
section below is a hard rule. When a build agent is dispatched, the prompt
template (§6) will say: *"MUST follow code patterns from doc 10."*

The two halves of the doc:
- §1–§3: rules of the road for code (language, layout, async, errors, tests, CI)
- §4–§5: how `quanta-core/` slots into the **existing** stack (ModelForge, Hermes
  Agent, dashboard, TimescaleDB ledger, GPU reservation YAML) — what to port,
  what to build greenfield, and the API contracts that **must not** change.

---

## 1. Code patterns — "how we write it"

### 1.1 Language & toolchain (one pick per category, no fence-sitting)

| Concern              | Pick                          | Why this and not the alternatives |
|---------------------:|-------------------------------|-----------------------------------|
| Python               | **3.12+** (3.13 once on PyPy-free wheels) | TFT/torch wheels are 3.12-ready; `typing.Self`, `type X = ...` aliases, PEP 695 generics, perf wins. **Not** 3.11 (no PEP 695). **Not** 3.10 (EOL Oct 2026). |
| Package + venv mgmt  | **uv** (`uv pip`, `uv venv`, `uv sync`) | 10–100× faster than pip; lockfile native (`uv.lock`); cargo-style UX; works as drop-in for pip. **Not** poetry (slow resolver, opinionated PEPs). **Not** plain pip (no lock). |
| Build backend        | **hatchling** (via `hatch`)   | PEP 517/621 first-class, ships with `hatch` test matrix. **Not** setuptools (legacy `setup.py` baggage). **Not** poetry-core (couples build to poetry). |
| Project layout       | **`src/` layout**             | Prevents accidental imports of the in-tree package during tests; aligns with hatchling default. **Not** flat layout. |
| Linter + formatter   | **ruff** (lint **and** format)  | Replaces flake8 + isort + black + pyupgrade + pydocstyle in one binary; rules are config-driven. **Not** black + isort + flake8 (3 tools, 3 configs, 10× slower). |
| Type checker         | **mypy --strict**             | Strict mode catches `Any`-bleed at function boundaries; integrates with pre-commit. **Not** pyright (faster but rule surface drifts from PEPs; we want PEP-canonical). |
| Test runner          | **pytest** + **pytest-asyncio** | Async-native via `asyncio_mode=auto`; fixtures > unittest classes; rich plugin ecosystem. **Not** unittest. |
| Property tests       | **hypothesis**                | Required for risk math, order-state machines, anchor-file invariants. |
| HTTP replay          | **vcr.py** (or `pytest-recording`) | Coinbase / Alpaca / ModelForge integration tests must replay without keys. **Not** `responses` (manual fixtures rot). |
| Time mocking         | **freezegun**                 | `time.sleep` is banned in tests (§1.7); freezegun handles all clock advance. |
| Async runtime        | **asyncio + uvloop** (Linux)  | uvloop is a 2–4× drop-in on the event loop where I/O dominates. **Not** trio (split community, no FastAPI bridge). |
| HTTP client          | **httpx** (sync + async)      | Same client object as the existing dashboard. **Not** `requests` (sync only). **Not** `aiohttp` (no sync surface). |
| Web framework        | **FastAPI**                   | Dashboard already runs on FastAPI; quanta-core's `/api/ops/*` writers slot into the same app. |
| Config               | **TOML + pydantic-settings v2** | TOML is human-readable; pydantic v2 gives validated dataclass-like config with env-var override out of the box. |
| Logging              | **structlog → JSONL**         | One line per event, machine-readable, pipes into existing log pipeline. See §1.5. |
| DB driver            | **psycopg 3** (already pinned in `requirements-extra.txt`) | TimescaleDB ledger reuses the same driver as the existing stack. **Not** asyncpg (different connection-pool semantics). |
| Model serialisation  | **safetensors** (weights) + **JSON** (metadata) | Replaces the legacy torch-`save`/load round-trip in `tft_pickle.py`. Removes the IResolver-re-import workaround entirely. **Never** use Python's stdlib serialiser for untrusted artefacts — known RCE risk; the existing module exists only as a compatibility shim with FreqAI. |
| CI                   | **GitHub Actions**            | Matches what's already in `.github/` (where present); no Jenkins, no self-hosted unless GPU-required. |
| Pre-commit           | **pre-commit** (`.pre-commit-config.yaml`) | Runs ruff + mypy + pytest-fast on every commit. Build agents must add their hook before pushing. |

### 1.2 Project layout (the actual tree)

```
quanta-core/
├── pyproject.toml                  # PEP 621 + hatchling + ruff + mypy + pytest config in ONE file
├── uv.lock                         # committed
├── README.md                       # 1-page; full design lives in docs/quanta-core-v4/
├── .pre-commit-config.yaml
├── .github/
│   └── workflows/
│       └── ci.yml                  # lint → mypy → pytest → coverage gate
├── src/
│   └── quanta_core/
│       ├── __init__.py             # __version__ only
│       ├── py.typed                # PEP 561 marker
│       ├── config/
│       │   ├── __init__.py
│       │   ├── schema.py           # pydantic-settings BaseSettings tree
│       │   └── loader.py           # TOML → schema → validated
│       ├── live/
│       │   └── engine.py           # greenfield (§5)
│       ├── backtest/
│       │   └── engine.py           # greenfield (§5)
│       ├── strategy/
│       │   └── mean_rev_tft.py     # port of FreqAIMeanRevV1 (§4)
│       ├── models/
│       │   ├── tft.py              # port of TFTModel (§4)
│       │   └── registry.py         # greenfield (§5)
│       ├── risk/
│       │   ├── governor.py         # port of risk_governor (§4)
│       │   └── monte_carlo.py      # greenfield (§5)
│       ├── execution/
│       │   ├── engine.py           # port of execution_engine (§4)
│       │   └── exit_manager.py     # port of stocks/shark exit_manager (§4)
│       ├── ownership.py            # NEW (no existing source — see §4 footnote)
│       ├── agents/
│       │   └── debate.py           # greenfield (§5)
│       ├── lora/
│       │   └── online.py           # greenfield (§5)
│       ├── ledger/
│       │   ├── reader.py           # read-only TimescaleDB views
│       │   └── writer.py           # trades + fills + decisions
│       ├── ops/
│       │   ├── routes.py           # FastAPI router — mirrors /api/ops/*
│       │   └── state_files.py      # JSON state for Hermes crons
│       ├── logging.py              # structlog config
│       └── cli.py                  # `quanta` entrypoint
└── tests/
    ├── conftest.py                 # mirrors current pattern: tmp anchor paths, frozen clocks
    ├── unit/
    ├── integration/                # uses vcr.py cassettes
    └── property/                   # hypothesis
```

`pyproject.toml` is the **single** config file. ruff, mypy, pytest, coverage,
hatchling, and pydantic-settings all read from it. **No** `setup.cfg`, **no**
`tox.ini`, **no** `pytest.ini`.

### 1.3 Async pattern

- **asyncio everywhere.** The live engine, the WebSocket consumers, every
  outbound HTTP call, the ledger writer, and the FastAPI ops routes are all
  `async def`. Synchronous code is reserved for: CLI entrypoints, model
  training inner loops (CPU/GPU bound, runs in `loop.run_in_executor`), and
  test helpers.
- **No thread-per-connection.** The current `execution_engine.py` uses
  `threading.Timer` for order timeouts — that pattern is **banned**. Use
  `asyncio.wait_for(..., timeout=...)` or `asyncio.TaskGroup` (3.11+).
- **One event loop per process.** Use `asyncio.Runner(loop_factory=uvloop.new_event_loop).run(main())`
  to construct the loop with uvloop on Linux. Do **not** call
  `asyncio.run()` repeatedly.
- **Cancellation is first-class.** Every long-running task accepts a
  `cancel_scope: asyncio.Event` or uses `asyncio.CancelledError` cleanup. No
  daemon threads. The build agent must verify graceful SIGTERM (10s budget) in
  an integration test.
- **No `time.sleep` in async code.** `await asyncio.sleep(...)` only.

### 1.4 Error handling

- **No bare `except:`.** Always catch a concrete exception or
  `Exception as e`. Bare `except` will fail ruff (rule `E722`).
- **No silent swallow.** Every `except` either re-raises, logs at
  `WARNING+`, or returns a structured `Result` (a typed dataclass with
  `ok: bool, error: str | None`). Pattern:

  ```python
  try:
      reply = await client.place(order)
  except CoinbaseRateLimit as e:
      logger.warning("coinbase_rate_limit", retry_in=e.retry_after, order_id=order.client_id)
      raise  # bubble for caller's retry-with-backoff
  except CoinbaseAPIError as e:
      logger.error("coinbase_api_error", error=str(e), order_id=order.client_id)
      return PlacementResult.failed(reason=f"api_error:{e.code}")
  ```

- **`raise ... from e`** every time a re-raise wraps a different exception
  type (mypy + ruff `B904`).
- **Exceptions are typed.** Define a module-level base class
  (`QuantaError(Exception)`) and subclass per failure mode. No `RuntimeError`
  with a string.
- **Operator-visible failures fail loudly.** Risk-gate violations, kill-switch
  trips, and missing config keys raise immediately at startup — they do **not**
  degrade silently to defaults. (Pattern lifted from current
  `risk_governor.py` `_resolve_anchor_path`.)

### 1.5 Logging

- **structlog** configured once in `quanta_core/logging.py`, JSON renderer in
  prod, console renderer when `QUANTA_LOG_FORMAT=console`.
- **Every log line carries**: `event` (snake_case verb noun), `subsystem`
  (matches package path), and `correlation_id` (UUIDv4 per trade / per request).
- **stdlib `logging` is bound to structlog** via
  `structlog.stdlib.LoggerFactory` so third-party libs (FastAPI, httpx, ccxt,
  freqtrade legacy) emit the same JSONL shape.
- **JSONL output goes to** `~/.quanta/logs/quanta-core.jsonl` (rotated by
  size, 5 × 50 MB). The existing log pipeline (Hermes nightly reflector reads
  `user_data/logs/*.jsonl`) gets one new path appended; Hermes config update is
  in §2.2.
- **No `print()`**, not even in CLI scripts. ruff `T201` rules enforce.
- **PII / API-key redaction** happens in a `structlog` processor before the
  renderer; build agents must add their secret pattern to the redactor.

### 1.6 Config

- **One TOML** at `~/.quanta/config.toml` (override path via
  `QUANTA_CONFIG_PATH`).
- **pydantic-settings v2** `BaseSettings` tree defines the schema in
  `quanta_core/config/schema.py`. Every field has a default OR is required;
  no `Optional` without a documented null semantic.
- **Env-var override** is automatic: `QUANTA_RISK__MAX_DRAWDOWN_PCT=0.05`
  beats the TOML value (pydantic-settings `env_nested_delimiter="__"`).
- **No `os.environ.get(...)` outside `config/loader.py`.** Build agents
  importing env vars inline will fail review. (The current codebase has 40+
  ad-hoc `os.environ.get` calls in `exit_manager.py`, `execution_engine.py`,
  etc. — those become typed config fields during the port.)
- **Config reload** is supported via `SIGHUP` (re-reads TOML, re-validates,
  swaps an `asyncio.Lock`-protected reference). Pattern matches the current
  `risk_governor`'s "edit config.json, no rebuild" requirement.

### 1.7 Idempotency

- **Every external write is idempotent OR explicitly marked one-shot.**
- Pattern A (idempotent): caller passes a `client_order_id: UUIDv4`. Repeated
  calls with the same ID return the same server-side state, never duplicate.
  Used for: order placement, ledger writes, ModelForge sample uploads.
- Pattern B (one-shot): function name ends in `_once` and the call site holds
  an in-process lock or a filesystem flag. Used for: nightly retrain kickoffs,
  GPU lease acquisitions, cron-triggered state snapshots.
- **No silent retries on non-idempotent endpoints.** Retries are explicit,
  bounded (max 3), exponential, and **only** wrap endpoints declared
  idempotent in their docstring. (Current `execution_engine.py` has this
  right; we keep the contract.)

### 1.8 Testing

- **pytest + pytest-asyncio.** `asyncio_mode = "auto"` in pyproject.
- **No `time.sleep` in tests.** Use `freezegun.freeze_time` + manual clock
  advance. Async tests use `asyncio.sleep(0)` to yield, not real wait.
- **hypothesis property tests are mandatory for**: risk-governor invariants
  (drawdown anchor monotonicity, correlation matrix PSD), order-state machine
  (PLACE → PARTIAL → FILL | CANCEL), backtest-engine determinism (same seed
  + same data ⇒ same trades).
- **vcr.py cassettes** in `tests/cassettes/`. Cassettes are committed,
  re-recorded only when the upstream API changes (mark cassette path in
  commit message).
- **Coverage gate: 85%** on `src/quanta_core/`, **95%** on `risk/` and
  `ledger/`. CI fails below.
- **Fixtures isolate state.** The current `conftest.py` already isolates
  `RISK_GOVERNOR_ANCHORS_PATH` per test (audit 2026-05-12 High #9). We carry
  this pattern: every test that touches durable state gets a `tmp_path`
  fixture; nothing writes to the operator's home dir.
- **One integration test per ported module verifies wire-compatibility**: feed
  a recorded production event through the ported module and assert the output
  is byte-identical to the legacy implementation's recorded output. This is
  the only way the API-contract guarantees in §3 can be machine-checked.

### 1.9 CI patterns

`.github/workflows/ci.yml` — sequential gates, fail fast:

```
1. setup uv  (cached)
2. uv sync --frozen
3. ruff check  + ruff format --check        # ~3s
4. mypy --strict src/                       # ~15s
5. pytest -m "not slow" --cov=src/quanta_core --cov-fail-under=85  # ~60s
6. pytest -m slow                            # vcr replays, e2e — ~3 min
7. coverage report on risk/ + ledger/ ≥ 95%
```

Pre-commit hooks mirror gates 3–5 (fast path only) so contributors catch
failures locally before push.

### 1.10 Module + function conventions

- **Module names are spelled-out.** `mean_rev_tft.py`, not `mrtft.py`.
  `execution_engine.py`, not `exec_eng.py`. `subsystem_ownership.py`, not
  `subs_own.py`. Abbreviations allowed only for industry terms already in the
  domain glossary (TFT, RSI, ATR, ETF, LoRA).
- **Verbs are consistent**: `get_*` reads, `list_*` returns a sequence,
  `set_*` writes, `create_*` allocates, `cancel_*` undoes, `close_*` finalises.
  No `fetch_`, no `retrieve_`, no `do_`. `compute_` is allowed for pure math.
- **Function size**: target **<50 lines**, hard cap **100 lines** (ruff
  `C901` complexity gate + a custom check). The current `risk_governor.py`
  is 759 lines across ~12 functions — that's the upper bound on density we
  accept. Anything denser must be split.
- **Public surface is explicit.** Every package `__init__.py` has an
  `__all__`. Symbols not in `__all__` are internal and may be renamed
  without notice.

### 1.11 Docstrings

- **NumPy style**, every public function/class.
- **Required sections**: short description (one line), Parameters, Returns,
  Raises (if any).
- **Optional but encouraged**: Examples (doctest-runnable),
  Notes (cross-references to design docs).
- Module-level docstring is required and explains the module's role in the
  pipeline (mirrors the pattern at the top of `risk_governor.py`,
  `TFTModel.py`, `execution_engine.py` — those are the canonical examples).

---

## 2. Integration with the existing stack

`quanta-core/` does **not** replace the operator's existing services. It
plugs into them.

### 2.1 ModelForge

**Direction**: `quanta-core` is a **producer** for training data and a
**consumer** for promoted adapters.

- **Out**: quanta-core writes RLRO (reinforcement-learning rollout
  observations) training samples to `~/.dgx-train/raw/<role>/<YYYYMMDD>.jsonl`
  — the **same path** `scripts/modelforge_ingest.py` already reads. The
  on-disk schema does **not** change; quanta-core just becomes another
  producer alongside the existing `decisions.md` + `llm-calls.jsonl` writers.
  See [`docs/MODELFORGE_DATA_PIPELINE.md`](../MODELFORGE_DATA_PIPELINE.md) for
  the canonical format.
- **In**: quanta-core polls `/api/forge/adapter_promotions` (existing
  ModelForge endpoint) and **hot-reloads** the affected adapter via
  `quanta_core.models.registry`. Hot-reload is atomic (build new model,
  swap reference under `asyncio.Lock`, drain old model).
- **Build-agent action**: implement `quanta_core/lora/online.py` as the
  write side and `quanta_core/models/registry.py` as the read side. Reuse
  the auth scheme already wired into the dashboard
  (`MODELFORGE_API_URL` + `MODELFORGE_API_KEY` env vars; see
  `docker-compose.yml`).

### 2.2 Hermes Agent (crons + Slack reporting)

**Direction**: Hermes stays in charge of the cron timetable and Slack alerts.
quanta-core writes JSON state files that Hermes reads.

- quanta-core writes:
  - `~/.quanta/state/risk_state.json` — every minute, atomic-rename
  - `~/.quanta/state/positions.json` — every fill event
  - `~/.quanta/state/regime.json` — every regime tick
  - `~/.quanta/state/last_decision.json` — every decision
- Hermes existing cron jobs (`.hermes/cron/*.job.json`) point at scripts
  under `.hermes/scripts/` that `cat` these JSON files and forward to
  Slack/Telegram via existing notifier modules
  (`user_data/modules/slack_alerts.py`, `telegram_alerts.py`).
- **No new cron jobs from quanta-core.** All scheduling stays in Hermes.
  If a build agent needs a new cron, the prompt template instructs them to
  add it to `.hermes/cron/` as a new job spec — **never** to spawn a
  `threading.Timer` or APScheduler instance inside quanta-core.
- The nightly reflector (`.hermes/scripts/nightly_reflector.sh`) gains one
  extra input path: `~/.quanta/logs/quanta-core.jsonl`. Build agents update
  the reflector script as a coordinated change.

### 2.3 Existing dashboard (port 8081, FastAPI + React SPA)

**Direction**: the dashboard is the **UI** — quanta-core swaps the backend
behind the existing `/api/ops/*` endpoints without changing a single URL,
response shape, or status code.

- All endpoints currently exposed by
  `user_data/dashboard/ops_routes.py` (~40 routes — `/api/ops/services`,
  `/api/ops/training`, `/api/ops/regime`, `/api/ops/trades_risk`,
  `/api/ops/pause`, `/api/ops/risk_gates`, `/api/ops/explainability/...`,
  `/api/ops/timeline/...`, `/api/ops/combined_portfolio`,
  `/api/ops/circuit_breakers`, `/api/ops/llm_stats`, …) must keep their
  **exact** response JSON shape.
- `quanta_core/ops/routes.py` declares a FastAPI `APIRouter` that mounts at
  `/api/ops` and **passes the API-contract integration tests** that capture
  the legacy responses (one cassette per route, recorded against the
  current production dashboard before the cutover).
- The dashboard's frontend code (`user_data/dashboard/static/js/*`)
  changes **zero lines** during the cutover. Static asset cache-busting
  (`?v=...`) is only required for new endpoints, not the swap.
- The dashboard's writers (`POST /api/ops/pause`, `POST /api/ops/resume`,
  `POST /api/ops/regime_config`, `POST /api/ops/risk_gates`,
  `POST /api/ops/rebalance`) keep their `require_mcp_key` dependency. The
  auth dependency is **imported from** the existing dashboard module —
  quanta-core does not re-implement it.

### 2.4 TimescaleDB ledger

**Direction**: TimescaleDB is the **single source of truth** for trades,
fills, and decisions. quanta-core writes; dashboard reads. No file-based
ledgers.

- Existing schema (in `postgres/init/*.sql`) defines the hypertables for
  `trades`, `fills`, `decisions`, `regime_ticks`. Build agents **do not**
  alter these schemas during the cutover. New columns are additive and
  shipped as a numbered migration in `postgres/init/0NN_*.sql`.
- Connection string and pool config are read from the same env vars the
  freqtrade container already uses (`POSTGRES_HOST`, `POSTGRES_USER`,
  `POSTGRES_PASSWORD`, `POSTGRES_DB`).
- Writes go through `quanta_core.ledger.writer.LedgerWriter`, which
  guarantees idempotency on `(client_order_id, event_seq)`.
- Reads go through `quanta_core.ledger.reader.LedgerReader`. The dashboard
  continues to read directly via its existing
  `user_data/dashboard/ops_db.py` module — they share the same DB, not
  the same Python object.

### 2.5 GPU reservation YAML

**Direction**: respect the existing schedule. Do **not** invent a new one.

- Live config lives at `~/.hermes/config/gpu_reservation.yaml` (canonical
  example committed at `user_data/config/gpu_reservation.example.yaml`).
- `quanta_core/lora/online.py` (the only GPU-heavy quanta-core module) calls
  `.hermes/scripts/gpu_gate.sh --caller quanta-core-lora` before each training
  kickoff. The gate exits non-zero during reserved windows; the LoRA loop
  catches the non-zero exit, logs, and waits for the next pre-drain
  notification.
- The Sunday 14:00 ET ModelForge weekly LoRA window blocks quanta-core's
  online training automatically — no quanta-core config change needed.
- **Phase 2** (future): swap the YAML read for a live `/api/forge/gpu_lease`
  call. Doc 10 is intentionally silent on Phase 2 — that becomes its own
  design doc.

---

## 3. Backwards-compatibility checklist

For every existing endpoint quanta-core consumes or replaces, this is the
migration contract.

| Endpoint                                  | Consumer        | Change      | Test |
|-------------------------------------------|-----------------|-------------|------|
| `GET /api/ops/services`                   | dashboard SPA   | swap backend, same JSON | cassette `tests/integration/ops_services.yaml` |
| `GET /api/ops/uptime`                     | dashboard SPA   | swap backend, same JSON | cassette |
| `GET /api/ops/training`                   | dashboard SPA   | swap backend, same JSON | cassette |
| `GET /api/ops/training_health`            | dashboard SPA   | swap backend, same JSON | cassette |
| `GET /api/ops/regime`                     | dashboard SPA   | swap backend, same JSON | cassette |
| `GET /api/ops/sentiment`                  | dashboard SPA   | swap backend, same JSON | cassette |
| `GET /api/ops/trades_risk`                | dashboard SPA   | swap backend, same JSON | cassette |
| `POST /api/ops/pause`                     | dashboard SPA   | swap backend, same body + status; `require_mcp_key` preserved | cassette |
| `POST /api/ops/resume`                    | dashboard SPA   | swap backend, same body + status; `require_mcp_key` preserved | cassette |
| `GET /api/ops/regime_config`              | dashboard SPA   | swap backend, same JSON | cassette |
| `POST /api/ops/regime_config`             | dashboard SPA   | swap backend, same body + status; `require_mcp_key` preserved | cassette |
| `GET /api/ops/risk_gates`                 | dashboard SPA   | swap backend, same JSON | cassette |
| `POST /api/ops/risk_gates`                | dashboard SPA   | swap backend, same body + status; `require_mcp_key` preserved | cassette |
| `GET /api/ops/sparklines`                 | dashboard SPA   | swap backend, same JSON | cassette |
| `GET /api/ops/readiness`                  | dashboard SPA   | swap backend, same JSON | cassette |
| `GET /api/ops/explainability/{base}/{quote}` | dashboard SPA | swap backend, same JSON | cassette |
| `GET /api/ops/timeline/{base}/{quote}`    | dashboard SPA   | swap backend, same JSON | cassette |
| `GET /api/ops/combined_portfolio`         | dashboard SPA   | swap backend, same JSON | cassette |
| `GET /api/ops/circuit_breakers`           | dashboard SPA   | swap backend, same JSON | cassette |
| `GET /api/ops/llm_stats`                  | dashboard SPA   | swap backend, same JSON | cassette |
| `GET /api/ops/stocks`                     | dashboard SPA   | swap backend, same JSON | cassette |
| `GET /api/ops/shark_briefing`             | dashboard SPA   | swap backend, same JSON | cassette |
| `GET /api/ops/stocks_ml`                  | dashboard SPA   | swap backend, same JSON | cassette |
| `GET /api/ops/stock_regime`               | dashboard SPA   | swap backend, same JSON | cassette |
| `GET /api/ops/shark_override_health`      | dashboard SPA   | swap backend, same JSON | cassette |
| `~/.dgx-train/raw/<role>/*.jsonl`         | ModelForge ingest | new producer | integration: assert curate stage accepts the new lines |
| `/api/forge/adapter_promotions`           | ModelForge      | new consumer | vcr cassette |
| `.hermes/scripts/gpu_gate.sh`             | quanta-core LoRA loop | new caller (`--caller quanta-core-lora`) | shell test |
| `~/.quanta/state/*.json`                  | Hermes scripts  | new producer | hermes cron `state_reporter` reads + sanity-checks |

**Cutover rule**: a route flips from legacy → quanta-core **only after** its
cassette test passes byte-for-byte against the legacy production response.
Cutover is per-route, not per-module, so we can roll back any single
endpoint without redeploying the bundle.

---

## 4. Reuse map (existing → new, concrete)

| Existing path                                                 | New location in `quanta-core/`                            | Adapter changes |
|---------------------------------------------------------------|-----------------------------------------------------------|-----------------|
| `user_data/strategies/FreqAIMeanRevV1.py` (2132 lines)        | `src/quanta_core/strategy/mean_rev_tft.py`                | (1) drop `IStrategy` inheritance — replaced by `quanta_core.live.engine.Strategy` ABC. (2) `populate_indicators` becomes pure: no `metadata` dict, no self-mutating attrs. (3) signal output is a typed `Signal` dataclass, not a DataFrame column. (4) split into `signals.py`, `entries.py`, `exits.py` if it grows past 800 lines after the de-Freqtrade-ification. |
| `user_data/freqaimodels/TFTModel.py` (829 lines) + `tft_architecture.py` (kept) + `tft_pickle.py` (716 lines, **deleted**) | `src/quanta_core/models/tft.py` (+ `tft_architecture.py` co-located) | (1) drop `BasePyTorchClassifier` inheritance — registry-managed model now. (2) **delete the legacy `tft_pickle.py` shim entirely.** It exists only because FreqAI's `IResolver` re-imports the model file via `spec_from_file_location`, which breaks class identity at serialisation time. quanta-core controls its own model loading, so the workaround is moot. Replace with `safetensors.torch.save_file` for weights + a sidecar `<model>.json` for metadata (epoch, hidden_size, training_window). No Python-stdlib serialisation of model artefacts under any circumstance. (3) feature pipeline runs via `quanta_core.models.registry.FeaturePipeline` not `FreqaiDataKitchen`. (4) GPU memory budget comment in module docstring stays — operator pinned it. |
| `user_data/modules/risk_governor.py` (759 lines)              | `src/quanta_core/risk/governor.py`                        | (1) anchor-file path stays `~/.quanta/state/risk_governor_anchors.json` (renamed from `user_data/state/`). (2) backtest-vs-live runmode detection swaps from "Freqtrade runmode" to `quanta_core.config.RunMode` enum. (3) keep the existing fix where backtest uses a `/tmp` transient anchor (bug 2 in the module docstring). (4) keep the dedup-Series-index fix in `_pearson_returns` (`fix(risk_governor): dedupe Series index`). (5) all 40+ `os.environ.get` calls migrate to `quanta_core.config.schema.RiskConfig` fields. |
| `user_data/modules/execution_engine.py` (664 lines)           | `src/quanta_core/execution/engine.py`                     | (1) `threading.Timer` order-timeout becomes `asyncio.wait_for`. (2) `_setup_execution_logger` migrates to structlog config in `quanta_core/logging.py` — separate `audit` logger name preserved. (3) keep dry-run synthetic-order pattern verbatim — tests depend on its determinism. (4) idempotency key migrates from `uuid.uuid4()` ad-hoc to `client_order_id: UUID` typed field on the request dataclass. |
| `stocks/shark/execution/exit_manager.py` (282 lines)          | `src/quanta_core/execution/exit_manager.py`               | (1) all `os.environ.get(...)` constants (HARD_STOP_PCT, TIME_DECAY_DAYS, …) become typed fields on `quanta_core.config.schema.ExitConfig`. (2) hard-coded magic numbers (`TIER1_R_MULTIPLE = 1.0`) stay in the module as named constants — they're domain rules, not config. (3) function signature `evaluate_exits(positions, trade_log, regime)` keeps its shape so the port is mechanical. |
| `stocks/shared/subsystem_ownership.py` (file does **not** currently exist) | `src/quanta_core/ownership.py` | **NEW MODULE.** The operator's spec references this file but no such file exists in the tree (verified 2026-05-12 against the worktree). Build agents treat this as greenfield: it owns the "which subsystem owns which symbol / which position" registry. Spec to be drafted in doc 11 (or a follow-up r11). |
| `stocks/shark/execution/guardrails.py`                        | merged into `src/quanta_core/risk/governor.py`            | guardrails are pre-trade gates and belong under risk. |
| `stocks/shark/execution/stops.py`                             | merged into `src/quanta_core/execution/exit_manager.py`   | stops are an exit reason. |
| `stocks/shark/execution/orders.py`                            | merged into `src/quanta_core/execution/engine.py`         | order placement is one engine. |
| `stocks/shark/execution/position_sizer.py`                    | `src/quanta_core/risk/sizer.py` (new file)                | Kelly + fixed-fraction sizers live next to the governor. |
| `user_data/modules/regime_detector.py`                        | `src/quanta_core/regime/detector.py`                      | HMM + heuristic regime; consumed by strategy + risk. |
| `user_data/modules/unified_risk.py`                           | folded into `src/quanta_core/risk/governor.py`            | dedupe with risk_governor; one source of truth. |
| `user_data/modules/meta_agent.py`                             | `src/quanta_core/agents/meta.py`                          | port as-is for now; will be eclipsed by `agents/debate.py` in §5. |
| `user_data/modules/trade_journal.py`                          | folded into `src/quanta_core/ledger/writer.py`            | journal is just a ledger write. |
| `user_data/modules/news_aggregator.py`                        | `src/quanta_core/data/news.py`                            | data source, not a strategy concern. |
| `user_data/modules/sentiment_engine.py` + `sentiment_prompts.py` | `src/quanta_core/agents/sentiment.py`                  | one agent, two files collapsed. |
| `user_data/modules/onchain_signals.py`                        | `src/quanta_core/data/onchain.py`                         | data source. |
| `user_data/modules/notifier.py` + `slack_alerts.py` + `telegram_alerts.py` | NOT ported — Hermes owns alerting (§2.2)         | quanta-core writes JSON state; Hermes reads + alerts. |
| `user_data/freqaimodels/tft_architecture.py`                  | `src/quanta_core/models/tft_architecture.py`              | unchanged code; co-located with `tft.py`. |

**Files explicitly NOT ported** (out of scope; the existing modules keep
running where they are):
- `user_data/modules/db.py` — Freqtrade SQLite path; quanta-core uses Timescale directly.
- `user_data/modules/drl_ensemble.py` — research code, not in the live path.
- `user_data/modules/ept_evolution.py` — paused cron (HANDOFF 2026-05-12).
- `user_data/modules/ensemble_voter.py` — superseded by `agents/debate.py`.
- `user_data/modules/monitoring_mixin.py` — replaced by structlog config.

---

## 5. Greenfield-only modules

These are NEW. No existing code to port; the build agent writes them from
the design docs only.

| Module                             | Owns                                                                                 | Depends on                          |
|------------------------------------|--------------------------------------------------------------------------------------|-------------------------------------|
| `quanta_core.live.engine`          | WebSocket consumer (Coinbase + Alpaca + market-data feeds); event loop owner; signal dispatcher; tick-rate pacing | strategy, risk, execution, ledger |
| `quanta_core.backtest.engine`      | Deterministic replay engine; reads ledger snapshots + historical candles; runs same `Strategy` ABC as live | strategy, risk (sim mode), ledger reader |
| `quanta_core.models.registry`      | Multi-model resident pool (TFT, LoRA-adapted Hermes 8B/70B); hot-reload via atomic swap; ModelForge promotion poller | config, lora.online              |
| `quanta_core.risk.monte_carlo`     | Forward Monte Carlo on the open book + pending signals; produces VaR / CVaR / probability-of-stop-out | risk.governor, ledger reader |
| `quanta_core.agents.debate`        | Parallel orchestrator: bull/bear/arbiter LLM calls in parallel, structured-output merge, decision logging to ledger | models.registry, ledger writer |
| `quanta_core.lora.online`          | Continuous LoRA training loop; respects GPU reservation YAML; writes adapters to ModelForge for promotion | gpu gate, config |
| `quanta_core.ownership`            | Symbol-to-subsystem ownership registry (see §4 footnote — file referenced in spec but does not exist) | config |
| `quanta_core.ops.state_files`      | Atomic-rename JSON writers for Hermes consumption (§2.2)                              | config |
| `quanta_core.config.loader`        | TOML + env-var resolver with `SIGHUP` reload                                          | (none) |

Each greenfield module ships with a doc-10-compliant test suite (unit +
hypothesis + at least one integration test against a cassette or a real
local service in CI).

---

## 6. Build-agent prompt template

Below is the actual prompt a build agent will receive when it's dispatched
to materialise one of the modules listed in §4 or §5. The operator can
read this verbatim to see exactly what each build agent will be told.

```
You are a build agent for the quanta-core project. Your task is to write
the code for ONE module. Read the spec, follow the rules, write the code,
write the tests, run the gates, and stop.

# Module
{{MODULE_PATH}}            (e.g. src/quanta_core/risk/governor.py)
{{MODULE_KIND}}            (port | greenfield)
{{SOURCE_PATH}}            (only set for port; e.g. user_data/modules/risk_governor.py)

# Design docs you MUST read first
- docs/quanta-core-v4/10-CODE_PATTERNS.md           ← this doc — code rules + integration contract
- docs/quanta-core-v4/{{MODULE_DOC}}                 ← the per-module design doc
- For ports: read {{SOURCE_PATH}} in full and note the adapter changes
  listed in doc 10 §4 for your module.

# Hard rules (lifted from doc 10)
- Python 3.12+, type hints everywhere, mypy --strict must pass.
- ruff lint + format must pass; no `print`, no bare `except`, no `time.sleep`
  in async code or tests.
- Async-first. No threading.Timer, no daemon threads, no thread-per-conn.
- Idempotent external writes (client_order_id pattern) OR `_once` suffix.
- structlog for all logging; no stdlib `logging.getLogger(...).info(...)`
  with a free-text string.
- pydantic-settings v2 for config; no `os.environ.get(...)` outside
  `quanta_core/config/loader.py`.
- Function ≤100 lines, target ≤50.
- NumPy-style docstrings on every public function/class.
- Tests: pytest + pytest-asyncio + hypothesis (where invariants exist) +
  vcr.py (where external HTTP is touched). Coverage ≥85% on the module
  (≥95% for risk/ and ledger/).
- No new cron jobs in quanta-core. Schedule via Hermes (doc 10 §2.2).
- Dashboard contract: if you touch /api/ops/*, the cassette test in
  tests/integration/ops_*.yaml MUST pass byte-for-byte (doc 10 §3).
- Model artefacts: safetensors for weights, JSON for metadata. Never use
  Python's stdlib serialiser for model files — RCE risk on untrusted load.

# What you may NOT do
- Push the branch.
- Change docker-compose.yml, postgres/init/*.sql, .hermes/cron/*.job.json,
  or user_data/dashboard/* without explicit operator approval.
- Add a new top-level dependency without updating pyproject.toml + uv.lock
  in the same commit.
- Skip the cassette tests on ported modules. Wire-compat is the whole point.

# Deliverables
1. The module source file(s) at {{MODULE_PATH}}.
2. The unit + property + integration tests under tests/.
3. Any new config schema fields in src/quanta_core/config/schema.py.
4. A short HANDOFF.md note appended to docs/quanta-core-v4/HANDOFF.md
   describing what you built, what tests pass, and what the next agent
   needs to know.
5. A single commit on the current worktree branch — NOT pushed.

# Verification before you stop
- `uv run ruff check .` exits 0
- `uv run ruff format --check .` exits 0
- `uv run mypy --strict src/quanta_core/` exits 0
- `uv run pytest -m "not slow" --cov=src/quanta_core` shows ≥85% on your
  module and no failures
- For ported modules, the cassette/contract integration test for your
  module passes.

If any gate fails, fix the underlying issue and re-run. Do NOT skip the
hook (no --no-verify). Do NOT commit broken code.
```

The prompt template is intentionally **prescriptive** — build agents are
not asked to decide tool choices, layout, or test style. Doc 10 (this
document) has already decided. The agent's job is to write the code, run
the gates, and stop.

---

## Appendix A — Cited tools (one pick per category, recap)

| Category                  | Pick                       |
|---------------------------|----------------------------|
| Python                    | 3.12+                      |
| Package + venv            | uv                         |
| Build backend             | hatchling                  |
| Project layout            | src/                       |
| Lint + format             | ruff (one tool)            |
| Type check                | mypy --strict              |
| Test runner               | pytest + pytest-asyncio    |
| Property tests            | hypothesis                 |
| HTTP replay               | vcr.py                     |
| Clock mocking             | freezegun                  |
| Async loop                | asyncio + uvloop           |
| HTTP client               | httpx                      |
| Web framework             | FastAPI                    |
| Config                    | TOML + pydantic-settings v2|
| Logging                   | structlog → JSONL          |
| DB driver                 | psycopg 3                  |
| Model serialisation       | safetensors + JSON         |
| CI                        | GitHub Actions             |
| Pre-commit                | pre-commit                 |

No fence-sitting. If a build agent proposes a different tool, the change
must go through a doc-10 revision (r11+), not into a feature commit.
