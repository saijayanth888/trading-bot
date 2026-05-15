/* dashboard_spa.js — Pair dashboard SPA (React 18, no JSX).
   Ported from /tmp/qtb-handoff/quanta-trading-bot/project/dashboard.jsx.
   Reads ?pair=... and ?venue=... from URL on mount; wires to:
     /api/state                       — sidebar + ML payload
     /api/candles/{base}/{quote}      — OHLCV + indicators
     /api/trades/{base}/{quote}       — entry/exit markers
*/
(function () {
  "use strict";

  const React = window.React;
  const ReactDOM = window.ReactDOM;
  const { useState, useEffect, useMemo, useRef, useCallback } = React;
  const h = React.createElement;
  const F = React.Fragment;

  const {
    NumberRoll, Sparkline, CandleChart, GateBadge,
    Sidebar, Card, ProgressBar, TimeSince,
    HeartbeatDot, KillBar, deriveHeartbeatStatus,
  } = window;
  const CommandPalette = (window.QC && window.QC.CommandPalette) || function () { return null; };

  // ─────────────── helpers ───────────────
  function envelopeData(env) {
    if (env && typeof env === "object" && "data" in env) return env.data;
    return env;
  }
  function fmtClockET() {
    // ET clock — matches the legacy /ops topbar (operator preference, see MIGRATION_NOTES §1).
    try {
      return new Date().toLocaleTimeString("en-US", {
        hour12: true, timeZone: "America/New_York",
        hour: "numeric", minute: "2-digit", second: "2-digit",
      }) + " ET";
    } catch (_) {
      const d = new Date();
      const pad = (n) => String(n).padStart(2, "0");
      return pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds()) + " UTC";
    }
  }
  function fmtUSD(v, frac) {
    if (v == null || isNaN(v)) return "—";
    const f = frac == null ? 2 : frac;
    return v.toLocaleString("en-US", { minimumFractionDigits: f, maximumFractionDigits: f });
  }
  // ET formatter for backend ISO/Date inputs.
  //   includeSeconds: true → "05-14 11:54:32 ET"
  //   includeSeconds: false (default) → "05-14 11:54 ET"
  // Returns "—" for null/invalid input. Operator preference: every
  // timestamp in the SPA renders in America/New_York (see MIGRATION_NOTES §1).
  function fmtET(ts, includeSeconds) {
    if (ts == null || ts === "") return "—";
    let d;
    if (ts instanceof Date) d = ts;
    else if (typeof ts === "number") d = new Date(ts < 1e12 ? ts * 1000 : ts);
    else d = new Date(String(ts));
    if (isNaN(d.getTime())) return "—";
    try {
      const opts = {
        timeZone: "America/New_York",
        month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit",
        hour12: false,
      };
      if (includeSeconds) opts.second = "2-digit";
      // en-GB gives day-first; we want MM-DD HH:MM. Build manually.
      const parts = new Intl.DateTimeFormat("en-US", opts).formatToParts(d);
      const get = (t) => (parts.find(p => p.type === t) || {}).value || "";
      const date = `${get("month")}-${get("day")}`;
      const time = includeSeconds
        ? `${get("hour")}:${get("minute")}:${get("second")}`
        : `${get("hour")}:${get("minute")}`;
      return `${date} ${time} ET`;
    } catch (_) {
      // Fallback: UTC slice
      return String(ts).replace("T", " ").slice(0, 16) + " UTC";
    }
  }
  function fmtPct(v, frac) {
    if (v == null || isNaN(v)) return "—";
    const f = frac == null ? 2 : frac;
    const sign = v >= 0 ? "+" : "";
    return sign + v.toFixed(f) + "%";
  }

  // Convert backend candle format `{time, open, high, low, close}` →
  // the prototype's CandleChart format `{o, h, l, c, t, i}`. The time
  // string is rendered as HH:MM in UTC.
  function toCandles(backendCandles, timeframe) {
    if (!Array.isArray(backendCandles)) return [];
    const minsPerStep = ({ "1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440 })[timeframe] || 5;
    const tz = "America/New_York";
    const fmt = minsPerStep >= 1440
      ? new Intl.DateTimeFormat("en-US", { timeZone: tz, month: "numeric", day: "numeric" })
      : minsPerStep >= 60
        ? new Intl.DateTimeFormat("en-US", { timeZone: tz, month: "numeric", day: "numeric", hour: "numeric", hour12: true })
        : new Intl.DateTimeFormat("en-US", { timeZone: tz, month: "numeric", day: "numeric", hour: "numeric", minute: "2-digit", hour12: true });
    return backendCandles.map((b, i) => {
      const d = new Date((b.time || 0) * 1000);
      const t = fmt.format(d).replace(",", "");
      return { o: Number(b.open), h: Number(b.high), l: Number(b.low), c: Number(b.close), t, i, ts: Number(b.time) };
    });
  }
  // Backend /api/trades/{b}/{q} returns lightweight-charts-formatted markers:
  //   { time, position, color, shape, text }
  // The prototype CandleChart wants:
  //   { i, side: "BUY"|"SELL", price, label }
  // Map by aligning marker.time → candle index using the unsorted unix time list
  // from /api/candles (passed as backendCandles), then parse the price out of
  // the "BUY 81522.83" / "SELL 81690.09 (-23.37)" `text` field.
  function toMarkers(backendMarkers, backendCandles) {
    if (!Array.isArray(backendMarkers) || !Array.isArray(backendCandles) || !backendCandles.length) return [];
    // Build unix-time → index map (binary search by time field).
    const times = backendCandles.map(c => Number(c.time || 0));
    const findIdx = (t) => {
      if (t == null) return times.length - 1;
      // Linear scan is fine for ≤500 candles; find closest.
      let bestIdx = 0;
      let bestDiff = Infinity;
      for (let i = 0; i < times.length; i++) {
        const d = Math.abs(times[i] - t);
        if (d < bestDiff) { bestDiff = d; bestIdx = i; }
      }
      return bestIdx;
    };
    return backendMarkers.map(m => {
      const idx = findIdx(Number(m.time || 0));
      const text = String(m.text || "");
      // "BUY 81522.83" or "SELL 81690.09  (-23.37)"
      const side = text.toUpperCase().startsWith("SELL")
        ? "SELL"
        : text.toUpperCase().startsWith("BUY")
          ? "BUY"
          : (m.shape === "arrowDown" || m.position === "aboveBar") ? "SELL" : "BUY";
      const priceMatch = text.match(/(-?\d+\.?\d*)/);
      const price = priceMatch ? Number(priceMatch[1]) : 0;
      return { i: idx, side, price, label: text };
    });
  }

  // Crypto pairs are fetched from /api/pairs (server is source of truth —
  // configured via DASHBOARD_PAIRS env). Stocks venue stays
  // hardcoded since the endpoint is crypto-only; STOCK_SYMBOLS matches the
  // operator's paper-trading basket (SOFI / PLTR / NVDA / AMD / SPY).
  // Fallbacks if /api/pairs or /api/ops/stocks_sparklines is unreachable.
  // Both are now config-driven on the backend (DASHBOARD_PAIRS env var for
  // crypto, DASHBOARD_STOCK_SYMBOLS env var for stocks) so these arrays
  // only exist as emergency seeds. Operator can't be left with a dropdown
  // that omits tickers they're actively trading — kept in sync with the
  // 12-pair quanta-core crypto basket + 10-symbol wheel/dashboard basket.
  // Minimal emergency seeds — used only if BOTH /api/universe AND /api/pairs
  // are unreachable on mount. Universe.json is the source of truth.
  const FALLBACK_CRYPTO_PAIRS = ["BTC/USD"];
  const FALLBACK_STOCK_SYMBOLS = ["SPY"];

  // ─────────────── TopbarLive ───────────────
  // Replaces the prototype's hardcoded Topbar ($119,842.42 + 1.84%). Wires
  // EQUITY to /api/ops/combined_portfolio.total_equity and the day-delta pill
  // to combined_drawdown_pct (signed: -dd surfaces below-peak as red). Also
  // pulls /api/mode + /api/ops/services.quanta_core for the mode + engine-OK
  // pills. Mirrors the legacy /ops topbar so /dashboard_spa A/Bs cleanly.
  function TopbarLive({ killState, setKillState, combined, mode, services, marketHours, venue, fetchedAt }) {
    const [clock, setClock] = useState(fmtClockET());
    useEffect(() => {
      const t = setInterval(() => setClock(fmtClockET()), 1000);
      return () => clearInterval(t);
    }, []);
    const cp = envelopeData(combined) || {};
    const modeD = envelopeData(mode) || {};
    const svc = envelopeData(services) || {};
    const equity = cp.total_equity != null ? Number(cp.total_equity) : null;
    const dd = cp.combined_drawdown_pct != null ? Number(cp.combined_drawdown_pct) : null;
    // Drawdown is reported as a positive % from peak. For the day-pill we
    // surface a signed delta vs peak (negative = below peak; sign matches the
    // legacy `/ops` topbar's day-delta convention).
    const dayPct = dd != null ? -dd : null;
    const modeLabel = (modeD.mode || "unknown").toUpperCase() + (modeD.dry_run ? " · DRY-RUN" : "");
    const modeCls = modeD.mode === "live" ? "up" : modeD.mode === "paused" ? "warn" : "info";
    // Engine label + probe — quanta_core is the only engine post-cutover.
    const engineName = String(modeD.engine || "").toLowerCase();
    const engineMeta = (engineName === "quanta_core" || svc.quanta_core)
      ? { name: "QUANTA", up: (svc.quanta_core || {}).up }
      : { name: "ENGINE", up: null };
    const ftOk = engineMeta.up === true;
    const hbStatus = deriveHeartbeatStatus({
      services: services,
      mode: mode,
      killState: killState,
    });
    return h(
      "header", { className: "topbar" },
      h("div", { className: "brand" },
        h("div", { className: "v3-brand-stack" },
          h(HeartbeatDot, { status: hbStatus, title: "System health (services + mode)" }),
          h("div", { className: "brand-mark" }, "Q")),
        h("span", { className: "brand-text" }, "QUANTA ",
          h("span", { className: "brand-version" }, "v2.6"))
      ),
      h("div", { className: "tb-group" },
        h("span", { className: "pill " + modeCls },
          h("span", { className: "dot " + modeCls + " pulse" }), " ", modeLabel),
        h("span", { className: "pill " + (engineMeta.up === true ? "up" : engineMeta.up === false ? "down" : "") },
          h("span", { className: "dot " + (engineMeta.up === true ? "up pulse" : engineMeta.up === false ? "down" : "dim") }),
          " ", engineMeta.name, " ", engineMeta.up === true ? "OK" : engineMeta.up === false ? "DOWN" : "—"),
        // NYSE session pill — surfaces ONLY on the Stocks venue tab. The
        // mode/engine pills above reflect the bot process (24/7), not the
        // equity market. Without this pill the operator sees "PAPER · LIVE"
        // green on the Stocks tab after-hours and assumes stocks are
        // trading — they're not (cron phases gate on NYSE hours).
        (venue === "stocks" ? (function() {
          const mh = envelopeData(marketHours) || {};
          if (mh.is_open == null && mh.session == null) {
            return h("span", { className: "pill", title: "loading market hours" },
              h("span", { className: "dot dim" }), " NYSE —");
          }
          // Granular labels match the canonical NYSE schedule:
          //   regular     09:30-16:00 ET → "NYSE OPEN" (green, pulse)
          //   pre_market  06:30-09:30 ET → "NYSE PRE-MKT" (amber, pulse)
          //   after_hours 16:00-20:00 ET → "NYSE AFT-HRS" (amber)
          //   broker_pre  04:00-06:30 ET → "BROKER EXT" (amber, dim)
          //   closed                     → "NYSE CLOSED" (down)
          const session = mh.session || (mh.is_open ? "regular"
            : mh.is_pre_market ? "pre_market"
            : mh.is_after_hours ? "after_hours"
            : mh.is_broker_pre ? "broker_pre"
            : mh.is_extended ? "after_hours" : "closed");
          const map = {
            regular:     { cls: "up",   txt: "NYSE OPEN",    pulse: true,
                           title: "NYSE regular session (09:30-16:00 ET) — stocks phases active" },
            pre_market:  { cls: "warn", txt: "NYSE PRE-MKT", pulse: true,
                           title: "NYSE pre-opening (06:30-09:30 ET) — limited liquidity; most phases idle" },
            after_hours: { cls: "warn", txt: "NYSE AFT-HRS", pulse: false,
                           title: "NYSE after-hours (16:00-20:00 ET) — stocks phases idle until next session" },
            broker_pre:  { cls: "info", txt: "BROKER EXT",   pulse: false,
                           title: "Broker extended-hours window (04:00-06:30 ET) — NYSE pre-opening not yet" },
            closed:      { cls: "down", txt: "NYSE CLOSED",  pulse: false,
                           title: "NYSE closed — next open " + (mh.next_open_utc || "next session")
                                  + (mh.holiday_note ? " · " + mh.holiday_note : "") },
          };
          const m = map[session] || map.closed;
          return h("span", { className: "pill " + m.cls, title: m.title },
            h("span", { className: "dot " + m.cls + (m.pulse ? " pulse" : "") }),
            " ", m.txt);
        })() : null),
        // STOCKS DATA UNTRUSTED pill — surfaces when unified_risk has
        // marked the stocks snapshot too stale to trust for the combined
        // drawdown calc AND NYSE is in the regular session (after-hours
        // staleness is expected). Without this pill, the operator only
        // learns about it via the combined breaker tripping — too late.
        // Source: /api/ops/combined_portfolio {stocks_data_untrusted,
        //         market_open_now} (already polled every 10s for equity).
        (function() {
          const m = envelopeData(marketHours) || {};
          const isOpen = !!(m.is_open || m.session === "regular");
          const cp = envelopeData(combined) || {};
          if (!isOpen) return null;
          if (!cp.stocks_data_untrusted && !cp.stocks_data_stale) return null;
          const ageS = cp.stocks_snapshot_age_s;
          const ageTxt = ageS != null ? Math.round(ageS / 60) + "m" : "—";
          const cls = cp.stocks_data_untrusted ? "down" : "warn";
          const txt = cp.stocks_data_untrusted ? "STOCKS UNTRUSTED" : "STOCKS STALE";
          return h("span", {
            className: "pill " + cls,
            title: "Stocks account snapshot is " + ageTxt + " old · combined-DD calculation degraded · "
                   + "refresh: bash ~/.hermes/scripts/wheel_snapshot.sh",
          },
            h("span", { className: "dot " + cls + " pulse" }),
            " ", txt, " ", ageTxt);
        })()
      ),
      h("div", { className: "tb-divider" }),
      h("div", { className: "tb-group", "data-test": "topbar-equity" },
        h("span", { className: "dim2 mono", style: { fontSize: "var(--t-xs)", letterSpacing: ".08em" } }, "EQUITY"),
        equity != null
          ? h(NumberRoll, { value: equity, decimals: 2, prefix: "$" })
          : h("span", { className: "num dim" }, "—"),
        dayPct != null
          ? h("span", { className: "pill " + (dayPct >= 0 ? "up" : "down"), "data-test": "topbar-daypct" },
              (dayPct >= 0 ? "+" : "") + dayPct.toFixed(2) + "%")
          : null
      ),
      h("span", { className: "tb-spacer" }),
      h("div", { className: "tb-group" },
        h(TimeSince, { ts: fetchedAt, className: "mono dim", style: { fontSize: "var(--t-xs)" } }),
        h("span", { className: "mono dim", style: { fontSize: "var(--t-xs)" } }, clock)
      )
    );
  }

  function DashApp() {
    const [killState, setKillStateRaw] = useState("normal");
    const killStateRef = useRef("normal");
    const setKillState = useCallback((next) => {
      const prev = killStateRef.current;
      killStateRef.current = next;
      setKillStateRaw(next);
      if (next === "killed" && prev !== "killed") {
        fetch("/api/ops/pause", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: "operator kill bar via pair dashboard" }),
        }).catch(() => {});
      } else if (next === "normal" && prev === "killed") {
        fetch("/api/ops/resume", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            reason: "operator kill bar resume via pair dashboard",
            confirm: true,
          }),
        }).catch(() => {});
      }
    }, []);

    const kbPause = useCallback(() => fetch("/api/ops/pause", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: "operator kill bar pause via dashboard spa" }),
    }).then(r => (r.ok ? "PAUSED" : "PAUSE HTTP " + r.status)), []);

    const kbFlatten = useCallback(() => fetch("/api/ops/pause", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: "operator kill bar flatten+halt via dashboard spa" }),
    }).then(r => (r.ok ? "HALT issued" : "HTTP " + r.status)), []);
    const [venue, setVenue] = useState("crypto");
    const [pair, setPair] = useState("BTC/USD");
    const [tf, setTf] = useState("5m");
    const [state, setState] = useState(null);
    const [candles, setCandles] = useState([]);
    const [markers, setMarkers] = useState([]);
    // Live-stream control: operator can pause candle auto-refresh. Refresh
    // interval ms drives the setInterval that re-fetches candles + markers.
    // Default 10s — matches the other fast-cycle fetches on the page.
    const [streamPaused, setStreamPaused] = useState(false);
    const [refreshMs, setRefreshMs] = useState(10_000);
    // Indicators for the RSI/MACD subcharts under the main candle chart.
    // Pulled from /api/candles/{base}/{quote} → {indicators: {...}}.
    // Stocks venue doesn't currently expose these (different pipeline).
    const [indicators, setIndicators] = useState({});
    const [combined, setCombined] = useState(null);
    const [mode, setMode] = useState(null);
    const [services, setServices] = useState(null);
    // NYSE session state — used by the topbar to surface a "NYSE OPEN /
    // CLOSED / EXT-HRS" pill when the Stocks venue tab is active.
    const [marketHours, setMarketHours] = useState(null);
    // Wheel positions snapshot — drives the stocks-venue positions card so
    // selecting NVDA / SOFI / etc. surfaces the open short put / long shares
    // instead of an empty "no positions" panel.
    const [stocksData, setStocksData] = useState(null);
    const [meta, setMeta] = useState({
      candles_fetched_at: null,
      state_fetched_at: null,
      trades_fetched_at: null,
      combined_fetched_at: null,
    });

    // Crypto pair list — fetched from /api/pairs (server has the canonical
    // DASHBOARD_PAIRS env). Falls back to FALLBACK_CRYPTO_PAIRS on a
    // network error so the dropdown still has something selectable.
    const [cryptoPairs, setCryptoPairs] = useState(FALLBACK_CRYPTO_PAIRS);
    // Stocks basket from /api/ops/stocks_sparklines.basket — config-driven
    // via DASHBOARD_STOCK_SYMBOLS env var. Falls back to the 10-symbol seed
    // if the endpoint is unreachable on boot.
    const [stockSymbols, setStockSymbols] = useState(FALLBACK_STOCK_SYMBOLS);
    // Tier C P1-2: helper to detect AbortError; useEffect-mounted fetches
    // pass the controller's signal so component unmount aborts in-flight
    // requests cleanly (no orange "stalled" entries in DevTools Network).
    const isAbortError = (e) => !!(e && (e.name === "AbortError" || (e.message && e.message.indexOf("aborted") !== -1)));

    useEffect(() => {
      // Primary: /api/universe — single source of truth (user_data/universe.json).
      // Falls back to /api/pairs + /api/ops/stocks_sparklines if universe.json
      // is unreachable.
      const ctrl = new AbortController();
      fetch("/api/universe", { signal: ctrl.signal })
        .then(r => r.json())
        .then(uni => {
          if (ctrl.signal.aborted) return;
          const cp = (uni && uni.crypto && uni.crypto.pairs) || [];
          const sb = (uni && uni.stocks && uni.stocks.dashboard_basket) || [];
          if (Array.isArray(cp) && cp.length) setCryptoPairs(cp);
          if (Array.isArray(sb) && sb.length) setStockSymbols(sb);
        })
        .catch((e) => {
          if (isAbortError(e)) return;
          // Fallback 1: /api/pairs for crypto
          fetch("/api/pairs", { signal: ctrl.signal }).then(r => r.json()).then(d => {
            if (ctrl.signal.aborted) return;
            const arr = Array.isArray(d && d.pairs) ? d.pairs : [];
            if (arr.length) setCryptoPairs(arr);
          }).catch((e2) => { if (!isAbortError(e2)) { /* ignore */ } });
          // Fallback 2: stocks_sparklines basket
          fetch("/api/ops/stocks_sparklines", { signal: ctrl.signal }).then(r => r.json()).then(env => {
            if (ctrl.signal.aborted) return;
            const basket = (env && env.data && env.data.basket) || [];
            if (Array.isArray(basket) && basket.length) setStockSymbols(basket);
          }).catch((e2) => { if (!isAbortError(e2)) { /* ignore */ } });
        });
      return () => ctrl.abort();
    }, []);

    // URL params on mount
    useEffect(() => {
      const params = new URLSearchParams(window.location.search);
      const p = params.get("pair");
      const v = params.get("venue");
      if (v === "stocks" || v === "crypto") setVenue(v);
      if (p) setPair(p);
    }, []);

    useEffect(() => {
      // Theme + density are now seeded from localStorage by an inline boot
      // script in templates/dashboard_spa.html before React mounts (B-5).
      document.documentElement.style.setProperty("--accent", "#7c5cff");
    }, []);

    // Fetch /api/state (sidebar / pair drill payload). The endpoint is
    // single-pair so it gives best context for whatever pair quanta-core is
    // showing — we surface it as the "model view" payload.
    //
    // Tier C P1-2 INVARIANT: every fetch issued from these callbacks must
    // pass the externally-supplied signal so the polling useEffect can abort
    // all in-flight requests on unmount or interval rotation.
    const fetchState = useCallback((signal) => {
      fetch("/api/state", { signal }).then(r => r.json()).then(j => {
        if (signal && signal.aborted) return;
        setState(j);
        setMeta(m => Object.assign({}, m, { state_fetched_at: new Date().toISOString() }));
      }).catch((e) => { if (!isAbortError(e)) { /* ignore */ } });
    }, []);

    // Fetch portfolio + mode + service health for the live topbar.
    // /api/ops/combined_portfolio drives the EQUITY NumberRoll and the
    // day-delta pill (signed -combined_drawdown_pct vs peak). /api/mode and
    // /api/ops/services together replace the prototype's mock PAPER / OK pills.
    const fetchTopbar = useCallback((signal) => {
      fetch("/api/ops/combined_portfolio", { signal }).then(r => r.json()).then(j => {
        if (signal && signal.aborted) return;
        setCombined(j);
        setMeta(m => Object.assign({}, m, { combined_fetched_at: new Date().toISOString() }));
      }).catch((e) => { if (!isAbortError(e)) { /* ignore */ } });
      fetch("/api/mode", { signal }).then(r => r.json()).then(j => {
        if (signal && signal.aborted) return;
        setMode(j);
      }).catch((e) => { if (!isAbortError(e)) { /* ignore */ } });
      fetch("/api/ops/services", { signal }).then(r => r.json()).then(j => {
        if (signal && signal.aborted) return;
        setServices(j);
      }).catch((e) => { if (!isAbortError(e)) { /* ignore */ } });
      // NYSE session — server-side cached 60s; polling on the same tick is cheap.
      fetch("/api/ops/market_hours", { signal }).then(r => r.json()).then(j => {
        if (signal && signal.aborted) return;
        setMarketHours(j);
      }).catch((e) => { if (!isAbortError(e)) { /* ignore */ } });
      // Wheel positions (cash, BP, open short puts / covered calls / longs).
      // Polled on the same 10s cadence as the topbar so the per-pair stocks
      // panel reflects today's CSP fires within one tick.
      fetch("/api/ops/stocks", { signal }).then(r => r.json()).then(j => {
        if (signal && signal.aborted) return;
        setStocksData(j);
      }).catch((e) => { if (!isAbortError(e)) { /* ignore */ } });
    }, []);

    // Fetch candles on pair/tf change. Crypto routes through
    // `/api/candles/{base}/{quote}` (coinbase-backed post-cutover). Stocks have NO
    // base/quote split — they route through `/api/ops/stock_candles/{symbol}`
    // (enveloped, Alpaca-cached on disk). The latter uses Alpaca's timeframe
    // codes (`5Min` / `1Hour` / `1Day`) and exposes `bars[]`, not `candles[]`.
    const fetchCandles = useCallback((signal) => {
      const isStock = !pair.includes("/");
      if (isStock) {
        const tfMap = { "1m": "1Min", "5m": "5Min", "15m": "15Min", "1h": "1Hour", "4h": "4Hour", "1d": "1Day" };
        const sym = pair.toUpperCase();
        const url = "/api/ops/stock_candles/" + encodeURIComponent(sym) + "?timeframe=" + encodeURIComponent(tfMap[tf] || "5Min");
        fetch(url, { signal }).then(r => r.json()).then(env => {
          if (signal && signal.aborted) return;
          const d = envelopeData(env) || {};
          const rawCandles = (d.bars || []).slice(-300);
          const cs = toCandles(rawCandles, tf);
          const lastClose = rawCandles.length ? Number(rawCandles[rawCandles.length - 1].close) : null;
          setCandles(cs);
          setMarkers([]);  // no stock trade markers endpoint yet
          setMeta(m => Object.assign({}, m, {
            candles_fetched_at: new Date().toISOString(),
            pair_state: null,
            last_close: lastClose,
            source: "alpaca-cache",
            trades_fetched_at: new Date().toISOString(),
          }));
        }).catch((e) => {
          if (isAbortError(e)) return;
          setCandles([]); setMarkers([]);
        });
        return;
      }
      const [base, quote] = pair.split("/");
      const url = "/api/candles/" + encodeURIComponent(base) + "/" + encodeURIComponent(quote) + "?timeframe=" + encodeURIComponent(tf) + "&limit=300";
      fetch(url, { signal }).then(r => r.json()).then(j => {
        if (signal && signal.aborted) return;
        const rawCandles = j.candles || [];
        const cs = toCandles(rawCandles, tf);
        setCandles(cs);
        setIndicators(j.indicators || {});
        setMeta(m => Object.assign({}, m, { candles_fetched_at: new Date().toISOString(), pair_state: j.pair_state, last_close: j.last_close, source: j.source }));
        // Pull trade markers after candles so we can align them.
        const turl = "/api/trades/" + encodeURIComponent(base) + "/" + encodeURIComponent(quote);
        fetch(turl, { signal }).then(r => r.json()).then(tj => {
          if (signal && signal.aborted) return;
          setMarkers(toMarkers(tj.markers || [], rawCandles));
          setMeta(m => Object.assign({}, m, { trades_fetched_at: new Date().toISOString() }));
        }).catch((e) => { if (!isAbortError(e)) setMarkers([]); });
      }).catch((e) => { if (!isAbortError(e)) setCandles([]); });
    }, [pair, tf]);

    useEffect(() => {
      // Tier C P1-2: shared AbortController for this polling cluster so
      // unmount + dependency-change re-mount both abort all in-flight
      // fetches at once. Previously these calls leaked on tab switch.
      const ctrl = new AbortController();
      const sig = ctrl.signal;
      fetchState(sig);
      fetchCandles(sig);
      fetchTopbar(sig);
      const isvc = setInterval(() => fetchState(sig), 10_000);
      const itb = setInterval(() => fetchTopbar(sig), 10_000);
      // Candle live-stream — driven by refreshMs + streamPaused so operator
      // can throttle or stop the auto-refresh from the chart card header.
      const ic = streamPaused ? null : setInterval(() => fetchCandles(sig), refreshMs);
      return () => {
        clearInterval(isvc); clearInterval(itb);
        if (ic) clearInterval(ic);
        ctrl.abort();
      };
    }, [fetchState, fetchCandles, fetchTopbar, streamPaused, refreshMs]);

    const venuePairs = venue === "crypto" ? cryptoPairs : stockSymbols;
    useEffect(() => {
      // When venue switches and current pair is not in that venue, jump to first.
      //
      // BUG FIX 2026-05-13: previously the deps were [venue, cryptoPairs] which
      // missed stockSymbols. When the user landed via ?pair=NVDA&venue=stocks,
      // stockSymbols was still the FALLBACK ["SPY"] at the time this effect
      // fired (because cryptoPairs landed first → triggered re-run before
      // /api/universe responded). The guard saw venuePairs=["SPY"], pair=NVDA,
      // !includes → setPair("SPY"). Result: NVDA click always redirected to SPY.
      //
      // Fix: depend on stockSymbols too, AND skip the reset while the universe
      // is obviously still in fallback (single-symbol list when we expect a
      // basket of 14). The reset only fires when we genuinely have venue data.
      const isFallback = venue === "stocks" && venuePairs.length <= 1
                         && !venuePairs.includes(pair);
      if (isFallback) return;
      if (venuePairs.length && !venuePairs.includes(pair)) setPair(venuePairs[0]);
    }, [venue, cryptoPairs, stockSymbols]);  // eslint-disable-line react-hooks/exhaustive-deps

    // Build dataset for hero strip
    const pairState = (state && state.pair_state) || (meta && meta.pair_state) || (state || {});
    const px = (meta.last_close != null) ? meta.last_close : (state && state.last_close) || 0;
    // Venue-aware day-delta: when on stocks, surface stocks_drawdown_pct +
    // (stocks_equity − stocks_peak_equity); when on crypto, the crypto
    // drawdown + state.daily_pnl (was freqtrade-only pre-cutover). Bleeding crypto numbers
    // into the stocks view was misleading (operator saw "Crypto day P&L:
    // −$23.37" while looking at SOFI).
    const cpData = envelopeData(combined) || {};
    const ddSrc = venue === "stocks" ? cpData.stocks_drawdown_pct : cpData.crypto_drawdown_pct;
    const ddPct = ddSrc != null ? Number(ddSrc) : null;
    const dayPct = ddPct != null ? -ddPct : 0;
    const recent = (state && state.recent_trades) || [];
    const dayPnlUsd = venue === "stocks"
      ? ((cpData.stocks_equity != null && cpData.stocks_peak_equity != null)
          ? Number(cpData.stocks_equity) - Number(cpData.stocks_peak_equity)
          : 0)
      : (state ? Number(state.daily_pnl || 0) : 0);
    const dayPnlLabel = venue === "stocks" ? "Stocks day P&L: " : "Crypto day P&L: ";
    const regime = state && state.regime;
    const regimeConf = state && state.regime_confidence;
    // Per-pair gate state: crypto pairs are hard-blocked when regime is
    // trending_down (see FreqAIMeanRevV1 regime gate). Stocks route through
    // the wheel runner — gate state isn't surfaced on /api/state yet.
    const gateState = venue === "crypto"
      ? (regime === "trending_down" ? "BLOCK" : "PASS")
      : "PASS";

    return h(F, null,
      h(CommandPalette, {
        variant: "dash",
        dash: { cryptoPairs, stockSymbols, pair, venue, setPair, setVenue },
      }),
      h("div", { className: "app" },
        h(TopbarLive, {
          killState, setKillState,
          combined, mode, services,
          marketHours, venue,
          fetchedAt: meta.combined_fetched_at,
        }),
        h(Sidebar, { active: "dashboard" }),
        h("main", { className: "main" },
          h("div", { className: "page-title" },
            h("h1", null, "Pair dashboard"),
            h("span", { className: "breadcrumb" }, "/ " + pair),
            h("span", { className: "tb-spacer", style: { flex: 1 } }),
            h("div", { className: "venue-tabs" },
              h("button", { className: "venue-tab " + (venue === "crypto" ? "active" : ""), onClick: () => setVenue("crypto") }, "CRYPTO"),
              h("button", { className: "venue-tab " + (venue === "stocks" ? "active" : ""), onClick: () => setVenue("stocks") }, "STOCKS")
            ),
            h("select", { className: "select", value: pair, onChange: e => setPair(e.target.value) },
              venuePairs.map(p => h("option", { key: p, value: p }, p))
            ),
            h("select", { className: "select", value: tf, onChange: e => setTf(e.target.value) },
              ["1m","5m","15m","1h","4h","1d"].map(x => h("option", { key: x, value: x }, x))
            )
          ),

          // HERO ROW
          h("div", { className: "grid g-12", style: { gap: "var(--gap-grid)" } },
            // PRICE STRIP
            h("div", {
              className: "card mountin",
              style: { gridColumn: "span 12", padding: "var(--s-4) var(--s-5)", display: "flex", flexDirection: "row", alignItems: "center", gap: "var(--s-6)", flexWrap: "wrap" }
            },
              h("div", null,
                h("div", { className: "metric-label" }, pair),
                h("div", { style: { display: "flex", alignItems: "baseline", gap: 8, marginTop: 4 } },
                  h("span", { style: { fontSize: "var(--t-3xl)", fontFamily: "var(--mono)", fontWeight: 300, letterSpacing: "-.02em" } },
                    px > 0
                      ? h(NumberRoll, { value: px, decimals: px < 10 ? 4 : (px < 1000 ? 2 : 0), prefix: "$" })
                      : "—"),
                  h("span", { className: "pill " + (dayPct >= 0 ? "up" : "down"), "data-test": "hero-daypct" },
                    fmtPct(dayPct) + " · day")
                ),
                dayPnlUsd !== 0 && h("div", { className: "mono dim", style: { fontSize: "var(--t-2xs)", marginTop: 2 } },
                  dayPnlLabel + (dayPnlUsd >= 0 ? "+$" : "−$") + fmtUSD(Math.abs(dayPnlUsd)))
              ),
              h("div", { className: "vr" }),
              h(Mini2, { lbl: "SOURCE", v: meta.source || "—" }),
              h(Mini2, { lbl: "REGIME", v: regime ? regime.toUpperCase() : "—",
                cls: regime === "trending_up" ? "up" : regime === "trending_down" ? "down" : "info" }),
              h(Mini2, { lbl: "CONF", v: regimeConf != null ? (regimeConf * 100).toFixed(0) + "%" : "—" }),
              h(Mini2, { lbl: "GATE", v: h(GateBadge, { state: gateState }) }),
              h(Mini2, { lbl: "BARS", v: candles.length + " · " + tf }),
              h(Mini2, { lbl: "MARKERS", v: markers.length }),
              h("span", { className: "tb-spacer", style: { flex: 1 } }),
              h(TimeSince, { ts: meta.candles_fetched_at, className: "mono dim", style: { fontSize: "var(--t-xs)" } })
            ),

            // CHART
            h("div", { style: { gridColumn: "span 8", display: "flex", flexDirection: "column", gap: "var(--gap-grid)" } },
              h(Card, {
                num: "01", title: pair + " · " + tf,
                sub: streamPaused
                  ? "stream paused · click LIVE to resume"
                  : "entries + exits annotated · live every " + Math.round(refreshMs / 1000) + "s · scroll = zoom",
                right: h("div", { style: { display: "flex", gap: 6, alignItems: "center" } },
                  // LIVE/PAUSE toggle — pulses green when streaming, red when paused
                  h("button", {
                    className: "icon-btn",
                    onClick: () => setStreamPaused(p => !p),
                    "aria-label": streamPaused ? "Resume live candle stream" : "Pause live candle stream",
                    title: streamPaused ? "Click to resume live stream" : "Click to pause live stream",
                    style: { display: "inline-flex", alignItems: "center", gap: 4 },
                  },
                    h("span", { className: "dot " + (streamPaused ? "down" : "up pulse") }),
                    streamPaused ? "PAUSED" : "LIVE"),
                  // refresh-interval selector — only meaningful when LIVE
                  h("select", {
                    className: "select",
                    value: String(refreshMs),
                    onChange: e => setRefreshMs(parseInt(e.target.value, 10)),
                    "aria-label": "Candle refresh interval",
                    style: { fontFamily: "var(--mono)", fontSize: "var(--t-xs)" },
                    disabled: streamPaused,
                  },
                    [[5000,"5s"],[10000,"10s"],[15000,"15s"],[30000,"30s"],[60000,"1m"]].map(([v, lbl]) =>
                      h("option", { key: v, value: v }, lbl))),
                  // manual refresh button
                  h("button", { className: "icon-btn", onClick: () => fetchCandles(),
                    "aria-label": "Refresh candles now", title: "Refresh now" }, "↻"),
                  ["1m","5m","15m","1h","4h","1d"].map(x =>
                    h("button", { key: x, className: "icon-btn " + (tf === x ? "active" : ""), onClick: () => setTf(x) }, x))
                )
              },
                candles.length > 0
                  ? h(CandleChart, {
                      candles,
                      markers,
                      height: 420,
                      // BB / EMA20 / EMA50 / VWAP overlays from /api/candles indicators
                      overlays: (venue === "crypto" ? {
                        bb_upper: indicators.bb_upper,
                        bb_mid:   indicators.bb_mid,
                        bb_lower: indicators.bb_lower,
                        ema20:    indicators.ema20,
                        ema50:    indicators.ema50,
                        vwap:     indicators.vwap,
                      } : null),
                    })
                  : h("div", { className: "dim", style: { padding: "var(--s-4)", fontSize: "var(--t-xs)" } }, "loading candles…")
              ),
              // RSI subchart — closes the path-A gap (legacy /charts has this).
              // Crypto venue only; stocks pipeline doesn't compute these yet.
              (venue === "crypto" && indicators && indicators.rsi && indicators.rsi.length > 0)
                ? h(Card, { num: "01a", title: "RSI · 14", sub: "" },
                    h(IndicatorSubchart, {
                      data: indicators.rsi,
                      refLines: [
                        { value: 70, color: "rgba(239,68,68,0.5)" },
                        { value: 30, color: "rgba(34,197,94,0.5)" },
                        { value: 50, color: "rgba(255,255,255,0.15)" },
                      ],
                      label: "RSI · 14",
                      color: "var(--accent)",
                    })
                  )
                : null,
              // MACD subchart — line + signal + histogram
              (venue === "crypto" && indicators && indicators.macd && indicators.macd.length > 0)
                ? h(Card, { num: "01b", title: "MACD · 12/26/9", sub: "" },
                    h(IndicatorSubchart, {
                      data: indicators.macd,
                      signal: indicators.macd_signal,
                      hist: indicators.macd_hist,
                      refLines: [{ value: 0, color: "rgba(255,255,255,0.15)" }],
                      label: "MACD",
                      color: "rgba(124,92,255,0.95)",
                    })
                  )
                : null
            ),

            // INTELLIGENCE RAIL
            h("div", { style: { gridColumn: "span 4", display: "flex", flexDirection: "column", gap: "var(--gap-grid)" } },
              h(ModelViewLive, { state, fetchedAt: meta.state_fetched_at }),
              h(MarketContextLive, { state, fetchedAt: meta.state_fetched_at }),
              h(ChampionGenome, { state, fetchedAt: meta.state_fetched_at }),
              h(PnLHistoryCard, { state, fetchedAt: meta.state_fetched_at })
            )
          ),

          // POSITIONS + RECENT TRADES
          h("div", { className: "grid g-12", style: { gap: "var(--gap-grid)" } },
            h("div", { style: { gridColumn: "span 7" } }, h(PositionsForPair, { state, pair, venue, stocksData, fetchedAt: meta.state_fetched_at })),
            h("div", { style: { gridColumn: "span 5" } }, h(RecentTrades, { state, fetchedAt: meta.state_fetched_at }))
          ),

          h("div", { style: { padding: "var(--s-4) 0", textAlign: "center", color: "var(--fg-4)", fontSize: "var(--t-xs)", fontFamily: "var(--mono)" } },
            "QUANTA v2.6 · build " + new Date().toISOString().slice(0, 10))
        )
      ),
      h(KillBar, {
        killState: killState,
        setKillState: setKillState,
        forceOpen: false,
        onPause: kbPause,
        onFlatten: kbFlatten,
        onKill: () => { setKillState("killed"); },
        onResume: () => {
          setKillState("normal");
          return Promise.resolve("RESUME sent");
        },
        resumeDisabled: killState !== "killed",
      })
    );
  }

  function Mini2({ lbl, v, cls: klass }) {
    return h("div", { style: { minWidth: 84, display: "flex", flexDirection: "column" } },
      h("div", { className: "metric-label" }, lbl),
      h("div", { className: "num " + (klass || ""), style: { fontSize: "var(--t-md)", marginTop: 4 } }, v)
    );
  }

  function ModelViewLive({ state, fetchedAt }) {
    const tft = (state && state.tft) || {};
    const meta_signal = state && state.meta_signal;
    const meta_conf = state && state.meta_confidence;
    const meta_strategies = (state && state.meta_strategies) || {};
    const meta_reasoning = state && state.meta_reasoning;
    // TFT probs are 0..1 — display as percentage. Confidence likewise.
    const pct1 = (v) => v != null ? (Number(v) * 100).toFixed(1) + "%" : "—";

    // Wave B (2026-05-14): TFT and META-AGENT are now independent blocks
    // with their own LIVE/OFFLINE states. quanta-core writes meta_signal
    // per cycle (LIVE); the TFT classifier has no replacement producer
    // post-cutover (OFFLINE until Wave D wires it).
    const hasTft = (tft.up != null || tft.flat != null || tft.down != null
                    || tft.confidence != null);
    const hasMeta = (meta_signal != null || meta_conf != null);

    const sig = Number(meta_signal || 0);
    const metaCls = sig > 0.05 ? "up" : sig < -0.05 ? "down" : "info";
    const metaLbl = sig > 0.05 ? "LONG" : sig < -0.05 ? "SHORT" : "HOLD";

    // Card-level pill reflects the strongest live signal in the card.
    const cardPill = hasMeta
      ? h("span", { className: "pill accent" }, h("span", { className: "dot accent pulse" }), " LIVE")
      : h("span", { className: "pill down" }, h("span", { className: "dot down" }), " OFFLINE");

    // H-4 fix: subtitle used to read "TFT · meta-agent" — a Wave D
    // holdover. The inline label below correctly says "MOMENTUM CLASSIFIER"
    // because Wave D renamed the producer to a heuristic momentum
    // classifier (no deep TFT model live yet). Derive the subtitle from
    // tft.classifier so the two never drift again. Falls back to
    // "momentum" if the producer hasn't tagged itself.
    const classifierName = (tft.classifier ? String(tft.classifier) : "momentum")
      .toLowerCase().replace(/_/g, " ");
    const cardSub = classifierName + " · meta-agent";

    return h(Card, {
      num: "02", title: "Model view", sub: cardSub,
      right: h(F, null,
        h(TimeSince, { ts: fetchedAt, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        cardPill
      )
    },
      // Classifier block — LIVE when classifier_log producer writes;
      // OFFLINE until then. Wave D: this is the heuristic momentum
      // classifier from quanta-core (post-cutover replacement for the retired FreqAI TFT). Label
      // accurately so the operator isn't misled.
      h("div", { style: { display: "flex", alignItems: "center", gap: 8 } },
        h("div", { className: "metric-label", style: { flex: 1 } },
          (tft.classifier || "MOMENTUM CLASSIFIER").toUpperCase().replace(/_/g, " ") + " · 5–30 MIN HORIZON"),
        hasTft
          ? h("span", { className: "pill up", style: { height: 14, fontSize: "var(--t-2xs)" } }, "LIVE")
          : h("span", { className: "pill down", style: { height: 14, fontSize: "var(--t-2xs)" } }, "OFFLINE")
      ),
      hasTft
        ? h(F, null,
            h("div", { style: { display: "flex", alignItems: "baseline", gap: "var(--s-3)", margin: "8px 0" } },
              h("div", { style: { flex: 1 } },
                h("div", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, "P(UP)"),
                h("div", { className: "up num", style: { fontSize: "var(--t-xl)" } }, pct1(tft.up))),
              h("div", { style: { flex: 1 } },
                h("div", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, "P(FLAT)"),
                h("div", { className: "num", style: { fontSize: "var(--t-xl)" } }, pct1(tft.flat))),
              h("div", { style: { flex: 1 } },
                h("div", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, "P(DOWN)"),
                h("div", { className: "down num", style: { fontSize: "var(--t-xl)" } }, pct1(tft.down)))
            ),
            h("div", { className: "metric-label", style: { marginTop: 10 } },
              "CONFIDENCE · " + pct1(tft.confidence)),
            h(ProgressBar, { value: (tft.confidence || 0) * 100, max: 100, cls: "accent" }),
            // Feature inputs (momentum, RSI, regime bias, sentiment) — gives
            // the operator a transparent view of why the classifier said what.
            tft.features && Object.keys(tft.features).length > 0
              ? h("div", { className: "mono dim", style: { fontSize: "var(--t-2xs)", marginTop: 6, lineHeight: 1.5 } },
                  "inputs: " + Object.entries(tft.features).map(([k, v]) => k + "=" + (typeof v === "number" ? v.toFixed(3) : v)).join(" · "))
              : null
          )
        : h("div", { className: "dim", style: { fontSize: "var(--t-xs)", lineHeight: 1.55, marginTop: 6, marginBottom: 8 } },
            "No classifier_log rows yet — waiting for quanta-core's next cycle to write the momentum classifier output."
          ),

      h("div", { className: "hr" }),

      // META-AGENT block — LIVE from quanta-core's meta_signal_log.
      h("div", { style: { display: "flex", alignItems: "center", gap: 8, padding: "6px 0" } },
        h("span", { className: "metric-label" }, "META-AGENT"),
        h("span", { className: "tb-spacer", style: { flex: 1 } }),
        hasMeta
          ? h("span", { className: "pill " + metaCls },
              h("span", { className: "dot " + metaCls + (sig !== 0 ? " pulse" : "") }), " ", metaLbl)
          : h("span", { className: "pill down" }, h("span", { className: "dot down" }), " OFFLINE")
      ),
      hasMeta
        ? h(F, null,
            h("div", { className: "dim", style: { fontSize: "var(--t-xs)", lineHeight: 1.55, marginTop: 4 } },
              "Meta signal: ",
              h("span", { className: "num " + metaCls }, sig.toFixed(0)),
              " · meta conf: ",
              h("span", { className: "num" }, pct1(meta_conf))
            ),
            // Per-strategy breakdown — e.g. "mean_rev_bb=FLAT · trend_follow=BUY"
            Object.keys(meta_strategies).length > 0
              ? h("div", { className: "mono dim", style: { fontSize: "var(--t-2xs)", marginTop: 4 } },
                  Object.entries(meta_strategies).map(([s, o]) => s + "=" + o).join(" · "))
              : null,
            meta_reasoning
              ? h("div", { className: "dim", style: { fontSize: "var(--t-2xs)", marginTop: 4, fontStyle: "italic" } },
                  meta_reasoning.length > 140 ? meta_reasoning.slice(0, 140) + "…" : meta_reasoning)
              : null
          )
        : h("div", { className: "dim", style: { fontSize: "var(--t-xs)", lineHeight: 1.55, marginTop: 4 } },
            "Waiting for quanta-core's first cycle to populate meta_signal_log."
          )
    );
  }

  // RegimeGuide — operator-readable cheat sheet showing all four HMM
  // regimes side-by-side with hand-drawn SVG sparkline shapes and per-
  // strategy gate chips (mr / tf). The currently-active regime is
  // highlighted with an accent border + pulse; siblings dim to 0.55.
  //
  // The strategy chip values must stay in sync with the permissive sets
  // in src/quanta_core/strategy/{mean_rev_bb,trend_follow}.py (mirrored
  // server-side in ops_routes.py _MR_PERMISSIVE_REGIMES /
  // _TF_PERMISSIVE_REGIMES). If those gating sets change, update this
  // const in the same commit.
  const REGIME_ROWS = [
    {
      key: "trending_up",
      glyph: "▲",
      cls: "up",
      // monotonically-rising zig-zag inside a 0..1 box
      path: "M0 14 L8 11 L14 13 L22 9 L28 11 L36 6 L44 8 L52 3 L60 5",
      mr: "✓",
      tf: "✓",
      blurb: "positive drift · both strategies fire",
    },
    {
      key: "mean_reverting",
      glyph: "●",
      cls: "warn",
      // symmetric oscillation about the midline — what BTC is doing now
      path: "M0 8 L8 4 L14 12 L22 5 L28 11 L36 4 L44 12 L52 5 L60 8",
      mr: "✓",
      tf: "✕",
      blurb: "chop · only MeanRevBB on BB-lower-band touches",
    },
    {
      key: "trending_down",
      glyph: "▼",
      cls: "down",
      // monotonically-falling zig-zag
      path: "M0 3 L8 5 L14 4 L22 8 L28 6 L36 11 L44 9 L52 13 L60 12",
      mr: "✕",
      tf: "✕",
      blurb: "negative drift · everything blocked, sit in cash",
    },
    {
      key: "high_volatility",
      glyph: "⚡",
      cls: "accent",
      // wide chaotic swings
      path: "M0 8 L8 2 L14 14 L22 3 L28 13 L36 1 L44 15 L52 4 L60 11",
      mr: "✕",
      tf: "✕",
      blurb: "wide swings · entries blocked, noise floor too high",
    },
  ];

  function RegimeGuide({ regime, conf, durHours }) {
    const active = String(regime || "");
    return h("div", null,
      h("div", { style: { display: "flex", alignItems: "center", marginBottom: 6 } },
        h("span", { className: "metric-label" }, "REGIME GUIDE"),
        h("span", { className: "tb-spacer", style: { flex: 1 } }),
        h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } },
          "what the bot does in each market state")
      ),
      h("div", {
        style: {
          display: "grid",
          gridTemplateColumns: "auto 1fr 64px auto",
          gap: "6px 10px",
          alignItems: "center",
          fontSize: "var(--t-xs)",
          padding: "6px 0",
        },
      },
        REGIME_ROWS.flatMap((r) => {
          const isActive = r.key === active;
          const dim = !isActive && active ? { opacity: 0.55 } : null;
          const rowBg = isActive ? {
            background: "color-mix(in srgb, var(--accent) 8%, transparent)",
            border: "1px solid var(--accent)",
            borderRadius: 4,
            margin: "-3px -6px",
            padding: "3px 6px",
          } : { padding: "3px 0" };
          // Each row spans all 4 columns via a wrapper div positioned
          // by gridColumn: span 4 — keeps the active-row border tight.
          return [h("div", {
            key: r.key,
            style: Object.assign({
              gridColumn: "span 4",
              display: "grid",
              gridTemplateColumns: "auto 1fr 64px auto",
              gap: "10px",
              alignItems: "center",
            }, rowBg, dim || {}),
          },
            h("span", { className: r.cls, style: { fontFamily: "var(--mono)", fontSize: "var(--t-sm)", width: 14, textAlign: "center" } },
              isActive ? h("span", { className: "pulse", style: { display: "inline-block" } }, r.glyph) : r.glyph),
            h("span", { className: "mono", style: { letterSpacing: ".02em" } }, r.key),
            h("svg", { width: 60, height: 16, viewBox: "0 0 60 16", style: { display: "block" } },
              h("path", { d: r.path, fill: "none",
                stroke: isActive ? "var(--accent)" : `var(--${r.cls === "accent" ? "fg-2" : r.cls})`,
                strokeWidth: isActive ? 1.6 : 1.2,
                strokeLinecap: "round", strokeLinejoin: "round" })),
            h("span", { className: "mono", style: { fontSize: "var(--t-2xs)", whiteSpace: "nowrap" } },
              "mr ", h("span", { className: r.mr === "✓" ? "up" : "down" }, r.mr),
              " · tf ", h("span", { className: r.tf === "✓" ? "up" : "down" }, r.tf))
          )];
        })
      ),
      // Live state line under the guide.
      h("div", {
        className: "dim mono",
        style: { fontSize: "var(--t-2xs)", marginTop: 6, lineHeight: 1.5, letterSpacing: ".02em" },
      },
        active
          ? (function() {
              const row = REGIME_ROWS.find(r => r.key === active);
              const parts = [
                "live · " + active,
                conf != null ? (Number(conf) * 100).toFixed(1) + "% conf" : null,
                durHours != null ? durHours.toFixed(1) + "h in regime" : null,
                row ? row.blurb : null,
              ].filter(Boolean);
              return parts.join(" · ");
            })()
          : "live · regime not yet classified"
      )
    );
  }

  function MarketContextLive({ state, fetchedAt }) {
    const onchain = (state && state.onchain) || {};
    const durHours = state && state.regime_duration_hours;
    return h(Card, {
      num: "03", title: "Market context", sub: "regime guide · sentiment · on-chain",
      right: h(TimeSince, { ts: fetchedAt, className: "mono dim", style: { fontSize: "var(--t-2xs)" } })
    },
      h(RegimeGuide, {
        regime: state && state.regime,
        conf: state && state.regime_confidence,
        durHours: typeof durHours === "number" ? durHours : null,
      }),
      h("div", { className: "hr" }),
      h("div", { className: "metric-label" }, "SENTIMENT"),
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginTop: 6, fontSize: "var(--t-xs)" } },
        h("span", { className: "dim" }, "Score"),
        h("span", { className: "num " + ((state && state.sentiment_score || 0) >= 0 ? "up" : "down"), style: { textAlign: "right" } },
          state && state.sentiment_score != null
            ? ((Number(state.sentiment_score) >= 0 ? "+" : "") + Number(state.sentiment_score).toFixed(3))
            : "—"),
        h("span", { className: "dim" }, "Confidence"),
        h("span", { className: "num", style: { textAlign: "right" } },
          state && state.sentiment_confidence != null
            ? (Number(state.sentiment_confidence) * 100).toFixed(1) + "%"
            : "—")
      ),
      h("div", { className: "hr" }),
      h("div", { className: "metric-label" }, "ON-CHAIN"),
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginTop: 6, fontSize: "var(--t-xs)" } },
        h("span", { className: "dim" }, "Netflow z"),
        h("span", { className: "num", style: { textAlign: "right" } },
          onchain.netflow_z != null ? Number(onchain.netflow_z).toFixed(2) : "—"),
        h("span", { className: "dim" }, "MVRV"),
        h("span", { className: "num", style: { textAlign: "right" } },
          onchain.mvrv != null ? Number(onchain.mvrv).toFixed(2) : "—"),
        h("span", { className: "dim" }, "Whale 1h"),
        h("span", { className: "num", style: { textAlign: "right" } },
          onchain.whale_count_1h != null ? Number(onchain.whale_count_1h).toFixed(2) : "—")
      )
    );
  }

  // Champion genome sidebar — parity with legacy /charts. Reads state.champion
  // from /api/state. Backend shape (data_sources.fetch_champion):
  //   { generation, champion_id, runner_up_id, champion_fitness }
  function ChampionGenome({ state, fetchedAt }) {
    const c = (state && state.champion) || {};
    const has = c && (c.generation != null || c.champion_id || c.runner_up_id);
    return h(Card, {
      num: "06", title: "Champion genome", sub: "evolution snapshot",
      right: h(TimeSince, { ts: fetchedAt, className: "mono dim", style: { fontSize: "var(--t-2xs)" } })
    },
      !has
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no snapshot yet")
        : h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
            h("span", { className: "dim" }, "Gen"),
            h("span", { className: "num", style: { textAlign: "right" } },
              c.generation != null ? String(c.generation) : "—"),
            h("span", { className: "dim" }, "Champion"),
            h("span", { className: "mono", style: { textAlign: "right" } }, c.champion_id || "—"),
            h("span", { className: "dim" }, "Fitness"),
            h("span", { className: "num", style: { textAlign: "right" } },
              c.champion_fitness != null ? Number(c.champion_fitness).toFixed(3) : "—"),
            h("span", { className: "dim" }, "Runner-up"),
            h("span", { className: "mono", style: { textAlign: "right" } }, c.runner_up_id || "—")
          )
    );
  }

  // P&L history sidebar — parity with legacy /charts. Reads
  // state.daily_pnl_history (backend shape: `{ "YYYY-MM-DD": float_usd }`).
  // Shows the most recent 14 days as a sparkline and the most recent 5 as
  // a dl-style list — operator scans the table while the spark provides shape.
  function PnLHistoryCard({ state, fetchedAt }) {
    const hist = (state && state.daily_pnl_history) || {};
    const days = Object.keys(hist).sort();
    const last14 = days.slice(-14).map(d => Number(hist[d] || 0));
    const last5 = days.slice(-5).reverse();
    const total14 = last14.reduce((a, v) => a + v, 0);
    return h(Card, {
      num: "07", title: "P&L history", sub: days.length + " days · trailing " + Math.min(14, days.length),
      right: h(TimeSince, { ts: fetchedAt, className: "mono dim", style: { fontSize: "var(--t-2xs)" } })
    },
      days.length === 0
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no closed days yet")
        : h(F, null,
            h("div", { className: "metric-label" },
              "14d net · " + (total14 >= 0 ? "+$" : "−$") + fmtUSD(Math.abs(total14))),
            last14.length > 1
              ? h("div", { style: { marginTop: 6, marginBottom: 8 } },
                  h(Sparkline, { data: last14, color: "var(--accent)", fill: false, height: 32 }))
              : null,
            h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
              last5.map(d => {
                const v = Number(hist[d] || 0);
                const cls = v > 0 ? "up" : v < 0 ? "down" : "";
                return h(F, { key: d },
                  h("span", { className: "dim mono" }, d.slice(5)),
                  h("span", { className: "num " + cls, style: { textAlign: "right" } },
                    (v >= 0 ? "+$" : "−$") + fmtUSD(Math.abs(v)))
                );
              })
            )
          )
    );
  }

  function PositionsForPair({ state, pair, venue, stocksData, fetchedAt }) {
    // Stocks venue: route through wheel.open_positions from /api/ops/stocks
    // and filter by underlying ticker == selected pair. Operator complaint
    // (2026-05-11): "if I go to NVDA you said we have purchased right I
    // don't see the data" — the crypto-only positions path returned empty.
    if (venue === "stocks") {
      const wheel = (stocksData && (stocksData.data || {}).wheel) || {};
      const allWheel = wheel.open_positions || [];
      const forPair = allWheel.filter(p => (p.underlying || "").toUpperCase() === pair.toUpperCase());
      const others = allWheel.filter(p => (p.underlying || "").toUpperCase() !== pair.toUpperCase());
      const subText = forPair.length + " on " + pair + " · " + others.length + " other ticker" + (others.length === 1 ? "" : "s");

      return h(Card, {
        num: "04", title: "Open positions · " + pair,
        sub: allWheel.length === 0 ? "no open wheel positions" : subText,
        right: h(TimeSince, { ts: (stocksData && stocksData.checked_at) || fetchedAt, className: "mono dim", style: { fontSize: "var(--t-2xs)" } })
      },
        allWheel.length === 0
          ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no open positions across the wheel basket")
          : forPair.length === 0
            ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } },
                "no open position on " + pair + " · " + others.length + " active on other ticker" + (others.length === 1 ? "" : "s"))
            : h("table", { className: "t" },
                h("thead", null, h("tr", null,
                  h("th", null, "Sym"),
                  h("th", null, "Type"),
                  h("th", { style: { textAlign: "right" } }, "Qty"),
                  h("th", { style: { textAlign: "right" } }, "Strike"),
                  h("th", null, "Expiry"),
                  h("th", { style: { textAlign: "right" } }, "Premium"),
                  h("th", { style: { textAlign: "right" } }, "Collateral")
                )),
                h("tbody", null,
                  forPair.map((p, i) => {
                    const kindLabel = p.kind === "short_put" ? "SHORT PUT"
                      : p.kind === "short_call" ? "SHORT CALL"
                      : p.kind === "long_shares" ? "LONG"
                      : (p.kind || "—");
                    const kindCls = p.kind === "long_shares" ? "up" : "warn";
                    const collateral = p.kind === "short_put" ? Number(p.strike || 0) * Number(p.qty || 1) * 100 : 0;
                    return h("tr", { key: i },
                      h("td", null, h("strong", { className: "mono" }, p.underlying || "—")),
                      h("td", null, h("span", { className: "pill " + kindCls, style: { height: 16, fontSize: "var(--t-2xs)" } }, kindLabel)),
                      h("td", { className: "num", style: { textAlign: "right" } }, p.qty),
                      h("td", { className: "num", style: { textAlign: "right" } }, p.strike != null ? "$" + Number(p.strike).toFixed(2) : "—"),
                      h("td", { className: "dim mono", style: { fontSize: "var(--t-xs)" } }, (p.expiry || "—").slice(0, 10)),
                      h("td", { className: "num up", style: { textAlign: "right" } },
                        "$" + fmtUSD(Number(p.entry_credit || 0) * Number(p.qty || 1))),
                      h("td", { className: "num dim", style: { textAlign: "right" } },
                        collateral > 0 ? "$" + fmtUSD(collateral) : "—")
                    );
                  })
                )
              )
      );
    }
    // Crypto venue: positions from quanta-core's trade_journal writes
    const positions = (state && state.positions) || [];
    const forPair = positions.filter(p => (p.pair || "").toUpperCase() === pair.toUpperCase());
    const others = positions.filter(p => (p.pair || "").toUpperCase() !== pair.toUpperCase());
    return h(Card, {
      num: "04", title: "Open positions",
      sub: forPair.length + " on " + pair + " · " + others.length + " other",
      right: h(TimeSince, { ts: fetchedAt, className: "mono dim", style: { fontSize: "var(--t-2xs)" } })
    },
      positions.length === 0
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no open positions")
        : h("table", { className: "t" },
            h("thead", null, h("tr", null,
              h("th", null, "Pair"),
              h("th", { style: { textAlign: "right" } }, "Open rate"),
              h("th", { style: { textAlign: "right" } }, "Stake"),
              h("th", { style: { textAlign: "right" } }, "Profit"),
              h("th", null, "Opened")
            )),
            h("tbody", null,
              positions.map((p, i) => h("tr", { key: i },
                h("td", null, h("strong", { className: "mono" }, p.pair || "—")),
                h("td", { className: "num", style: { textAlign: "right" } }, p.open_rate != null ? fmtUSD(p.open_rate, p.open_rate < 10 ? 4 : 2) : "—"),
                h("td", { className: "num", style: { textAlign: "right" } }, p.stake_amount != null ? "$" + fmtUSD(p.stake_amount) : "—"),
                h("td", { className: "num " + ((p.current_profit || 0) >= 0 ? "up" : "down"), style: { textAlign: "right" } },
                  p.current_profit != null ? fmtPct(p.current_profit * 100, 2) : "—"),
                h("td", { className: "dim mono", style: { fontSize: "var(--t-xs)" } }, fmtET(p.open_date))
              ))
            )
          )
    );
  }

  function RecentTrades({ state, fetchedAt }) {
    const trades = (state && state.recent_trades) || [];
    // /api/state recent_trades shape:
    //   { pair, opened_at, closed_at, entry_price, exit_price, pnl, pnl_pct,
    //     exit_reason, confidence, regime }
    // pnl_pct is a FRACTION (e.g. -0.012305 = -1.23%). Multiply for display.
    // There is no `side` field — crypto is long-only here, so display LONG.
    return h(Card, {
      num: "05", title: "Recent trades · last 10",
      sub: trades.length + " rows · " + (trades.filter(t => Number(t.pnl_pct || 0) > 0).length) + " green",
      right: h(TimeSince, { ts: fetchedAt, className: "mono dim", style: { fontSize: "var(--t-2xs)" } })
    },
      trades.length === 0
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no recent trades")
        : h("table", { className: "t" },
            h("thead", null, h("tr", null,
              h("th", null, "Pair"),
              h("th", null, "Side"),
              h("th", { style: { textAlign: "right" } }, "Entry"),
              h("th", { style: { textAlign: "right" } }, "Exit"),
              h("th", { style: { textAlign: "right" } }, "PnL %"),
              h("th", null, "Closed"),
              h("th", null, "Reason")
            )),
            h("tbody", null, trades.map((t, i) => {
              const pct = Number(t.pnl_pct != null ? t.pnl_pct : 0) * 100;
              const pnlUp = Number(t.pnl || 0) >= 0;
              const closedAt = t.closed_at || t.opened_at;
              const closedShort = fmtET(closedAt);
              const entryPx = t.entry_price;
              const exitPx = t.exit_price;
              // quanta-core crypto today is long-only, but state.recent_trades will
              // carry wheel rows (short_put / short_call / long_shares) once
              // wheel execution is wired. Prefer the explicit side field when
              // present; fall back to t.kind; finally to LONG.
              const side = t.direction || t.side || (
                t.kind === "short_put"   ? "SHORT PUT"  :
                t.kind === "short_call"  ? "SHORT CALL" :
                t.kind === "long_shares" ? "LONG"       :
                "LONG"
              );
              const sideCls = (side === "SHORT PUT" || side === "SHORT CALL") ? "down" : "up";
              return h("tr", { key: i },
                h("td", null, h("strong", { className: "mono" }, t.pair || "—")),
                h("td", { className: "mono " + sideCls }, side),
                h("td", { className: "num", style: { textAlign: "right" } }, entryPx != null ? fmtUSD(entryPx, entryPx < 10 ? 4 : 2) : "—"),
                h("td", { className: "num", style: { textAlign: "right" } }, exitPx != null ? fmtUSD(exitPx, exitPx < 10 ? 4 : 2) : "—"),
                h("td", { className: "num " + (pnlUp ? "up" : "down"), style: { textAlign: "right" } },
                  t.pnl_pct != null ? fmtPct(pct, 2) : "—"),
                h("td", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, closedShort),
                h("td", { className: "dim", style: { fontSize: "var(--t-2xs)" } }, t.exit_reason || "—")
              );
            }))
          )
    );
  }

  const root = ReactDOM.createRoot(document.getElementById("root"));
  root.render(h(DashApp));
})();
