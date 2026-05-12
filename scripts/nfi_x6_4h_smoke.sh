#!/usr/bin/env bash
# nfi_x6_4h_smoke.sh — Gate 2.5 verifier.
#
# Asserts that for every pair in NFI X6's whitelist, a 4h JSON file exists
# under user_data/data/coinbase/ and contains ≥ MIN_BARS (default 100) bars.
# 100 bars × 4h = 400h ≈ 16.7 days — enough to hydrate every NFI X6 indicator
# (longest lookback ≈ EMA(200) on 4h ≈ 800h ≈ 33 days; informative pairs are
# resampled to 4h inside the strategy so 100 bars covers most rolling windows;
# the seed run should populate 90 days, after which cron keeps it fresh).
#
# Exit codes:
#   0 — all pairs PASS
#   1 — one or more pairs FAIL (missing file OR too few bars)
#   2 — config/missing-deps error (cannot determine pass/fail)
#
# Used by:
#   - scripts/install_nfi_x6.sh (gate 2.5)
#   - operator manual: bash scripts/nfi_x6_4h_smoke.sh

set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CONFIG="${NFI_CONFIG:-$REPO/user_data/strategies/nfi_x6_config.json}"
DATADIR="${NFI_DATADIR:-$REPO/user_data/data/coinbase}"
MIN_BARS="${MIN_BARS:-100}"
QUIET="${QUIET:-0}"

if [[ ! -f "$CONFIG" ]]; then
    echo "FAIL config not found: $CONFIG" >&2
    exit 2
fi

if ! command -v python3 >/dev/null; then
    echo "FAIL python3 not on PATH" >&2
    exit 2
fi

python3 - "$CONFIG" "$DATADIR" "$MIN_BARS" "$QUIET" <<'PY'
import json
import sys
from pathlib import Path

cfg_path = Path(sys.argv[1])
datadir = Path(sys.argv[2])
min_bars = int(sys.argv[3])
quiet = sys.argv[4] == "1"

cfg = json.loads(cfg_path.read_text())
pairs = cfg.get("exchange", {}).get("pair_whitelist") or []
if not pairs:
    print("FAIL no pair_whitelist in config", file=sys.stderr)
    sys.exit(2)

fail = 0
ok = 0
for pair in pairs:
    fname = pair.replace("/", "_") + "-4h.json"
    p = datadir / fname
    if not p.exists():
        print(f"FAIL {pair}: file missing → {p}")
        fail += 1
        continue
    try:
        rows = json.loads(p.read_text())
    except Exception as exc:
        print(f"FAIL {pair}: cannot parse JSON ({exc})")
        fail += 1
        continue
    if not isinstance(rows, list):
        print(f"FAIL {pair}: not a list, got {type(rows).__name__}")
        fail += 1
        continue
    if len(rows) < min_bars:
        print(f"FAIL {pair}: only {len(rows)} bars (< {min_bars} required)")
        fail += 1
        continue
    if not (isinstance(rows[0], list) and len(rows[0]) >= 5):
        print(f"FAIL {pair}: row shape unexpected → {rows[0]!r}")
        fail += 1
        continue
    if not quiet:
        last_ts = rows[-1][0]
        print(f"PASS {pair}: {len(rows)} bars, last_ts_ms={last_ts}")
    ok += 1

print(f"summary: ok={ok} fail={fail} (min_bars={min_bars})")
sys.exit(0 if fail == 0 else 1)
PY
