# quanta-core

Trading bot core for the Quanta v4 stack. Live engine, backtest engine, risk
governor, execution engine, model registry, and strategy ABC — all bound by
the contracts in `docs/quanta-core-v4/06-ARCHITECTURE.md` and the operator
decisions in `docs/quanta-core-v4-rev2/DESIGN-LOCK.md`.

This package owns the trading layer only. ModelForge owns training; Hermes
owns scheduling. The three-way ownership contract is enforced as import
discipline — see DESIGN-LOCK §2.

## Status

`0.1.0a1` — foundation skeleton. Types, Strategy ABC, config loader, and
logging are in place. Exchanges / live / backtest / models / agents / risk /
execution / lora / ledger / observability / hermes packages exist as empty
placeholders for downstream build agents.

## Install

```bash
uv venv --python 3.12
uv pip install -e '.[dev]'
```

## Module map

| Path | Status | Responsibility |
|------|--------|----------------|
| `quanta_core.types` | done | Bar / Tick / Fill / Position / OrderProposal / Context |
| `quanta_core.strategy.base` | done | Strategy ABC |
| `quanta_core.config` | done | TOML + env-var loader (`runtime.mode` flag) |
| `quanta_core.logging_setup` | done | structlog JSONL + PII redaction |
| `quanta_core.exchanges` | stub | Alpaca + Coinbase adapters (agent: exchanges) |
| `quanta_core.live` | stub | LiveEngine + dispatcher (agent: live) |
| `quanta_core.backtest` | stub | Replay engine (agent: backtest) |
| `quanta_core.models` | stub | TFT + registry (agent: models) |
| `quanta_core.agents` | stub | Bull/bear/arbiter debate (agent: debate) |
| `quanta_core.risk` | stub | Governor + Monte Carlo gates (agent: risk) |
| `quanta_core.execution` | stub | Order routing + idempotency (agent: execution) |
| `quanta_core.lora` | stub | Online LoRA training (agent: lora) |
| `quanta_core.ledger` | stub | Postgres trade/fill/decision log (agent: ledger) |
| `quanta_core.observability` | stub | Prometheus + dashboard hooks |
| `quanta_core.hermes` | stub | Layer 8 cron glue (read state only) |

## Design docs (read these before changing anything)

* [`docs/quanta-core-v4-rev2/DESIGN-LOCK.md`](../docs/quanta-core-v4-rev2/DESIGN-LOCK.md) — operator-locked decisions
* [`docs/quanta-core-v4/06-ARCHITECTURE.md`](../docs/quanta-core-v4/06-ARCHITECTURE.md) — module API spec
* [`docs/quanta-core-v4/10-CODE_PATTERNS.md`](../docs/quanta-core-v4/10-CODE_PATTERNS.md) — toolchain + style rules

## Quality gates

* `ruff check && ruff format --check`
* `mypy --strict src/`
* `pytest` with coverage >= 85% (95% on types + strategy/base + config)

Run all three before opening a PR.
