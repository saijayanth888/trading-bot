# ModelForge Data Pipeline — Stages 1-2

The trading-bot writes per-role training data into ModelForge's HF Arrow
curated format via a six-stage cron pipeline (see
[MODELFORGE_INTEGRATION_PLAN.md](./MODELFORGE_INTEGRATION_PLAN.md) § *Pipeline*).
This document covers the first two stages, both pure Python data-shaping
scripts:

| Stage | Script | Cron slot (ET) | Purpose |
|------:|--------|----------------|---------|
| 1     | `scripts/modelforge_ingest.py` | 21:00 nightly | Parse `decisions.md` + `llm-calls.jsonl` → raw per-role JSONL |
| 2     | `scripts/modelforge_curate.py` | 21:30 nightly | Filter raws → write HF Arrow shard ModelForge can train on |

## Stage 1 — Ingest

For each of the 6 trading-bot LLM roles
(`trading-reflector`, `trading-bull`, `trading-bear`, `trading-arbiter`,
`trading-regime-tagger`, `trading-indicator-selector`), Stage 1 pulls
yesterday's calls and emits one raw JSONL line per call to
`~/.dgx-train/raw/<role>/<YYYYMMDD>.jsonl`.

- `trading-reflector` reads `stocks/memory/decisions.md` via the canonical
  `stocks.shark.memory.decisions` parser, filtering for entries whose
  `closed_at` (derived from `open_date + holding_days`) matches the target
  date. Pending blocks are also emitted with `pending_outcome=True` so the
  exporter has visibility into open trades; Stage 2 filters them out.
- The five other roles read `stocks/memory/llm-calls.jsonl` (schema:
  [LLM_LOGGER_SCHEMA.md](./LLM_LOGGER_SCHEMA.md)) and split by `agent` field.

Idempotent: a per-day file already on disk causes the role to skip silently.

## Stage 2 — Curate

For each role, Stage 2 reads the new raw JSONL files since its last state
checkpoint and applies a deterministic filter:

| Role | Keep rule |
|------|-----------|
| `trading-reflector` | realized + response ∈ [80, 600] chars + cited alpha within ±5 pp of ledger + `exit_reason` in known enum |
| `trading-bull` / `trading-bear` | response ∈ [200, 1500] chars + ≥2 numeric / indicator / date evidence items |
| `trading-arbiter` / `trading-regime-tagger` / `trading-indicator-selector` | upstream `valid=True` flag + response parses as JSON |

Kept examples are projected onto ModelForge's HF Arrow row schema:

```
category: string       # = role / track_id
source: string         # = "trading-bot"
dataset_name: string   # = role
instruction: string    # = "[SYSTEM]\n<sys>\n[USER]\n<user>"
response: string       # = model output
```

…and saved via `datasets.Dataset.save_to_disk()` to
`~/.dgx-train/datasets/<role>/curated/`. A sidecar `mf_meta.json` carries
`track_id, generation, source_split, sample_count, timestamp_utc`, plus the
five fields ModelForge's own curator writes (`num_samples`, `categories`,
`sources`, `weakness_report`, `max_samples`).

Stats land in `~/.dgx-train/curate/<role>_<date>.json` (accept count, reject
count, reject reasons, accept rate). A Slack notifier fires only when accept
rate falls outside `[10%, 90%]`.

## Where files land

```
~/.dgx-train/
├── raw/                                    # Stage 1 output
│   ├── trading-reflector/20260511.jsonl
│   ├── trading-bull/20260511.jsonl
│   ├── trading-bear/20260511.jsonl
│   ├── trading-arbiter/20260511.jsonl
│   ├── trading-regime-tagger/20260511.jsonl
│   └── trading-indicator-selector/20260511.jsonl
├── datasets/                               # Stage 2 output (HF Arrow)
│   └── <role>/curated/
│       ├── data-00000-of-00001.arrow
│       ├── dataset_info.json
│       ├── state.json
│       └── mf_meta.json
└── curate/                                 # state + per-day stats
    ├── state.json
    └── <role>_<YYYY-MM-DD>.json
```

## Schemas

### Stage-1 raw row (one JSONL line)

```json
{
  "ts":              "2026-05-11",
  "ticker":          "NVDA",
  "system_message":  "<system prompt>",
  "user_message":    "<user prompt>",
  "response":        "<model output>",
  "pending_outcome": false,
  "outcome_key":     "2026-05-11|NVDA",
  "ledger":          { "open_date": "...", "raw_pct": "...", ... }
}
```

### Stage-2 HF Arrow row

```json
{
  "category":     "trading-reflector",
  "source":       "trading-bot",
  "dataset_name": "trading-reflector",
  "instruction":  "[SYSTEM]\nYou are Shark's nightly reflector...\n[USER]\n...",
  "response":     "Worked: NVDA AI capex catalyst held; +1.0% alpha vs SPY..."
}
```

## Required ModelForge fix (R1, blocking)

Before any training run consumes this data, **`apps/api/src/agents/
training_backend.py:301` must be patched** to honour `config["curated_path"]`
instead of hardcoding `load_dataset("Open-Orca/OpenOrca", split="train[:1000]")`.
One-line fix in a separate ModelForge staging branch. Without it the trainer
ignores everything this pipeline produces. Reference:
[MODELFORGE_INTEGRATION_PLAN.md § R1](./MODELFORGE_INTEGRATION_PLAN.md#7-risks--open-questions).

Suggested patch:

```python
# was:
raw = load_dataset("Open-Orca/OpenOrca", split="train[:1000]")
# replace with:
curated = (config or {}).get("curated_path")
if curated:
    raw = load_from_disk(curated)
else:
    raw = load_dataset("Open-Orca/OpenOrca", split="train[:1000]")
```

## Enabling full-text logging

The five non-reflector roles produce *zero* training examples unless the bot
is writing prompt + response to `llm-calls.jsonl`. Set
`SHARK_LLM_LOG_FULL_TEXT=1` in the bot's `.env`:

```bash
echo "SHARK_LLM_LOG_FULL_TEXT=1" >> .env
```

See [LLM_LOGGER_SCHEMA.md § How to enable](./LLM_LOGGER_SCHEMA.md#how-to-enable).

## Running locally

```bash
# Ingest yesterday's calls
python scripts/modelforge_ingest.py

# Or a specific day
python scripts/modelforge_ingest.py 2026-05-11

# Curate everything new since last run
python scripts/modelforge_curate.py

# Tests
python -m pytest tests/test_modelforge_ingest.py tests/test_modelforge_curate.py -v
```

Both scripts are fail-soft: errors go to
`stocks/memory/cron-modelforge-{ingest,curate}.log` and the process exits 0
so cron never alarms.
