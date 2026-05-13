# V4 runtime data

JSONL event logs written by V4 modules and read by the dashboard's
`/api/v4/*` handlers via `src/quanta_core/observability/v4_buffer.V4Buffer`.

## Files

- `debates.jsonl` — one event per debate session decision (regime → micro →
  bull/bear → arbiter → reflect). Consumed by `/api/v4/debate/history`.
- `parity.jsonl` — backtest-vs-live decision diffs (`agree` / `conflict` /
  `abstain`). Consumed by `/api/v4/parity`. Populated by the future
  shadow-mode runner — see `docs/V4_SHADOW_MODE_DESIGN.md`.
- `montecarlo.jsonl` — one event per Monte Carlo run. Consumed by
  `/api/v4/montecarlo/{trade_id}`.

## Operational notes

- Append-only. The dashboard never deletes here.
- An in-memory ring (default 256 events) lives alongside the file —
  reads are O(1) from RAM; the file is the durable record.
- A future cron at `scripts/v4_rotate_runtime.sh` will rotate files
  >100 MB. Not yet implemented; under 100 MB tonight even at full
  shadow-mode throughput, so this is a "later" tax.
- This directory is mounted only into the dashboard container. The
  freqtrade container does NOT read here — V4 is additive, see
  [[feedback-v4-is-additive]] in memory.

## Empty buffer behavior

When a JSONL file is empty (or the ring hasn't seen any events since
process start), `/api/v4/*` handlers fall back to deterministic mock
payloads via the `_live_or_mock` helper in
`user_data/dashboard/v4_routes.py`. This keeps the SPA rendering
end-to-end during early shadow-mode bring-up.
