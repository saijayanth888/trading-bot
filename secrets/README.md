# Secrets

Drop credential files here. They are mounted **read-only** into the
freqtrade container at `/run/secrets/trading-bot/`.

## Coinbase API key (recommended path)

1. Go to https://www.coinbase.com/settings/api → **New API Key**
2. Grant **View** + **Trade** for the portfolio you want the bot to use
3. Set an **IP allowlist** to your Spark host's outbound IP — strongly recommended
4. Coinbase will offer a JSON download named like `cdp_api_key_<id>.json`
5. Save that file as **`secrets/coinbase.json`** in this directory

The compose `freqtrade` service forwards
`COINBASE_KEY_FILE=/run/secrets/trading-bot/coinbase.json` automatically; the
SDK loads it via `RESTClient(key_file=...)`. No need to escape multi-line PEM
data inside `.env`.

## What's gitignored

This directory's contents (except `README.md` and `.gitkeep`) are excluded
from version control via `.gitignore`. Never commit the real key.
