#!/usr/bin/env bash
#
# Telegram bot setup walkthrough for the trading-bot Hermes Agent gateway.
#
# Usage:
#   ./hermes-mcp/setup_telegram.sh
#
# Hermes uses Telegram for real-time trade alerts and interactive commands
# (/pause, /resume, /status). Slack stays as the structured-report channel
# (see .hermes/skills/slack_reporting.md).
#
# This script is idempotent — safe to run multiple times.

set -euo pipefail

ENV_FILE="${ENV_FILE:-$HOME/Documents/trading-bot/.env}"
HERMES_ENV="${HERMES_ENV:-$HOME/.hermes/.env}"

cat <<'BANNER'

────────────────────────────────────────────────────────────────────────
Telegram bot setup for Hermes Agent
────────────────────────────────────────────────────────────────────────

Step 1 — Create the bot via @BotFather on Telegram:
  • Open Telegram, search for @BotFather, send /newbot
  • Choose a display name (e.g. "DGX Trading Bot")
  • Choose a username ending in "_bot" (e.g. "dgx_trading_bot")
  • BotFather replies with an HTTP API token like:
      1234567890:AAH...EXAMPLE...xyz
  • Copy that token.

Step 2 — Get your personal chat_id:
  • Open Telegram, search for your new bot, send any message ("hi")
  • Run: curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | jq '.result[-1].message.chat.id'
  • The number that prints is your chat_id (positive int for DM,
    negative for groups).

Step 3 — Paste both values when prompted below. They get written
into the env files locally; nothing is sent over the network.

BANNER

read -r -p "Telegram bot token (or 'skip' to leave placeholder): " TOKEN
if [[ "$TOKEN" == "skip" || -z "$TOKEN" ]]; then
    TOKEN="your_token_here"
    CHAT_ID="your_chat_id_here"
    echo "[setup_telegram] leaving placeholders — re-run when you have the token."
else
    read -r -p "Your Telegram chat_id (digits, may be negative): " CHAT_ID
    [[ -n "$CHAT_ID" ]] || { echo "chat_id required" >&2; exit 1; }
fi

write_or_replace() {
    local file="$1" key="$2" value="$3"
    [[ -f "$file" ]] || touch "$file"
    if grep -q "^${key}=" "$file"; then
        local tmp; tmp="$(mktemp)"
        awk -v k="$key" -v v="$value" -F= 'BEGIN{OFS="="} {if ($1==k) print k,v; else print}' "$file" >"$tmp"
        mv "$tmp" "$file"
        chmod 600 "$file"
    else
        printf '\n# Telegram bot for Hermes Agent gateway\n%s=%s\n' "$key" "$value" >>"$file"
        chmod 600 "$file"
    fi
}

write_or_replace "$ENV_FILE"   "TELEGRAM_BOT_TOKEN" "$TOKEN"
write_or_replace "$ENV_FILE"   "TELEGRAM_CHAT_ID"   "$CHAT_ID"
write_or_replace "$HERMES_ENV" "TELEGRAM_BOT_TOKEN" "$TOKEN"
write_or_replace "$HERMES_ENV" "TELEGRAM_CHAT_ID"   "$CHAT_ID"

cat <<EOF

[setup_telegram] wrote TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to:
  • $ENV_FILE
  • $HERMES_ENV  (so Hermes Agent + cron jobs see them)

Step 4 — Register Telegram with Hermes Agent:
  hermes platform configure telegram
    (or paste TOKEN / CHAT_ID into ~/.hermes/config.yaml under platforms.telegram)

Step 5 — Smoke test:
  curl -s "https://api.telegram.org/bot\$TELEGRAM_BOT_TOKEN/sendMessage" \\
    -d chat_id=\$TELEGRAM_CHAT_ID -d text='Hermes Agent connected.'

Once the test message arrives, the risk-monitor cron (every 15 min) will
deliver alerts here.
EOF
