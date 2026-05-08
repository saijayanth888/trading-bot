#!/usr/bin/env bash
#
# Emergency stop — flip the bot back to dry-run, cancel every open
# Coinbase order, snapshot state, alert Slack.
#
# This is a *kill switch*, not a graceful exit. Steps run in order; any
# step that fails logs and continues so the next one still executes.
#
# Usage:
#   ./scripts/emergency_stop.sh "reason text"
#
# The reason is passed to the Slack alert.

set -uo pipefail

REASON="${1:-manual_trigger}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${ROOT_DIR}/user_data/config.json"
SNAPSHOT_DIR="${ROOT_DIR}/user_data/snapshots/$(date -u '+%Y-%m-%dT%H-%M-%SZ')"
LOG="${ROOT_DIR}/user_data/logs/emergency_stop.log"

mkdir -p "$(dirname "$LOG")" "$SNAPSHOT_DIR"

log() {
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [emergency_stop] $*" | tee -a "$LOG"
}

log "TRIGGERED reason='${REASON}'"

# 1. Flip dry_run=true so the next iteration of the bot stops sending real orders.
log "step 1/4: flipping dry_run=true in config.json"
python3 - "$CONFIG" <<'PY' || log "step 1 FAILED — config patch error"
import json, os, sys
path = sys.argv[1]
with open(path) as f:
    cfg = json.load(f)
prev = cfg.get("dry_run", None)
cfg["dry_run"] = True
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=4)
os.replace(tmp, path)
print(f"dry_run: {prev} -> True")
PY

# 2. Cancel every open Coinbase order via the SDK. This *bypasses* freqtrade's
#    order tracking — freqtrade will see the cancellations on its next poll
#    and reconcile the trade rows.
log "step 2/4: cancelling all open Coinbase orders"
python3 - <<'PY' >> "$LOG" 2>&1 || log "step 2 FAILED — order cancellation error (likely SDK or auth)"
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "user_data"))
def main():
    api_key = os.environ.get("COINBASE_API_KEY")
    api_secret = os.environ.get("COINBASE_API_SECRET")
    if not api_key or not api_secret:
        print("[stop] COINBASE_API_KEY/SECRET missing — skipping cancel step")
        return
    try:
        from coinbase.rest import RESTClient
    except ImportError:
        print("[stop] coinbase-advanced-py not installed — cannot cancel orders")
        return
    client = RESTClient(api_key=api_key, api_secret=api_secret)
    try:
        resp = client.list_orders(order_status="OPEN")
        orders = getattr(resp, "orders", None) or (
            resp["orders"] if isinstance(resp, dict) and "orders" in resp else []
        )
        ids = []
        for o in orders:
            oid = getattr(o, "order_id", None) or (
                o["order_id"] if isinstance(o, dict) and "order_id" in o else None
            )
            if oid:
                ids.append(str(oid))
        if not ids:
            print("[stop] no open orders to cancel")
            return
        print(f"[stop] cancelling {len(ids)} open orders: {ids}")
        client.cancel_orders(order_ids=ids)
        print(f"[stop] cancel batch sent for {len(ids)} orders")
    except Exception as exc:
        print(f"[stop] cancel call failed: {exc!r}")
        raise

main()
PY

# 3. Snapshot state — config copy, journal CSV, recent freqtrade logs.
log "step 3/4: writing state snapshot to ${SNAPSHOT_DIR}"
{
    cp -f "$CONFIG" "${SNAPSHOT_DIR}/config.json" 2>/dev/null || true
    python3 - "${SNAPSHOT_DIR}/journal.csv" <<'PY' || true
import csv, os, sys
out = sys.argv[1]
dsn = os.environ.get("DATABASE_URL",
    "postgresql://tradebot:tradebot-change-me@localhost:5433/tradebot")
try:
    import psycopg
    from psycopg.rows import dict_row
except Exception as exc:
    print(f"[snap] psycopg missing: {exc}")
    sys.exit(0)
try:
    with psycopg.connect(dsn, connect_timeout=5) as c:
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM trade_journal ORDER BY opened_at DESC LIMIT 500"
            )
            rows = list(cur.fetchall())
    if rows:
        with open(out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(rows[0].keys())
            for r in rows:
                w.writerow([r[k] for k in r.keys()])
        print(f"[snap] journal → {out} ({len(rows)} rows)")
    else:
        print("[snap] journal empty")
except Exception as exc:
    print(f"[snap] journal export failed: {exc}")
PY
    if [[ -f "${ROOT_DIR}/user_data/logs/freqtrade.log" ]]; then
        tail -n 500 "${ROOT_DIR}/user_data/logs/freqtrade.log" \
            > "${SNAPSHOT_DIR}/freqtrade.log.tail" 2>/dev/null || true
    fi
    docker compose -f "${ROOT_DIR}/docker-compose.yml" ps \
        > "${SNAPSHOT_DIR}/docker-compose-ps.txt" 2>&1 || true
} 2>&1 | tee -a "$LOG"

# 4. Slack alert. Failure here is non-fatal.
log "step 4/4: posting Slack alert"
SLACK_REASON="$REASON" python3 - <<'PY' >> "$LOG" 2>&1 || log "step 4 FAILED — slack post error"
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "user_data"))
def main():
    try:
        from modules.slack_alerts import SlackAlerter
    except Exception as exc:
        print(f"[stop] slack_alerts import failed: {exc}")
        return
    s = SlackAlerter.from_env()
    if not s.enabled:
        print("[stop] slack alerter not enabled (SLACK_WEBHOOK_URL unset)")
        return
    try:
        ok = s.notify_risk_critical("emergency_stop", 1.0, 0.0)
        s.notify_error(
            component="emergency_stop.sh",
            exc=os.environ.get("SLACK_REASON", "manual_trigger"),
            context={"snapshot": os.environ.get("SNAPSHOT_DIR", "")},
        )
        print(f"[stop] slack alert sent ok={ok}")
    except Exception as exc:
        print(f"[stop] slack notify failed: {exc!r}")

main()
PY

# Restart the container so it picks up dry_run=true. Do this LAST so any
# fresh exit_signal it emits during the kill flow uses a real connection.
log "restarting freqtrade container so dry_run takes effect"
docker compose -f "${ROOT_DIR}/docker-compose.yml" restart freqtrade \
    >> "$LOG" 2>&1 || log "container restart failed"

log "DONE — emergency stop complete (snapshot=${SNAPSHOT_DIR})"
