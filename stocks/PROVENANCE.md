# stocks/ — provenance

This subdirectory was imported from a separate repo on 2026-05-10 as part of unifying the crypto + stocks trading systems under a single project root.

## Origin

| Field | Value |
|---|---|
| Repo | https://github.com/saijayanth888/shark-trading-agent |
| Commit (HEAD at import) | `deb04b3eeda925dd4468d18ee0e79ce43a697d01` |
| Branch | `main` |
| Commit date | `2026-05-08T13:46:13+00:00` |
| Commit message | `market-open 2026-05-08: none regime=BEAR_VOLATILE` |
| Imported by | Claude Code session, 2026-05-10 |

The original repo's `.git/`, `venv/`, `__pycache__/`, and `.pytest_cache/` were excluded — only source, docs, KB, and config templates were copied.

## Why we merged it

- Single project root → single `.env`, one dashboard (`:8081/ops`), one Hermes cron daemon scheduling both crypto + stocks bots.
- Operator runs both systems on the same DGX Spark; coexisting under `~/Documents/trading-bot/` means one place to look for state, logs, backups.
- The hourly out-of-tree backup at `~/Documents/setup/backups/trading-bot/` automatically captures everything under `trading-bot/` — `stocks/` gets backed up for free.

## What lives where now

| Path | What it is |
|---|---|
| `trading-bot/user_data/` | Crypto stack (freqtrade + TFT + DRL) — runs in Docker |
| `trading-bot/stocks/` | This directory — US stock momentum trading via Alpaca + Anthropic + Perplexity, runs as Python module via cron |
| `trading-bot/.env` | **Shared** secrets — both systems read from here |
| `trading-bot/docker-compose.yml` | Crypto stack orchestration (does NOT include the stocks side) |

## How stocks/ is supposed to run

1. Set up Python venv: `cd stocks && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt`
2. Read env from the parent `.env`: `python -m dotenv -f ../.env run python -m shark.run <phase>`
3. Cron jobs schedule each phase (pre-market, market-open, midday, eod, weekly) — wired via Hermes daemon, NOT system cron.
4. Outputs commit themselves to git at `trading-bot/stocks/kb/`, `trading-bot/stocks/memory/` — same way as the original.

## Going back upstream (if needed)

The origin repo `saijayanth888/shark-trading-agent` is unchanged. To pull future updates:

```bash
cd /tmp && git clone https://github.com/saijayanth888/shark-trading-agent
diff -r /tmp/shark-trading-agent/shark trading-bot/stocks/shark
# manually merge any upstream improvements you want
```

This is a one-way mirror — we do not push back upstream. If the merged version diverges meaningfully and you want to keep them in sync long-term, consider making `stocks/` a git submodule pointing back at the upstream repo.
