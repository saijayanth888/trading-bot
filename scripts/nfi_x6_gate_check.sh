#!/usr/bin/env bash
# nfi_x6_gate_check.sh — run the 5 NFI X6 activation gates (or just 1+2 in --dry-run)
#
# Usage:
#   ./scripts/nfi_x6_gate_check.sh             # gates 1, 2, 3 (full backtest, ~10-60 min)
#   ./scripts/nfi_x6_gate_check.sh --dry-run   # gates 1+2 only — fast, no compute
#
# Gate 4 (paper-soak 7d) and Gate 5 (operator GO) are operational, not scriptable.
# Activation (`docker compose --profile nfi up -d freqtrade-nfi`) is NOT performed
# by this script — it's done by the operator after reading the report.
#
# See docs/NFI_X6_ACTIVATION_2026-05-11.md for the runbook.
# See docs/NFI_X6_BACKTEST_REPORT_*.md for the latest measured pass/fail.

set -euo pipefail

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STRATEGY_PATH="${REPO_ROOT}/user_data/strategies/NostalgiaForInfinityX6.py"
CONFIG_PATH="/freqtrade/user_data/strategies/nfi_x6_config.json"
IMAGE="trading-bot/freqtrade:local"
UPSTREAM_RAW="https://raw.githubusercontent.com/iterativv/NostalgiaForInfinity/main/NostalgiaForInfinityX6.py"

cd "${REPO_ROOT}"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "\033[32m✓\033[0m %s\n" "$*"; }
fail() { printf "\033[31m✗\033[0m %s\n" "$*"; }
warn() { printf "\033[33m!\033[0m %s\n" "$*"; }

# ── Gate 1: byte-identical to upstream ──────────────────────────────────────
bold "Gate 1: file integrity (byte-identical to upstream)"
TMPF="$(mktemp)"
trap 'rm -f "${TMPF}"' EXIT
if curl -fsSL "${UPSTREAM_RAW}" -o "${TMPF}"; then
  if diff -q "${TMPF}" "${STRATEGY_PATH}" >/dev/null 2>&1; then
    ok "byte-identical to iterativv/NostalgiaForInfinity@main:NostalgiaForInfinityX6.py"
    GATE1_PASS=1
  else
    fail "diff vs upstream main is non-empty — investigate before activation"
    diff -u "${TMPF}" "${STRATEGY_PATH}" | head -40 || true
    GATE1_PASS=0
  fi
else
  warn "could not fetch upstream; skipping diff. SHA256 of local file:"
  sha256sum "${STRATEGY_PATH}"
  GATE1_PASS=0
fi

# ── Gate 2: dependencies importable in the freqtrade image ─────────────────
bold "Gate 2: rapidjson + pandas_ta + talib importable in ${IMAGE}"
if docker run --rm --entrypoint python "${IMAGE}" -c \
    "import rapidjson, pandas_ta, talib; print('rapidjson', rapidjson.__version__); print('pandas_ta', pandas_ta.version); print('talib', talib.__version__)"; then
  ok "all three deps importable"
  GATE2_PASS=1
else
  fail "dependency import failed"
  GATE2_PASS=0
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  bold "--dry-run: skipping Gate 3 (backtest) and activation"
  if [[ "${GATE1_PASS}" == "1" && "${GATE2_PASS}" == "1" ]]; then
    ok "gates 1+2 PASS"
    exit 0
  else
    fail "gates 1+2 FAIL"
    exit 1
  fi
fi

# ── Gate 3: 2-year backtest ─────────────────────────────────────────────────
bold "Gate 3: 2-year backtest (this can take 10-60 minutes)"
mkdir -p "${REPO_ROOT}/user_data/backtest_results"
docker compose --profile nfi run --rm --no-deps freqtrade-nfi \
  backtesting \
    --config "${CONFIG_PATH}" \
    --strategy NostalgiaForInfinityX6 \
    --timerange 20240501-20260501 \
    --export trades \
    --export-filename /freqtrade/user_data/backtest_results/nfi_x6_2y.json \
  || { fail "backtest run failed"; exit 1; }

bold "Gate 3 results — see user_data/backtest_results/ and parse with scripts/nfi_x6_parse_backtest.py"
ok "gates 1+2+3 ran. Decide activation manually or via the parent runbook."
