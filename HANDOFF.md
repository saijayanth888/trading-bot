# HANDOFF — stage/llm-calls-ux

Branch: `stage/llm-calls-ux`
Status: ready for review · DO NOT MERGE
Tests: 88/88 passing (rotation 17, endpoint 23, override-verifier 11, ops-dashboard 20, llm-logger 28; bt_quality_gates 46 also re-checked clean)
Worktree: `.claude/worktrees/agent-aad7bc15946e36c34`

## Why

Operator: "the JSONL is **very ugly**". Once `SHARK_LLM_LOG_FULL_TEXT=1`
flipped on, each line at `stocks/memory/llm-calls.jsonl` ballooned to 1-4
KB of dense JSON — `cat` and `tail -f` stopped being usable.

This branch delivers a proper UX over that file: dashboard card, slide-
over modal, terminal viewer, and nightly rotation so the file doesn't
grow forever.

This is also one of the **viral-launch screenshots** — the "watch the AI
work" angle. The modal aesthetic matters because the screenshots will
end up on social media.

## What ships

| # | Thing                                | Path                                                     |
|---|--------------------------------------|----------------------------------------------------------|
| 1 | List endpoint                        | `user_data/dashboard/ops_routes.py` — `GET /api/ops/llm_calls` |
| 2 | Detail endpoint                      | `user_data/dashboard/ops_routes.py` — `GET /api/ops/llm_calls/{call_id}` |
| 3 | LLMCallsLive card + modal            | `user_data/dashboard/static/js/ops_spa.js`              |
| 4 | Cache-bust bump                      | `user_data/dashboard/templates/ops_spa.html` → `?v=20260512-llm-calls-ux` |
| 5 | Rotation library + CLI               | `stocks/shark/llm/rotate.py`                            |
| 6 | Terminal viewer                      | `scripts/tail_llm_calls.sh`                             |
| 7 | Hermes cron wrapper                  | `~/.hermes/scripts/llm_log_rotate.sh` (out-of-tree)     |
| 8 | Endpoint tests (23)                  | `tests/test_llm_calls_endpoint.py`                      |
| 9 | Rotation tests (17)                  | `stocks/tests/test_llm_rotation.py`                     |
| 10| Operator runbook                     | `docs/LLM_CALLS_UX.md`                                  |

NOT touched: `stocks/shark/llm/tracker.py` (already correct from the
earlier merge), the JSONL write format. Read-side only.

## Card + modal sketches

```
┌─────────────────────────────────────────────────────────────────────┐
│ 21 · LLM activity · last 24h               142 · 24H               │
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

  Click any row → slide-over from the right:

  ┌─────────────────────────────────────┐
  │ analyst_bull  [● 3.10s]      [✕ esc]│
  │ ─────────────────────────────────── │
  │ METADATA                            │
  │   timestamp     2026-05-12T14:31:54 │
  │   provider      ollama              │
  │   model         qwen3:30b           │
  │   tier          fast                │
  │   role          default             │
  │   latency       3.103s              │
  │   prompt_tokens 502                 │
  │   completion    340                 │
  │   total         842                 │
  │   redacted      0                   │
  │                                     │
  │ SYSTEM MESSAGE                      │
  │ ┌─────────────────────────────────┐ │
  │ │ you are a stock analyst…        │ │
  │ └─────────────────────────────────┘ │
  │                                     │
  │ USER PROMPT          [copy prompt] │
  │ ┌─────────────────────────────────┐ │
  │ │ Analyse AAPL on the 4h …        │ │
  │ └─────────────────────────────────┘ │
  │                                     │
  │ ASSISTANT RESPONSE  [copy response] │
  │ ┌─────────────────────────────────┐ │
  │ │ AAPL is consolidating above…    │ │
  │ └─────────────────────────────────┘ │
  └─────────────────────────────────────┘
```

## Endpoint contracts

### `GET /api/ops/llm_calls`

Read-only, no auth dep. Query params:
- `limit` (1..500, default 50)
- `agent`, `model` — substring filters
- `since` — ISO timestamp
- `q` — regex; covers prompt/response when `include_text=1`
- `min_latency`, `max_latency` — seconds
- `include_text` (0|1, default 0)

Returns `{status: "ok", data: {calls, total_in_window, total_24h, summary,
log_path, log_size_bytes, include_text}, error, checked_at}`.

When `include_text=0` the heavy fields (`prompt`, `system_message`,
`response_text`, `messages`) are stripped from each call object so the
polling payload stays small.

### `GET /api/ops/llm_calls/{call_id}`

`call_id` is the URL-encoded ISO timestamp.

- `200` — found in live log; full record (with text) returned.
- `404` — not in live AND no archives.
- `410` — in archive (rotated); response includes `archive_path` so
  operator can grep manually.

## Terminal viewer

```bash
bash scripts/tail_llm_calls.sh                   # live tail, metadata only
bash scripts/tail_llm_calls.sh --full            # + system/user/reply previews (≤200 chars)
bash scripts/tail_llm_calls.sh --agent reflector # only that agent's calls
bash scripts/tail_llm_calls.sh --since 2h        # back-fill last 2h then tail
```

Output is colour-coded by latency (green <2s, yellow 2-5s, orange 5-15s,
red >15s). Honours `$SHARK_TRACKER_LOG`.

## Rotation cron

Hermes cron: `0 3 * * *` (03:00 local · after midnight UTC pivot, before
pre-market open).

Wrapper: `~/.hermes/scripts/llm_log_rotate.sh`
Library: `stocks/shark/llm/rotate.py`

Triggers:
1. Live file size > **50 MB**, OR
2. Oldest record > **30 days**.

After rotation:
- Live file gzipped to `llm-calls.YYYY-MM-DD.jsonl.gz` in the same dir.
- Live file truncated in place (inode preserved → open writers keep
  appending to the same fd).
- Archives older than **90 days** deleted.

To install in crontab (operator does this once):

```bash
crontab -l > /tmp/cron.txt
echo "0 3 * * * /home/saijayanthai/.hermes/scripts/llm_log_rotate.sh" >> /tmp/cron.txt
crontab /tmp/cron.txt
```

## How to test locally

```bash
# Tests
pytest tests/test_llm_calls_endpoint.py stocks/tests/test_llm_rotation.py -v

# CLI rotation smoke test (won't actually rotate the live file)
cd stocks && SHARK_TRACKER_LOG=/tmp/dummy.jsonl python3 -m shark.llm.rotate

# Terminal viewer (works against a tiny mock file)
TMPLOG=$(mktemp /tmp/llm-calls.XXXXXX.jsonl)
echo '{"agent":"reflector","model":"qwen3:30b","provider":"ollama","tier":"fast","role":"default","latency_seconds":8.2,"prompt_tokens":421,"completion_tokens":180,"timestamp":"2026-05-12T14:32:11.103+00:00"}' > "$TMPLOG"
SHARK_TRACKER_LOG="$TMPLOG" timeout 1 bash scripts/tail_llm_calls.sh
```

Browser: `http://localhost:8081/ops` (or whatever port the dashboard
is bound to). The new card is mounted under the TRAINING row, full
width, anchored at `#llm-calls`.

## Out-of-scope (deferred)

- **Token cost projection forward** — current summary shows
  counterfactual savings (`shark.total_api_cost_saved_usd`) but doesn't
  break it down by agent. Add per-agent saved-USD when operator asks.
- **Live SSE stream** — card currently polls every 10 s. SSE would be
  nicer but the operator's primary "live" channel is the terminal
  viewer; the card is for browsing.
- **Modal regex search across the full archive** — the modal's
  search-box only filters the visible page. To search archives an
  operator currently runs `zcat` manually (hint surfaced in the 410
  response). Could be done server-side with a `q` param on the detail
  endpoint, defer until asked.

## Files changed

```
M  user_data/dashboard/ops_routes.py          (+~360 lines: 2 endpoints + helpers)
M  user_data/dashboard/static/js/ops_spa.js   (+~500 lines: LLMCallsLive + modal + helpers)
M  user_data/dashboard/templates/ops_spa.html (cache-bust)
A  stocks/shark/llm/rotate.py                 (rotation lib + CLI)
A  scripts/tail_llm_calls.sh                  (terminal viewer)
A  ~/.hermes/scripts/llm_log_rotate.sh        (out-of-tree cron wrapper)
A  tests/test_llm_calls_endpoint.py           (23 tests)
A  stocks/tests/test_llm_rotation.py          (17 tests)
A  docs/LLM_CALLS_UX.md                       (operator runbook)
A  HANDOFF.md                                 (this file)
```
