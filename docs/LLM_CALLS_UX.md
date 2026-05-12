# LLM Calls UX — Operator Runbook

A proper UX for the LLM-call log at `stocks/memory/llm-calls.jsonl`.

Before this branch the only way to inspect calls was `cat`/`tail`, which
became unusable once `SHARK_LLM_LOG_FULL_TEXT=1` flipped on and each line
ballooned to 1–4 KB of dense JSON.

This pack delivers three things:

1. **Dashboard card** — `LLMCallsLive` on `/ops_spa` shows the feed, with a
   slide-over modal for full prompt + response on click.
2. **Terminal viewer** — `scripts/tail_llm_calls.sh` for the operator's
   tmux window.
3. **Rotation** — `stocks/shark/llm/rotate.py` plus a Hermes cron at 03:00
   ET nightly so the live file stays bounded and 90 days of archives are
   kept.

## Dashboard card

Mounted under the training row on `/ops`. Sketch:

```
┌─────────────────────────────────────────────────────────────────────┐
│ 21 · LLM activity · last 24h                       142 · 24H        │
│ ─────────────────────────────────────────────────────────────────── │
│ Calls   Tokens   Avg lat   P95 lat   Ollama   Success               │
│ 142     89.3k    2.4s      6.1s      88%      99.3%                 │
│                                                                     │
│ AGENT [all agents (8) ▾]  SEARCH [regex over agent/model · ⌘F ]     │
│                                                                     │
│ TIME      AGENT              MODEL · TIER     LAT   TOKENS  STATUS  │
│ 14:32:11  reflector          qwen3:30b · fast 8.2s  421/180  ●      │
│ 14:31:54  analyst_bull       qwen3:30b · fast 3.1s  502/340  ●      │
│ 14:31:21  risk_debate.agg    qwen3:30b · deep 4.2s  233/128  ●      │
│ ...                                                                 │
│ [ load more (92 more) ]                                             │
│ click any row · Esc closes modal · ⌘F focuses search                │
└─────────────────────────────────────────────────────────────────────┘
```

- **Latency colour** — green <2s, yellow 2-5s, orange 5-15s, red >15s.
- **Status dot** — green = success, red = failure (no completion + zero
  latency).
- **Click any row** to open a right-side slide-over modal with the full
  system message, user prompt, assistant response, metadata table, and
  copy-to-clipboard buttons for prompt + response.
- **Keyboard** — `Esc` closes the modal; `⌘F` / `Ctrl-F` focuses the
  search box.
- **Auto-refresh** — 10 s (FAST_ENDPOINTS group).

## Endpoints

### `GET /api/ops/llm_calls`

Read-only, no auth. Query params:

| Param            | Type    | Default | Notes                                  |
|------------------|---------|---------|----------------------------------------|
| `limit`          | int     | 50      | clamped 1..500                         |
| `agent`          | string  | —       | substring match (case-insensitive)     |
| `model`          | string  | —       | substring match                        |
| `since`          | ISO ts  | —       | reject calls older than this           |
| `q`              | regex   | —       | matches agent/model/tier/role; also prompt+response when `include_text=1` |
| `include_text`   | 0\|1    | 0       | strip prompt/system/response by default|
| `min_latency`    | seconds | —       | reject faster calls                    |
| `max_latency`    | seconds | —       | reject slower calls                    |

Response envelope:

```json
{
  "status": "ok",
  "data": {
    "calls":            [<record>, ...],
    "total_in_window":  142,
    "total_24h":        142,
    "summary": {
      "total_calls":        142,
      "total_prompt_tokens": 67891,
      "total_completion_tokens": 21456,
      "total_tokens":       89347,
      "avg_latency_s":      2.40,
      "p95_latency_s":      6.12,
      "max_latency_s":      14.23,
      "ollama_pct":         88.0,
      "anthropic_pct":      12.0,
      "success_pct":        99.3,
      "by_agent":           {"reflector": 31, ...},
      "by_model":           {"qwen3:30b": 122, ...},
      "by_tier":            {"fast": 90, "deep": 52},
      "providers":          {"ollama": 125, "anthropic": 17}
    },
    "log_path":     "/freqtrade/stocks/memory/llm-calls.jsonl",
    "log_size_bytes": 1240432,
    "include_text": false
  },
  "error": null
}
```

### `GET /api/ops/llm_calls/{call_id}`

`call_id` is the URL-encoded ISO timestamp. The tracker doesn't generate
UUIDs but timestamps have microsecond resolution, so collisions are
effectively impossible.

Status codes:
- `200` — record found in live log; full record (including
  prompt/system/response) returned.
- `404` — record not in live log AND no archives on disk.
- `410` — record is in an archive (file was rotated). Response body
  includes `archive_path` so operator can grep manually.

## Terminal viewer

One-liner the operator runs to watch live:

```bash
bash scripts/tail_llm_calls.sh
```

Output:

```
TIME      AGENT                MODEL            LAT   P_TOK/C_TOK
14:32:11  reflector            qwen3:30b       8.2s   421/180
14:31:54  analyst_bull         qwen3:30b       3.1s   502/340
```

Flags:
- `--full` — appends 200-char previews of `system:`, `user:`, `reply:`.
- `--agent <name>` — only rows whose `agent` field exactly equals `<name>`.
- `--since <duration>` — back-fill records from the last `1h` / `30m` /
  `2d` before starting the live tail.
- `--help` — usage.

Environment:
- `SHARK_TRACKER_LOG` — override the log path. Mirrors the tracker.

Dependencies: `jq`, `tail` (standard).

## Rotation

`stocks/shark/llm/rotate.py` rotates the live file when:

- size > **50 MB**, OR
- first-record timestamp older than **30 days**.

After rotation:
1. Live file is gzipped to `llm-calls.YYYY-MM-DD.jsonl.gz` in the same
   directory.
2. Live file is truncated in place (inode preserved so any open writer
   keeps appending to the same fd).
3. Archives older than **90 days** are deleted.

CLI:

```bash
cd stocks && python3 -m shark.llm.rotate
```

Hermes cron: `0 3 * * *` via `~/.hermes/scripts/llm_log_rotate.sh`.

## Files

| Path                                                          | What                                       |
|---------------------------------------------------------------|--------------------------------------------|
| `user_data/dashboard/ops_routes.py` (extended)                | 2 endpoints: list + detail                 |
| `user_data/dashboard/static/js/ops_spa.js` (extended)         | `LLMCallsLive` + `LLMCallModal` components |
| `user_data/dashboard/templates/ops_spa.html` (cache-bust)     | bumped to `?v=20260512-llm-calls-ux`       |
| `stocks/shark/llm/rotate.py`                                  | rotation library + CLI shim                |
| `scripts/tail_llm_calls.sh`                                   | terminal viewer                            |
| `~/.hermes/scripts/llm_log_rotate.sh`                         | nightly cron wrapper                       |
| `tests/test_llm_calls_endpoint.py`                            | endpoint tests (23 cases)                  |
| `stocks/tests/test_llm_rotation.py`                           | rotation tests (17 cases)                  |

## Why we built this

Operator feedback (2026-05-12): "the JSONL is **very ugly**". After the
flag flip to `SHARK_LLM_LOG_FULL_TEXT=1`, `cat` and `tail -f` both stopped
being usable: each line is 1-4 KB of unindented JSON, and there's no way
to filter by agent or search across prompts.

The LLMCallsLive card and `tail_llm_calls.sh` are also two of the
viral-launch screenshots — the "watch the AI work in real-time" angle.
Operator wants the modal to look polished because the screenshots will
end up on social media.
