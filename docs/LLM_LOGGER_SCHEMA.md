# LLM-Call Logger — JSONL Schema

Source of truth: `stocks/memory/llm-calls.jsonl`
Producer: `stocks/shark/llm/tracker.py` (`LLMTracker.record` → `_append_jsonl`)
Consumer: dashboard `/api/ops/llm-stats`, and (planned) the ModelForge SFT
exporter — see `docs/MODELFORGE_INTEGRATION_PLAN.md` once that doc lands.

Every line is one JSON object representing one LLM call, fsync'd and
locked-on-write so concurrent shark crons don't tear records.

---

## Always-present fields (metadata)

These keys are present on every record, in every mode. The dashboard's
`/api/ops/llm-stats` summariser depends only on these.

| Key                  | Type    | Example                              | Notes                                                |
|----------------------|---------|--------------------------------------|------------------------------------------------------|
| `agent`              | string  | `"combined_analyst"`                 | Caller's name from the `agent=` kwarg.               |
| `model`              | string  | `"hermes3:8b"`                       | Resolved model string actually used.                 |
| `provider`           | string  | `"ollama"` \| `"anthropic"` \| …     | Provider class that served the call.                 |
| `tier`               | string  | `"fast"` \| `"deep"`                 | Routing tier (8B vs 70B; cheaper vs quality).        |
| `role`               | string  | `"default"`, `"arbiter"`, `"risk"`   | Agent role for env-var overrides.                    |
| `latency_seconds`    | float   | `2.341`                              | Wall-clock chat() time, rounded to 3 d.p.            |
| `prompt_tokens`      | int     | `843`                                | From provider usage block.                           |
| `completion_tokens`  | int     | `212`                                | From provider usage block.                           |
| `timestamp`          | string  | `"2026-05-12T14:22:18.103+00:00"`    | ISO-8601 UTC, generated at record time.              |

---

## Optional full-text fields

Persisted ONLY when `SHARK_LLM_LOG_FULL_TEXT=1`. Older lines written
before the flag existed simply lack these keys (or have them as `null`),
which is fine — readers should `.get(...)` with defaults.

| Key               | Type        | Notes                                                                                      |
|-------------------|-------------|--------------------------------------------------------------------------------------------|
| `prompt`          | string\|null | The final user message (post-schema-hint augmentation, if any).                            |
| `system_message`  | string\|null | The system prompt sent to the model.                                                       |
| `response_text`   | string\|null | The model's text output (post-validation if structured).                                   |
| `messages`        | list\|null  | Full chat-format `[{role, content}, ...]` array — useful for training reproducing context. |
| `redacted_count`  | int\|null   | Total substitutions across all four text fields; non-zero is a tripwire to investigate.    |

All text values are passed through the redactor (see below) **before**
they hit disk — the in-memory ring also stores the redacted version.
Original strings never persist anywhere outside the request that
produced them.

---

## Redaction list

Implemented in `stocks/shark/llm/redaction.py`. Each rule swaps its match
for `<REDACTED:<name>>` and increments `redacted_count`.

| Name      | Catches                                                      | Example in → out                                                                                       |
|-----------|--------------------------------------------------------------|---------------------------------------------------------------------------------------------------------|
| `api_key` | `sk-…`, `pk-…`, `xoxb-…`, `key-…`, or `api_key = "…"` (16+)  | `sk-abcdef0123456789ABCDEF` → `<REDACTED:api_key>`                                                      |
| `webhook` | Slack incoming-webhook URLs                                  | `https://hooks.slack.com/services/T0/B0/xyz` → `<REDACTED:webhook>`                                     |
| `path`    | Anything under `/home/saijayanthai/…`                        | `/home/saijayanthai/Documents/trading-bot/log` → `<REDACTED:path>`                                      |
| `email`   | RFC-5322-ish addresses                                       | `saijayanth532@gmail.com` → `<REDACTED:email>`                                                          |
| `account` | 10+ digit numbers immediately adjacent to `account`/`wallet`/`id` | `account 1234567890` → `account <REDACTED:account>`                                                |

Negative cases (explicitly NOT redacted, covered in tests):

- `session 1234567890 expired` — "session" is not the trigger keyword.
- `ts=1715000000000 latency=1.2` — bare timestamps without a trigger keyword.
- `/opt/trading-bot/bin/shark` — non-operator paths.
- `https://example.com/api/foo` — non-Slack URLs.
- `sk-short` — fewer than 16 chars after the prefix.

---

## How to enable

Default behaviour is **flag OFF** — the producer keeps writing only the
metadata block. To opt in:

```bash
# One-off for a single cron run
SHARK_LLM_LOG_FULL_TEXT=1 python shark/run.py midday

# Or permanently — add to .env
echo "SHARK_LLM_LOG_FULL_TEXT=1" >> .env
```

Reads happen on every record call, so toggling the env var takes effect
at the next LLM call — no process restart needed.

---

## ModelForge consumption

The (upcoming) `stocks/scripts/export_for_modelforge.py` reads this JSONL,
filters to rows where `prompt` and `response_text` are non-null, and emits
SFT-pair training files. See `docs/MODELFORGE_INTEGRATION_PLAN.md` for the
exporter contract.

Until that exporter ships, callers can prototype against the schema with:

```python
import json
rows = [json.loads(l) for l in open("stocks/memory/llm-calls.jsonl")]
sft = [
    {"prompt": r["prompt"], "completion": r["response_text"]}
    for r in rows
    if r.get("prompt") and r.get("response_text")
]
```

---

## Storage estimate

- Metadata-only record: ~250 bytes
- Full-text record (median trading prompt): ~1 KB
- Typical busy week: ~200 calls/day

So with the flag on for a month: **~6 MB**. Disk is not the constraint.

## Retention

Rotation is **out of scope for this branch**. The plan:

- Rotate `llm-calls.jsonl` daily (e.g. `llm-calls-2026-05-12.jsonl`).
- Delete files older than 90 days unless flagged as a training corpus.
- Cleanup runs as part of the nightly maintenance cron.

Until that ships, the file grows unbounded — fine at 6 MB/month, but flag
it for archival when it crosses 100 MB.

---

## Sample records

**Flag OFF** (current production format — unchanged for backward compat):

```json
{"agent":"combined_analyst","model":"hermes3:8b","provider":"ollama","tier":"fast","role":"default","latency_seconds":1.842,"prompt_tokens":620,"completion_tokens":180,"timestamp":"2026-05-12T14:22:18.103+00:00","prompt":null,"system_message":null,"response_text":null,"messages":null,"redacted_count":null}
```

**Flag ON** (extended format — new):

```json
{"agent":"combined_analyst","model":"hermes3:8b","provider":"ollama","tier":"fast","role":"default","latency_seconds":1.842,"prompt_tokens":620,"completion_tokens":180,"timestamp":"2026-05-12T14:22:18.103+00:00","prompt":"Analyse AAPL on the 4h timeframe.","system_message":"You are a stock analyst.","response_text":"AAPL is consolidating above the 50-EMA…","messages":[{"role":"system","content":"You are a stock analyst."},{"role":"user","content":"Analyse AAPL on the 4h timeframe."},{"role":"assistant","content":"AAPL is consolidating above the 50-EMA…"}],"redacted_count":0}
```
