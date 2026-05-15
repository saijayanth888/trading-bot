# Code-Quality Audit — 2026-05-14 Night

Read-only audit of the trading-bot repo. No source files modified, no `--fix`, no tests run.

## Tools run

| Tool | Scope | Exit | Findings |
|---|---|---|---|
| `ruff check` (no-fix) | `src/` `user_data/` `stocks/` `scripts/` `hermes-mcp/` `hermes_patches/` | 1 | **970 issues** (657 fixable, 164 unsafe-fixable). Output 972 lines. |
| `mypy --no-incremental --pretty` | `src/` `user_data/` | 1 | **413 errors** + 29 notes across 1700 lines of output. Completed within timeout. |
| `tsc --noEmit` | `frontend-v4/src/` | 0 | **Clean.** |
| `eslint src/` | `frontend-v4/src/` | 0 | **Clean** (default formatter; `compact` formatter removed in v9 — used default). |
| `npm run build` | `frontend-v4/` | 0 | **Builds.** `index-Bq4I5srl.js` 423.70 kB / gzip 136.96 kB; built in 2.77s. |
| `node --check` per file | `user_data/dashboard/static/js/*.js` (43 files) | 0 | **All parse.** |
| `git ls-files` existence check | full repo | 0 | **No tracked-but-missing files.** |
| TODO(P0)/FIXME(P0) grep | py + ts/tsx + js | 0 | **None.** |
| freqtrade import grep | py scope | 0 | **None left** after the freqtrade purge. |

## Inventory

- Python files in `src/` + `user_data/` + `stocks/`: **246**
- Python files in `scripts/` + `hermes-mcp/` + `hermes_patches/`: **1797** (large; `hermes_patches/` likely dominates)
- TypeScript files in `frontend-v4/src/`: **6** (.ts/.tsx) — small surface
- Dashboard JS files: **43**

---

## P0 — Blocks runtime / unsafe to ship

**None found.** Highlights:
- Zero ruff `E9xx` (Python syntax errors).
- Zero `tsc` errors.
- Zero `node --check` failures.
- No tracked-but-missing files.
- No `freqtrade.X` imports left after purge.
- No `# TODO(P0)` / `# FIXME(P0)` markers anywhere.

---

## P1 — Latent bugs / undefined names / dead-code-but-shipped

### F821 Undefined name (ruff) — 2 occurrences, 1 file, 1 symbol

```
stocks/shark/phases/pre_market.py:30:18:  F821 Undefined name `HistoricalEdge`
stocks/shark/phases/pre_market.py:284:26: F821 Undefined name `HistoricalEdge`
```

`HistoricalEdge` is referenced (likely in a type-annotation context on line 30 and a constructor on line 284) without being imported. If ever evaluated (or if either site is hit at runtime), it raises `NameError`. Suggest grepping for the original module that defined `HistoricalEdge` and adding the import, OR removing the dead reference.

### Mypy import-not-found — 8 occurrences

The mypy invocation can't resolve a few first-party `modules.*` imports because it's run from repo root without `MYPYPATH=user_data`:
```
user_data/modules/monitoring_mixin.py:57  modules.slack_alerts
user_data/modules/monitoring_mixin.py:58  modules.trade_journal
user_data/modules/monitoring_mixin.py:59  modules.metrics_writer
user_data/scripts/run_ept_generation.py:84  modules.ept_evolution
```
Plus the 3rd-party gap:
```
user_data/modules/news_aggregator.py:440  feedparser   (genuine; not installed)
user_data/modules/metrics_writer.py:55    influxdb_client (3 hits — purged at runtime per memory; stub still imports it)
```
Action: either drop the `influxdb_client` import in `metrics_writer.py` (memory says influx was removed), or guard with `try/except ImportError`. The `modules.*` ones are mypy-config issues, not real bugs.

### Mypy `[index]` and `[union-attr]` clusters — 32 occurrences total

- `[index]` 21 — likely indexing into Optional / mistyped containers. Highest concentration: `user_data/dashboard/ops_routes.py`, `user_data/modules/onchain_signals.py`.
- `[union-attr]` 11 — calling attrs on possibly-None values. Same hot files.

These are the highest-signal categories — each one is a potential `KeyError`/`AttributeError`/`TypeError` at runtime. Recommend a follow-up pass scoped to just these two codes (32 sites) before next ship.

### Mypy `[assignment]` — 16 occurrences

Type mismatch on assignment. Could be benign (mypy strictness) or could mask a real bug. Worth a manual scan.

---

## P2 — Style / modernization with churn risk

### Top 10 ruff codes

| Count | Code | Meaning | Example |
|---|---|---|---|
| 180 | UP017 | Use `datetime.UTC` alias | `hermes-mcp/server.py:224:60` |
| 116 | I001 | Import block un-sorted | `scripts/migrate_historic_predictions_dtype.py:64:1` |
| 96 | F401 | Imported but unused | `scripts/archive_shark_memory.py:37:20` (`typing.Callable`) |
| 73 | T201 | `print` found | `hermes-mcp/server.py:69:5` |
| 68 | UP045 | `Optional[X]` → `X | None` | `scripts/nightly_reflector.py:157:39` |
| 48 | PT019 | Pytest fixture passed but unused-as-value | `stocks/tests/test_config.py:34:40` |
| 38 | UP035 | Import from `collections.abc` | `scripts/archive_shark_memory.py:37:1` |
| 30 | UP006 | `Type[X]` → `type[X]` | `stocks/shark/llm/structured.py:76:26` |
| 29 | UP037 | Remove quotes from type annotation | `scripts/run_v4_shadow.py:113:26` |
| 29 | F841 | Local var assigned but unused | `stocks/shark/agents/combined_analyst.py:196:5` |

**Auto-fixable safely:** UP017, I001, F401, UP045, UP035, UP006, UP037 — 557 of the 657 fixable. These are pure modernization with no behavior change. A single `ruff check --fix` pass on these specific codes would clear ~57 % of all findings.

**Needs human review:** F841 (29) — sometimes the unused local is left over from refactoring (real dead-code) and sometimes it's a side-effect call that should stay. Spot-checked `stocks/shark/phases/pre_market.py` shows 5 F841s in the same file as the 2 F821s — that file has clearly drifted.

**T201 (73 prints):** mostly in scripts/ (CLI tools) and `hermes-mcp/server.py`. For CLI scripts T201 is a style preference; the prints are intentional. For the MCP server, prints to stdout can corrupt MCP protocol — worth grepping that one file.

### Top files by ruff count

```
100 user_data/dashboard/ops_routes.py     <- biggest hot-spot
 34 stocks/tests/test_stops.py
 26 stocks/tests/test_config.py
 24 user_data/modules/unified_risk.py
 21 stocks/tests/test_llm_rotation.py
 20 scripts/validate_readiness.py
 19 user_data/modules/ept_evolution.py
 17 stocks/wheel/strategy.py
 16 stocks/wheel/runner.py
 16 stocks/shark/run.py
```

`ops_routes.py` (118 mypy + 100 ruff) is by far the largest source of issues — long file, many endpoints, mixed types. Not blocking but ripe for a focused cleanup PR.

### Top mypy categories

| Count | Category | Notes |
|---|---|---|
| 151 | type-arg | Missing type params on generics (`dict`, `list`, `tuple`, etc.) — pure annotation gap |
| 91 | no-untyped-def | Missing parameter/return annotations |
| 41 | no-untyped-call | Calling an un-annotated function from typed code |
| 21 | index | **(P1 above)** |
| 16 | assignment | **(P1 above)** |

### Top files by mypy count

```
118 user_data/dashboard/ops_routes.py
 34 user_data/dashboard/mcp_local.py
 32 user_data/modules/onchain_signals.py
 29 user_data/modules/sentiment_engine.py
 22 user_data/modules/unified_risk.py
 20 user_data/modules/metrics_writer.py
 17 user_data/modules/regime_detector.py
 15 user_data/modules/ept_evolution.py
 12 user_data/modules/monitoring_mixin.py
 12 user_data/dashboard/ops_db.py
```

---

## P3 — Cosmetic / informational

- 24 `RUF100` unused-noqa directives — left over from earlier rule cleanups; pure noise but trivially auto-fixable.
- 22 `E741` ambiguous variable names (`l`, `I`, `O`).
- 19 `UP041` aliased errors (`asyncio.TimeoutError` → `TimeoutError`).
- 16 `SIM117` nested `with` blocks.
- 14 `RUF022` unsorted `__all__`.
- 13 `B904` raise-without-from inside except.
- 13 `ASYNC240` async-blocking calls (worth a glance — could be P2 if any are in hot paths).
- 10 each: `RUF059` (unused unpacked var), `RET504` (unnecessary assignment before return).

### Frontend

- `frontend-v4` is in good shape: tsc + eslint + build all green. Only 6 source files in `src/` though, so the surface is small.
- Legacy SPA JS (43 files) all pass `node --check` — no syntax errors.

### eslint formatter note

ESLint 9 removed `--format=compact`. Used default formatter; output is empty so all files clean. To re-enable compact: `npm install -D eslint-formatter-compact` (do not auto-install).

---

## Recommendations (ordered)

1. **P1 — Fix the `HistoricalEdge` undefined name** in `stocks/shark/phases/pre_market.py` (lines 30, 284). Either restore the missing import or delete the reference. 5-minute fix that removes a latent `NameError`.
2. **P1 — Audit `metrics_writer.py` influx imports.** Memory note 2026-05-09 says influxdb was removed; the import on line 55 is dead/broken. Remove or guard.
3. **P1 — Triage the 32 `[index]` + `[union-attr]` mypy hits**, mostly in `ops_routes.py` and `onchain_signals.py`. These are the categories most likely to be real bugs.
4. **P2 — Run `ruff check --fix` scoped to UP017/I001/F401/UP045/UP035/UP006/UP037** on a feature branch. Eliminates ~557 issues with zero behavior change. Review the diff, ship.
5. **P2 — Manually review the 29 `F841` unused locals** — about half are likely dead refactoring residue worth deleting.
6. **P2 — Decide policy on `T201` prints in `hermes-mcp/server.py`.** Prints to stdout in MCP servers can corrupt the protocol; likely should be `logger.info`.
7. **P3 — Optional `ruff check --fix` for RUF100/UP041/RUF022** on the same branch — pure cleanup.
8. **P3 — Configure `mypy.ini` `mypy_path = user_data`** to silence the spurious `modules.*` import-not-found errors and let real ones surface.

---

## Counts summary

```
Python files in scope     : 2043 (246 src/user_data/stocks + 1797 scripts/hermes*)
TS/TSX files in scope     :    6
JS files in scope         :   43

ruff issues               :  970   (657 auto-fixable, 164 with --unsafe-fixes)
ruff P0 (E9xx)            :    0
ruff P1 (F821)            :    2
mypy errors               :  413
mypy notes                :   29
tsc errors                :    0
eslint errors             :    0
node --check failures     :    0
build failures            :    0
tracked-but-missing       :    0
TODO(P0) / FIXME(P0)      :    0
stale freqtrade imports   :    0
```

**Bottom line:** No P0 issues. Three P1 fixes (1 undefined name, 1 stale influx import, ~32 typed-Optional indexing/attr risks). Everything else is style + type-annotation backlog, the bulk of which is auto-fixable with no behavior change.
