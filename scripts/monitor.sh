#!/usr/bin/env bash
# monitor.sh — single-pane snapshot of the whole trading system.
# Run anytime: `./scripts/monitor.sh` or `bash scripts/monitor.sh`.
set -u

DASH="${DASHBOARD_URL:-http://localhost:8081}"

python3 - "$DASH" <<'PY'
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

DASH = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8081"

USE_COLOR = sys.stdout.isatty()
def c(code, s):
    if not USE_COLOR: return s
    return f"\033[{code}m{s}\033[0m"
BOLD = lambda s: c("1", s)
DIM  = lambda s: c("2", s)
RED  = lambda s: c("31", s)
GRN  = lambda s: c("32", s)
YEL  = lambda s: c("33", s)
BLU  = lambda s: c("34", s)
CYA  = lambda s: c("36", s)

def hr():
    print(DIM("─" * 72))

def hdr(title):
    print(); print(BOLD(BLU(f"▌ {title}"))); hr()

def fetch(path, method="GET", timeout=4):
    url = f"{DASH}{path}"
    try:
        req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
            return data.get("data") if isinstance(data, dict) and "data" in data else data
    except Exception as exc:
        return {"_error": str(exc)}

def get(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict): return default
        d = d.get(k, default)
    return d

# ── 1. Header ───────────────────────────────────────────────────────
now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
try:
    et_dt = datetime.now().astimezone()
    et = et_dt.strftime("%Y-%m-%d %I:%M %p")
except Exception:
    et = "?"
print(BOLD(CYA("═" * 22 + " TRADING BOT MONITOR " + "═" * 22)))
print(DIM(f"{now_utc} UTC"))

mode = fetch("/api/mode")
m = mode.get("mode","?")
st = mode.get("state","?")
dr = mode.get("dry_run","?")
print(f"Mode: {BOLD(m):<20s}   State: {BOLD(str(st)):<20s}   Dry-run: {BOLD(str(dr))}")

# ── 2. Combined portfolio ───────────────────────────────────────────
hdr("Combined portfolio  (crypto + stocks)")
p = fetch("/api/ops/combined_portfolio")
if p.get("_error"):
    print(f"  {RED('error:')} {p['_error']}")
else:
    te = p.get("total_equity", 0) or 0
    ce = p.get("crypto_equity", 0) or 0
    se = p.get("stocks_equity", 0) or 0
    dd = p.get("combined_drawdown_pct", 0) or 0
    cb = p.get("circuit_breaker_active")
    op_c = p.get("crypto_open_positions", 0) or 0
    op_s = p.get("stocks_open_positions", 0) or 0
    stale = " [STALE]" if p.get("stocks_data_stale") else ""
    print(f"  Total equity:  ${te:>11,.2f}")
    print(f"    Crypto:      ${ce:>11,.2f}   ({op_c} open positions)")
    print(f"    Stocks:      ${se:>11,.2f}   ({op_s} open positions){stale}")
    print(f"  Combined DD:   {dd:>+11.2f}%   threshold {p.get('threshold_pct',10)}%")
    if cb:
        print(f"  Kill switch:   {RED('TRIPPED ⚠')}")
    else:
        print(f"  Kill switch:   {GRN('clear ✓')}")

# ── 3. Live trades ──────────────────────────────────────────────────
hdr("Live trades  (all venues)")
lt = fetch("/api/ops/live_trades")
if lt.get("_error"):
    print(f"  {RED('error:')} {lt['_error']}")
else:
    s = lt.get("summary", {}) or {}
    trades = lt.get("trades", []) or []
    print(f"  Total active: {s.get('total_active',0):>3}   "
          f"crypto: {s.get('crypto_active',0)}   "
          f"stocks: {s.get('shark_active',0)}   "
          f"wheel: {s.get('wheel_active',0)}   "
          f"(paper={s.get('alpaca_paper',True)})")
    if not trades:
        print(DIM("  - no active trades right now -"))
    else:
        print(f"  {'venue':8s}  {'pair':10s}  {'side':6s}  {'qty':>12s}  {'entry':>10s}  {'now':>10s}  {'PnL%':>7s}  {'PnL$':>9s}")
        for t in trades[:20]:
            pnl = t.get('pnl_pct', 0) or 0
            pnl_usd = t.get('pnl_usd', 0) or 0
            pnl_s = f"{pnl:+7.2f}"
            usd_s = f"{pnl_usd:+9.2f}"
            if pnl > 0: pnl_s = GRN(pnl_s); usd_s = GRN(usd_s)
            elif pnl < 0: pnl_s = RED(pnl_s); usd_s = RED(usd_s)
            print(f"  {t.get('kind','?'):8s}  {t.get('label','?'):10s}  "
                  f"{t.get('subkind','?'):6s}  {t.get('qty',0) or 0:>12.4f}  "
                  f"{t.get('entry',0) or 0:>10.4f}  {t.get('current',0) or 0:>10.4f}  "
                  f"{pnl_s}  {usd_s}")
            opened = t.get('opened_at')
            if opened:
                print(DIM(f"            opened: {opened}  ·  {t.get('extra','')}"))

# ── 4. Entry gates ──────────────────────────────────────────────────
hdr("Entry gates  (why pairs are / aren't trading)")
gt = fetch("/api/ops/gates", timeout=8)
if gt.get("_error"):
    print(f"  {RED('error:')} {gt['_error']}")
else:
    print(BOLD("  Crypto:"))
    print(f"  {'pair':10s}  {'regime':14s}  {'P_up':>6s}  {'tft_c':>6s}  {'blockers':30s}")
    for pair in gt.get("crypto", []):
        snap = pair.get("snapshot", {}) or {}
        blockers = [g['gate'] for g in (pair.get('gates') or []) if g.get('pass') is False]
        if not blockers:
            blk = GRN("ELIGIBLE")
        else:
            blk = YEL(", ".join(blockers))
        up = snap.get("up"); tc = snap.get("tft_confidence")
        up_s = f"{up:.3f}" if up is not None else "  -  "
        tc_s = f"{tc:.3f}" if tc is not None else "  -  "
        print(f"  {pair.get('pair','?'):10s}  {pair.get('regime','?'):14s}  {up_s:>6s}  {tc_s:>6s}  {blk}")
    if gt.get("stocks"):
        print()
        print(BOLD("  Stocks (wheel):"))
        print(f"  {'ticker':10s}  {'regime':14s}  {'gates':>8s}  {'blockers':30s}")
        for s in gt.get("stocks", []):
            blockers = [g['gate'] for g in (s.get('gates') or []) if g.get('pass') is False]
            blk = GRN("ELIGIBLE") if not blockers else YEL(", ".join(blockers))
            gates_count = f"{s.get('n_gates',0)-s.get('n_blocking',0)}/{s.get('n_gates',0)} pass"
            print(f"  {s.get('pair','?'):10s}  {s.get('regime','?'):14s}  {gates_count:>8s}  {blk}")

# ── 5. Regime ───────────────────────────────────────────────────────
hdr("Regime detector  (both venues)")
r = fetch("/api/ops/regime")
if not r.get("_error"):
    rg = r.get("current","?")
    pr = r.get("probability", 0) or 0
    dh = r.get("duration_hours", 0) or 0
    print(f"  Crypto (BTC HMM):  {BOLD(str(rg)):<14s}   prob {pr:.2f}   age {dh:.1f}h")
sr = fetch("/api/ops/stock_regime")
if not sr.get("_error"):
    age = (sr.get("data_age_seconds") or 0) / 3600
    print(f"  Stocks (SPY):      {BOLD(str(sr.get('current','?'))):<14s}   prob {sr.get('probability',0) or 0:.2f}   "
          f"structure={sr.get('structure','?')}   age {age:.1f}h")

# ── 6. LLM provider health ──────────────────────────────────────────
hdr("LLM provider health  (Ollama + failover)")
oh = fetch("/api/ops/ollama_health")
if oh.get("_error"):
    print(f"  {YEL('warn:')} ollama_health endpoint says: {oh['_error']}")
else:
    healthy = oh.get("healthy")
    lat = oh.get("last_probe_latency_s", 0) or 0
    fails = oh.get("consecutive_failures", 0) or 0
    badge = GRN("●") if healthy else RED("●")
    print(f"  {badge} ollama healthy: {healthy}    latency: {lat:.2f}s    consecutive failures: {fails}")
    missing = oh.get("models_missing") or []
    if missing:
        print(f"  models missing:    {', '.join(missing)}")
cb = fetch("/api/ops/circuit_breakers")
if not cb.get("_error"):
    breakers = cb.get("breakers") or []
    if not breakers:
        print(DIM("  circuit breakers:  (none registered yet — open after first LLM call)"))
    else:
        for b in breakers:
            stt = b.get("state","?")
            badge = GRN("●") if stt == "CLOSED" else (RED("●") if stt == "OPEN" else YEL("●"))
            print(f"  {badge} CB[{b.get('name','?'):20s}]  state={stt:10s}  fails={b.get('consecutive_failures',0)}")

# ── 7. Services ─────────────────────────────────────────────────────
hdr("Services")
sv = fetch("/api/ops/services")
if sv.get("_error"):
    print(f"  {RED('error:')} {sv['_error']}")
else:
    for name, ss in sv.items():
        if not isinstance(ss, dict): continue
        up = ss.get("up", False)
        badge = GRN("●") if up else RED("○")
        age = ss.get("age_s")
        extra = f"  age={age:.0f}s" if isinstance(age, (int, float)) else ""
        print(f"  {badge} {name:20s}  up={str(up):<6s}{extra}")

# ── 8. Stocks ML ────────────────────────────────────────────────────
hdr("Stocks ML  (Shark TFT)")
sm = fetch("/api/ops/stocks_ml")
if sm.get("_error") and not sm.get("weights_present"):
    print(f"  {DIM('note:')} {sm.get('_error') or 'no model yet'}")
else:
    print(f"  weights present:  {sm.get('weights_present', '?')}")
    print(f"  best val_acc:     {sm.get('best_val_acc')}")
    print(f"  n_train / n_val:  {sm.get('n_train')} / {sm.get('n_val')}")
    print(f"  next train cron:  {sm.get('next_train_cron','?')}")

# ── 9. Champion genome (EPT) ────────────────────────────────────────
hdr("Champion genome  (EPT evolution)")
try:
    req = urllib.request.Request(
        f"{DASH}/api/ops/mcp/get_champion_genome",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=4) as r:
        cd = json.loads(r.read().decode()).get("data") or {}
except Exception as exc:
    cd = {"error": str(exc)}
if cd.get("error"):
    print(f"  {YEL(cd['error'])}")
elif cd.get("note"):
    print(f"  {DIM(cd['note'])}")
else:
    print(f"  champion:   {cd.get('member_id','?')}")
    print(f"  fitness:    {cd.get('fitness')}")
    g = cd.get("genome") or {}
    if g:
        print(f"  genome:     {len(g)} keys")

# ── 10. Containers ──────────────────────────────────────────────────
hdr("Containers")
try:
    out = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}|{{.Status}}|{{.Ports}}"],
        capture_output=True, text=True, timeout=4,
    ).stdout.strip().splitlines()
    for line in out[:10]:
        parts = line.split("|", 2)
        if len(parts) < 2: continue
        name, status = parts[0], parts[1]
        ports = parts[2] if len(parts) > 2 else ""
        badge = GRN("●") if "healthy" in status or "Up" in status else RED("○")
        print(f"  {badge} {name:20s}  {status}")
except Exception as exc:
    print(f"  {YEL('docker ps failed:')} {exc}")

print()
print(DIM(f"refresh: run {sys.argv[0]} again  ·  dashboard: {DASH}"))
print()
PY
