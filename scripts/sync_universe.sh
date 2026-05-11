#!/usr/bin/env bash
# sync_universe.sh — mirror user_data/universe.json into .env + freqtrade config.
#
# Edit user_data/universe.json (the source of truth), then run this to keep
# the four downstream surfaces in sync:
#   1. .env  WHEEL_SYMBOLS              ← stocks.wheel_universe
#   2. .env  DASHBOARD_STOCK_SYMBOLS    ← stocks.dashboard_basket
#   3. .env  DASHBOARD_PAIRS            ← crypto.pairs
#   4. user_data/config.json exchange.pair_whitelist ← crypto.pairs
#
# Restart freqtrade + dashboard after running so processes re-read the values.
# Idempotent — re-running with no universe.json changes is a no-op.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIVERSE="$REPO/user_data/universe.json"
ENV="$REPO/.env"
FT_CONFIG="$REPO/user_data/config.json"

if [[ ! -f "$UNIVERSE" ]]; then
    echo "ERROR: $UNIVERSE not found"
    exit 2
fi

echo "── reading universe.json ──"
crypto_pairs=$(python3 -c "import json; print(','.join(json.load(open('$UNIVERSE'))['crypto']['pairs']))")
wheel_symbols=$(python3 -c "import json; print(','.join(json.load(open('$UNIVERSE'))['stocks']['wheel_universe']))")
dash_basket=$(python3 -c "import json; print(','.join(json.load(open('$UNIVERSE'))['stocks']['dashboard_basket']))")

echo "  crypto.pairs:           $(echo $crypto_pairs | tr ',' '\n' | wc -l) symbols"
echo "  stocks.wheel_universe:  $(echo $wheel_symbols | tr ',' '\n' | wc -l) symbols"
echo "  stocks.dashboard_basket: $(echo $dash_basket | tr ',' '\n' | wc -l) symbols"

echo "── syncing .env ──"
for var in "WHEEL_SYMBOLS:$wheel_symbols" "DASHBOARD_STOCK_SYMBOLS:$dash_basket" "DASHBOARD_PAIRS:$crypto_pairs"; do
    name="${var%%:*}"
    val="${var#*:}"
    if grep -q "^${name}=" "$ENV"; then
        sed -i "s|^${name}=.*|${name}=${val}|" "$ENV"
    else
        echo "${name}=${val}" >> "$ENV"
    fi
done
echo "  .env updated"

echo "── syncing freqtrade config.json pair_whitelist ──"
python3 <<PY
import json
from pathlib import Path
uni = json.load(open("$UNIVERSE"))
cfg = json.load(open("$FT_CONFIG"))
new_list = uni["crypto"]["pairs"]
old_list = cfg["exchange"]["pair_whitelist"]
if old_list != new_list:
    cfg["exchange"]["pair_whitelist"] = new_list
    Path("$FT_CONFIG").write_text(json.dumps(cfg, indent=4))
    print(f"  pair_whitelist {len(old_list)} → {len(new_list)} pairs")
else:
    print(f"  pair_whitelist already in sync ({len(new_list)} pairs)")
PY

echo
echo "── DONE ──"
echo "Next steps:"
echo "  • docker compose restart freqtrade        # pick up pair_whitelist + retrain"
echo "  • docker compose restart dashboard        # pick up DASHBOARD_* env vars"
