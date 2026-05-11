/* ops_spa.js — Ops console SPA (React 18, no JSX, no Babel).
   Ported from /tmp/qtb-handoff/quanta-trading-bot/project/ops.jsx with all
   D2.PAIRS / D2.GATES / D2.RESEARCH_FEED / etc. mock-data reads replaced by
   live fetches against the FastAPI ops endpoints in ops_routes.py.

   Primitives (Card, NumberRoll, Sparkline, KillSwitch, GateBadge, Topbar,
   Sidebar, LiveTicker, ProgressBar, RegimeRibbon, TimeSince) come from
   qc_react.js — they're attached to window by that file.

   Mount: ReactDOM.createRoot(document.getElementById("root")).render(<OpsApp />)
*/
(function () {
  "use strict";

  const React = window.React;
  const ReactDOM = window.ReactDOM;
  const { useState, useEffect, useMemo, useRef, useCallback } = React;
  const h = React.createElement;
  const F = React.Fragment;

  // Primitives exposed by qc_react.js
  const {
    NumberRoll, Sparkline, RegimeRibbon, StatusRow, GateBadge, KillSwitch,
    Topbar, Sidebar, Card, LiveTicker, ProgressBar, TimeSince,
  } = window;

  // ─────────────── helpers ───────────────
  function cls(...xs) { return xs.filter(Boolean).join(" "); }
  function fmtUSD(v, frac) {
    if (v == null || isNaN(v)) return "—";
    const f = frac == null ? 2 : frac;
    return v.toLocaleString("en-US", { minimumFractionDigits: f, maximumFractionDigits: f });
  }
  function fmtPct(v, frac) {
    if (v == null || isNaN(v)) return "—";
    const f = frac == null ? 2 : frac;
    const sign = v >= 0 ? "+" : "";
    return sign + v.toFixed(f) + "%";
  }
  function durToHM(hours) {
    if (hours == null) return "—";
    if (hours >= 24) return Math.floor(hours / 24) + "d";
    const m = Math.round((hours - Math.floor(hours)) * 60);
    return Math.floor(hours) + "h " + String(m).padStart(2, "0") + "m";
  }
  function safeJsonFetch(url, opts) {
    return fetch(url, opts).then(r => r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)));
  }
  function envelopeData(env) {
    if (env && typeof env === "object" && "data" in env) return env.data;
    return env;
  }
  function envelopeStatus(env) {
    if (env && typeof env === "object" && "status" in env) return env.status;
    return null;
  }
  function envelopeError(env) {
    if (env && typeof env === "object" && "error" in env) return env.error;
    return null;
  }

  // ─────────────── card state helpers ───────────────
  // Returns a normalised view of one fetch slot. Cards use it to decide
  // between "loading", "down" (envelope.status === "down" or fetch threw),
  // and "ok". When down/loading, the card body renders the placeholder
  // instead of trying to render fields that may not exist.
  function slotState(data, key) {
    const env = data[key];
    const err = data[key + "_error"];
    const fetchedAt = data[key + "_fetched_at"];
    if (err) return { phase: "down", reason: String(err), env: null, fetchedAt };
    if (env == null) return { phase: "loading", reason: null, env: null, fetchedAt };
    const s = envelopeStatus(env);
    if (s === "down") {
      return { phase: "down", reason: envelopeError(env) || "endpoint reported down", env, fetchedAt };
    }
    return { phase: "ok", reason: null, env, fetchedAt };
  }

  // Right-side TimeSince + optional extras for every card head.
  function cardRight(fetchedAt, extras) {
    return h(F, null,
      h(TimeSince, { ts: fetchedAt, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
      extras || null
    );
  }

  // Auto-ticking "retry in Ns" — countdown from last fetch + period seconds.
  function RetryCountdown({ fetchedAt, period = 10 }) {
    const [, force] = useState(0);
    useEffect(() => {
      const iv = setInterval(() => force(n => n + 1), 1000);
      return () => clearInterval(iv);
    }, []);
    let remain = "—";
    if (fetchedAt) {
      const ms = Date.now() - new Date(fetchedAt).getTime();
      const sec = Math.max(0, Math.ceil(period - ms / 1000));
      remain = sec + "s";
    }
    return h("span", { className: "mono dim" }, remain);
  }

  function EmptyState({ reason, fetchedAt, period }) {
    return h("div", {
      className: "dim",
      style: {
        display: "flex", flexDirection: "column", gap: 6,
        padding: "var(--s-3) var(--s-2)", fontSize: "var(--t-xs)",
        background: "var(--bg-inset)", borderRadius: 4,
      }
    },
      h("div", { style: { color: "var(--warn)", fontFamily: "var(--mono)", letterSpacing: ".08em" } },
        "ENDPOINT UNAVAILABLE"),
      h("div", null, reason || "no response from endpoint"),
      h("div", { className: "mono dim", style: { fontSize: "var(--t-2xs)" } },
        "retry in ", h(RetryCountdown, { fetchedAt, period: period || 10 }))
    );
  }

  function LoadingState() {
    return h("div", { className: "dim", style: { fontSize: "var(--t-xs)", padding: "var(--s-2) 0" } }, "loading…");
  }

  // ─────────────── data hook ───────────────
  // Fires N parallel fetches on mount; refetches the fast group every 10s and
  // the slow group every 60s. Each envelope is stored under its key plus a
  // `_fetched_at` timestamp the cards' TimeSince components can use.
  const FAST_ENDPOINTS = {
    mode: "/api/mode",
    combined_portfolio: "/api/ops/combined_portfolio",
    regime: "/api/ops/regime",
    stock_regime: "/api/ops/stock_regime",
    services: "/api/ops/services",
    gates: "/api/ops/gates",
    sparklines: "/api/ops/sparklines",
    stocks_sparklines: "/api/ops/stocks_sparklines",
    trades_risk: "/api/ops/trades_risk",
    live_trades: "/api/ops/live_trades",
    stocks_ml: "/api/ops/stocks_ml",
    stocks: "/api/ops/stocks",
    ollama_health: "/api/ops/ollama_health",
    circuit_breakers: "/api/ops/circuit_breakers",
    llm_stats: "/api/ops/llm_stats",
    mcp: "/api/ops/mcp",
    sentiment: "/api/ops/sentiment",
    // stocks_sentiment endpoint removed 2026-05-11 — see ops_routes.py
    // comment. Shark Briefing card (data-num 13c) is the source of truth
    // for per-symbol stocks sentiment via Shark's analyst pipeline.
    shark_briefing: "/api/ops/shark_briefing",
  };
  const SLOW_ENDPOINTS = {
    ept_champion: { url: "/api/ops/mcp/get_champion_genome", method: "POST", body: {} },
    training: "/api/ops/training",
    readiness: "/api/ops/readiness",
    regime_config: "/api/ops/regime_config",
    slack_preview: "/api/ops/slack_preview",
    tools: "/api/ops/tools",
  };

  function useOpsData() {
    const [state, setState] = useState({});
    const stateRef = useRef(state);
    stateRef.current = state;

    const fetchOne = useCallback((key, urlOrSpec) => {
      const isSpec = typeof urlOrSpec === "object";
      const url = isSpec ? urlOrSpec.url : urlOrSpec;
      const opts = isSpec
        ? { method: urlOrSpec.method || "GET",
            headers: { "Content-Type": "application/json" },
            body: urlOrSpec.method === "POST" ? JSON.stringify(urlOrSpec.body || {}) : undefined }
        : undefined;
      return safeJsonFetch(url, opts)
        .then(env => {
          setState(s => Object.assign({}, s, {
            [key]: env,
            [key + "_fetched_at"]: new Date().toISOString(),
            [key + "_error"]: null,
          }));
        })
        .catch(err => {
          setState(s => Object.assign({}, s, {
            [key + "_fetched_at"]: new Date().toISOString(),
            [key + "_error"]: String(err && err.message || err),
          }));
        });
    }, []);

    const refetchFast = useCallback(() => {
      Object.entries(FAST_ENDPOINTS).forEach(([k, u]) => fetchOne(k, u));
    }, [fetchOne]);
    const refetchSlow = useCallback(() => {
      Object.entries(SLOW_ENDPOINTS).forEach(([k, spec]) => fetchOne(k, spec));
    }, [fetchOne]);

    useEffect(() => {
      refetchFast();
      refetchSlow();
      const ifast = setInterval(refetchFast, 10_000);
      const islow = setInterval(refetchSlow, 60_000);
      return () => { clearInterval(ifast); clearInterval(islow); };
    }, [refetchFast, refetchSlow]);

    return { state, refetchFast, refetchSlow };
  }

  // ─────────────── TODAY SCOREBOARD — single-card at-a-glance summary ─────
  // Operator's stated need (2026-05-11): "top right corner, daily P&L,
  // capital, trades done, all the things". This card distills 6 numbers
  // that answer "where are we right now" without scrolling.
  function TodayScoreboard({ data }) {
    const cpSlot = slotState(data, "combined_portfolio");
    const cp = envelopeData(cpSlot.env) || {};
    const tr = envelopeData(data.trades_risk) || {};
    const stocks = envelopeData(data.stocks) || {};
    const wheelOpen = ((stocks.wheel || {}).open_positions || []).length;

    const equity = Number(cp.total_equity ?? 0);
    const peak = Number(cp.combined_peak_equity ?? equity);
    const dd = Math.abs(Number(cp.combined_drawdown_pct ?? 0));
    // Closed-trade day P&L (from trade_journal, server-side at ops_routes:2549).
    // This is the "realized" component — only moves when a trade closes.
    const closedPnl = Number(cp.day_pnl_usd ?? 0);
    // Live unrealized P&L on open positions — this is what makes the number
    // TICK with market moves. Operator complaint (2026-05-11 ~3 PM): "I don't
    // see the drop in -23.37, that is not getting changed" because the page
    // was showing closed-only. Sum crypto-unrealized (sources.crypto_unrealised_pnl
    // — freqtrade hot-quotes) and stocks day-move (stocks_equity − stocks_peak_equity,
    // captures wheel MTM since the wheel_snapshot cron now fires every minute).
    const srcs = cp.sources || {};
    const cryptoUnrl = Number(srcs.crypto_unrealised_pnl ?? 0);
    const stocksEq = Number(cp.stocks_equity ?? 0);
    const stocksPeak = Number(cp.stocks_peak_equity ?? stocksEq);
    const stocksMove = stocksEq - stocksPeak;
    const liveDayPnl = closedPnl + cryptoUnrl + stocksMove;
    // Percent against starting combined capital — use peak as a sane proxy
    // when peak ≈ start (early in the campaign). Operator-readable %.
    const baseCap = peak > 0 ? peak : equity;
    const liveDayPct = baseCap > 0 ? (liveDayPnl / baseCap) * 100 : 0;
    const closedToday = Number(tr.closed_today ?? 0);
    const openCrypto = Number(tr.open_count ?? 0);
    const totalOpen = openCrypto + wheelOpen;
    const dayCls = liveDayPnl >= 0 ? "up" : "down";
    const ddCls = dd >= 8 ? "down" : dd >= 5 ? "warn" : "up";

    const stat = (lbl, val, cls) => h("div", { style: { display: "flex", flexDirection: "column", gap: 2, minWidth: 110 } },
      h("div", { className: "dim2 mono", style: { fontSize: "var(--t-2xs)", letterSpacing: ".08em", textTransform: "uppercase" } }, lbl),
      h("div", { className: "num " + (cls || ""), style: { fontSize: "var(--t-lg)", fontFamily: "var(--mono)", fontWeight: 500, fontVariantNumeric: "tabular-nums" } }, val)
    );

    return h(Card, {
      num: "00", title: "Today · scoreboard",
      sub: "live · realized + unrealized · refreshes every 10s",
      right: cardRight(cpSlot.fetchedAt,
        h("span", { className: "pill " + dayCls, style: { height: 18 } },
          h("span", { className: "dot " + dayCls + (liveDayPnl === 0 ? "" : " pulse") }),
          " ", (liveDayPct >= 0 ? "+" : "") + liveDayPct.toFixed(2) + "% live"))
    },
      h("div", { style: { display: "flex", flexWrap: "wrap", gap: "var(--s-5)", alignItems: "baseline" } },
        stat("Capital", "$" + fmtUSD(equity)),
        stat("Live P&L", (liveDayPnl >= 0 ? "+$" : "−$") + fmtUSD(Math.abs(liveDayPnl)), dayCls),
        stat("Realized today", (closedPnl >= 0 ? "+$" : "−$") + fmtUSD(Math.abs(closedPnl)),
          closedPnl >= 0 ? "up" : "down"),
        stat("Unrealized", (cryptoUnrl + stocksMove >= 0 ? "+$" : "−$") + fmtUSD(Math.abs(cryptoUnrl + stocksMove)),
          (cryptoUnrl + stocksMove) >= 0 ? "up" : "down"),
        stat("Drawdown", dd.toFixed(2) + "%", ddCls),
        stat("Peak", "$" + fmtUSD(peak)),
        stat("Open", totalOpen + " (" + openCrypto + "C + " + wheelOpen + "S)"),
        stat("Closed today", closedToday)
      )
    );
  }

  // ─────────────── HERO — combined equity + 3-cell status ───────────────
  function HeroLive({ data, killState }) {
    const slot = slotState(data, "combined_portfolio");
    const cp = envelopeData(slot.env) || {};
    const tr = envelopeData(data.trades_risk) || {};
    const stocksEnv = envelopeData(data.stocks) || {};
    const stocksAlpaca = stocksEnv.alpaca || {};
    // The combined_portfolio envelope is flat: `crypto_equity`, `stocks_equity`,
    // `total_equity`, `combined_peak_equity`, `combined_drawdown_pct`,
    // `circuit_breaker_active`, plus the newly-added `day_pnl_usd` and
    // `day_pnl_pct` (closed-trade day P&L from trade_journal).
    const cryptoEq = Number(cp.crypto_equity || 0);
    const stocksEq = Number(cp.stocks_equity || stocksAlpaca.portfolio_value || 0);
    const equity = cp.total_equity != null ? Number(cp.total_equity) : (cryptoEq + stocksEq);
    const peak = cp.combined_peak_equity != null ? Number(cp.combined_peak_equity) : equity;
    // Day P&L — closed-trade day P&L from trade_journal, computed server-side
    // at ops_routes.py:2549-2550 (commit 58ea6b2). Backend always sets these
    // fields (defaults to 0.0 on enrichment failure), so no fallback needed.
    // day_pnl_pct is already × 100 on the server.
    const dayPnl = Number(cp.day_pnl_usd ?? 0);
    const dayPct = Number(cp.day_pnl_pct ?? 0);
    // Per-leg day P&L (kept for the Mini strip).
    const cryptoStart = Number((cp.sources && cp.sources.crypto_starting_equity) || cp.crypto_peak_equity || cryptoEq || 1);
    const cryptoDayPnl = cryptoEq - Number(cp.crypto_peak_equity || cryptoStart);
    const cryptoDayPct = cryptoStart > 0 ? (cryptoDayPnl / cryptoStart) * 100 : 0;
    const stocksStart = Number((cp.sources && cp.sources.stocks_starting_equity) || cp.stocks_peak_equity || stocksEq || 1);
    const stocksDayPnl = stocksEq - Number(cp.stocks_peak_equity || stocksStart);
    const stocksDayPct = stocksStart > 0 ? (stocksDayPnl / stocksStart) * 100 : 0;
    const dd = Math.abs(Number(cp.combined_drawdown_pct || 0));
    const pauseTh = Number(cp.threshold_pct || 10) * 0.8;
    const killTh = Number(cp.threshold_pct || 10);
    // Drawdown bar (#5): width = abs(dd)/10 * 100; color up<5%, warn 5–8%, down ≥8%.
    const ddBarMax = 10;
    const ddCls = dd >= 8 ? "down" : dd >= 5 ? "warn" : "up";

    const sparks = envelopeData(data.sparklines);
    const seriesPair = sparks && sparks.pairs && Object.values(sparks.pairs)[0];
    const series = (seriesPair && seriesPair.closes && seriesPair.closes.length)
                  ? seriesPair.closes : [equity || 1, equity || 1, equity || 1];

    const live = envelopeData(data.live_trades) || {};
    const liveSummary = live.summary || {};
    const ticker = (live.trades || []).map((t, i) => ({
      t: -1 * (i + 1),
      pair: t.label, side: (t.subkind || "").includes("short") ? "SELL" : "BUY",
      qty: t.qty || 0,
      px: t.current || t.entry || 0,
      pnl: t.pnl_usd || 0,
      venue: t.kind === "crypto" ? "Coinbase" : "Alpaca",
    }));
    const mode = envelopeData(data.mode) || {};
    const modeLabel = (mode.mode || "unknown").toUpperCase();
    const modeCls = mode.mode === "live" ? "up" : mode.mode === "paused" ? "warn" : "info";

    return h(F, null,
      ticker.length ? h(LiveTicker, { items: ticker }) : null,
      h("section", { className: "grid g-12", style: { gap: "var(--gap-grid)" } },
        h("div", { className: "card mountin", style: { gridColumn: "span 6", position: "relative", overflow: "hidden" } },
          h("div", { style: { padding: "var(--s-4) var(--s-5) 0", display: "flex", alignItems: "baseline", gap: "var(--s-3)" } },
            h("span", { className: "metric-label" }, "COMBINED EQUITY · CRYPTO + STOCKS"),
            h("span", { className: "pill " + modeCls }, h("span", { className: "dot " + modeCls + " pulse" }), " ", modeLabel),
            h("span", { className: "tb-spacer", style: { flex: 1 } }),
            h(TimeSince, { ts: data.combined_portfolio_fetched_at, className: "mono dim", style: { fontSize: "var(--t-xs)" } })
          ),
          slot.phase === "down"
            ? h("div", { style: { padding: "var(--s-2) var(--s-5) var(--s-4)" } },
                h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 }))
            : h("div", { style: { padding: "var(--s-2) var(--s-5) var(--s-2)", display: "flex", alignItems: "flex-end", gap: "var(--s-6)" } },
            h("div", { id: "hero-equity-value", "data-equity": equity, style: { fontSize: "var(--t-hero)", fontWeight: 300, lineHeight: 1, letterSpacing: "-.025em" } },
              h(NumberRoll, { value: equity, decimals: 2, prefix: "$", className: "num" })
            ),
            h("div", { style: { display: "flex", flexDirection: "column", gap: 6, paddingBottom: 14 } },
              h("span", { className: (dayPnl >= 0 ? "up" : "down") + " num", style: { fontSize: "var(--t-xl)" } },
                (dayPnl >= 0 ? "+$" : "−$") + fmtUSD(Math.abs(dayPnl))),
              h("span", { className: (dayPnl >= 0 ? "up" : "down") + " num", style: { fontSize: "var(--t-base)" } },
                fmtPct(dayPct) + " · day"),
              h("span", { className: "dim mono", style: { fontSize: "var(--t-xs)" } },
                "DD " + dd.toFixed(2) + "% / pause " + pauseTh.toFixed(0) + "% / kill " + killTh.toFixed(0) + "%"),
              h("div", { style: { width: 180, marginTop: 4 } },
                h(ProgressBar, { value: dd, max: ddBarMax, ticks: [pauseTh, killTh], cls: ddCls })
              )
            )
          ),
          h("div", { style: { height: 90, padding: "0 var(--s-3) var(--s-3)" } },
            h(Sparkline, { data: series, color: "--up", height: 90 })
          ),
          h("div", { style: { display: "flex", padding: "var(--s-3) var(--s-5)", borderTop: "1px solid var(--line-1)", gap: "var(--s-6)", flexWrap: "wrap" } },
            h(Mini, { lbl: "CRYPTO", v: "$" + fmtUSD(cryptoEq, 0), d: fmtPct(cryptoDayPct), up: cryptoDayPct >= 0 }),
            h(Mini, { lbl: "STOCKS", v: "$" + fmtUSD(stocksEq, 0), d: fmtPct(stocksDayPct), up: stocksDayPct >= 0 }),
            h(Mini, { lbl: "OPEN", v: (liveSummary.total_active || 0) + " positions", d: (liveSummary.crypto_active || 0) + " cr · " + (liveSummary.wheel_active || 0) + " st" }),
            h(Mini, { lbl: "CLOSED 24h", v: (tr.closed_today || 0) + " trades", d: fmtUSD(tr.daily_pnl_usd || 0, 2) + " USD", up: (tr.daily_pnl_usd || 0) >= 0 }),
            h(Mini, { lbl: "BREAKER", v: cp.circuit_breaker_active ? "TRIPPED" : "armed", d: "pause " + pauseTh.toFixed(0) + "%" })
          )
        ),
        h("div", { className: "grid", style: { gridColumn: "span 6", gridTemplateRows: "1fr 1fr", gap: "var(--gap-grid)" } },
          h("div", { className: "grid g-2", style: { gap: "var(--gap-grid)" } },
            h(RegimeCellLive, { venue: "CRYPTO", sym: "BTC", env: data.regime, fetchedAt: data.regime_fetched_at }),
            h(RegimeCellLive, { venue: "STOCKS", sym: "SPY", env: data.stock_regime, fetchedAt: data.stock_regime_fetched_at })
          ),
          h("div", { className: "grid g-2", style: { gap: "var(--gap-grid)" } },
            h(BotStateCellLive, { mode: mode, killState: killState, data: data }),
            h(ResearchPulseLive, { data: data })
          )
        )
      )
    );
  }

  function Mini({ lbl, v, d, up }) {
    return h("div", { style: { minWidth: 96, display: "flex", flexDirection: "column" } },
      h("div", { className: "metric-label" }, lbl),
      h("div", { className: "num", style: { fontSize: "var(--t-md)", marginTop: 2 } }, v),
      h("div", { className: "mono " + (up ? "up" : "dim"), style: { fontSize: "var(--t-xs)", marginTop: 2 } }, d || "")
    );
  }

  function RegimeCellLive({ venue, sym, env, fetchedAt }) {
    const d = envelopeData(env) || {};
    const cur = (d.current || "unknown").toLowerCase();
    const conf = Number(d.probability || 0);
    const dur = d.duration_hours;
    const regimeBucket =
      cur === "trending_up" ? "BULL"
      : cur === "trending_down" ? "BEAR"
      : cur === "high_volatility" ? "VOL"
      : cur === "mean_reverting" ? "RANGE"
      : "UNK";
    const klass = regimeBucket === "BULL" ? "up" : regimeBucket === "BEAR" ? "down" : "info";
    const segments =
      regimeBucket === "BULL" ? [{kind:"bull",weight:70},{kind:"range",weight:20},{kind:"bear",weight:10}]
      : regimeBucket === "BEAR" ? [{kind:"bear",weight:65},{kind:"range",weight:25},{kind:"bull",weight:10}]
      : [{kind:"range",weight:60},{kind:"bull",weight:25},{kind:"bear",weight:15}];
    return h("div", { className: "card mountin", style: { padding: "var(--s-3) var(--s-4)", justifyContent: "space-between", minHeight: 132, gap: 6 } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "var(--s-2)" } },
        h("span", { className: "metric-label" }, venue + " · " + sym),
        h("span", { className: "tb-spacer", style: { flex: 1 } }),
        h(TimeSince, { ts: fetchedAt, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        h("span", { className: "pill " + klass }, h("span", { className: "dot " + klass + " pulse" }), " ", regimeBucket)
      ),
      h("div", { style: { display: "flex", alignItems: "baseline", justifyContent: "space-between", margin: "var(--s-2) 0" } },
        h("span", { className: "num", style: { fontSize: "var(--t-2xl)", letterSpacing: "-.02em" } },
          Math.round(conf * 100),
          h("span", { style: { fontSize: "var(--t-md)", color: "var(--fg-3)" } }, "%")
        ),
        h("span", { className: "mono dim", style: { fontSize: "var(--t-xs)" } },
          "conf · " + (dur != null ? durToHM(dur) : "—"))
      ),
      h(RegimeRibbon, { segments: segments })
    );
  }

  function BotStateCellLive({ mode, killState, data }) {
    // Derive posture from regime + open positions so the pill doesn't say
    // "RUNNING" alongside a "TRENDING DOWN" regime — that confused the
    // operator on legacy /ops; mirror the same fix here.
    const regimeEnv = envelopeData(data.regime) || {};
    const cryptoDown = String(regimeEnv.current || "").toLowerCase() === "trending_down";
    const liveEnv = envelopeData(data.live_trades) || {};
    const openCount = (liveEnv.trades || []).length || 0;
    const klass = killState === "killed" ? "down" : killState === "armed" ? "warn"
              : mode.state === "running" ? "up"
              : mode.state === "paused" ? "warn" : "info";
    let lbl;
    if (killState === "killed") lbl = "KILLED";
    else if (killState === "armed") lbl = "ARMED";
    else if (mode.state === "running") {
      if (openCount > 0)     lbl = "ACTIVE · IN TRADE";
      else if (cryptoDown)   lbl = "ACTIVE · HOLD (DOWN)";
      else                   lbl = "ACTIVE · READY";
    } else {
      lbl = (mode.state || "—").toUpperCase();
    }
    const champEnv = envelopeData(data.ept_champion) || {};
    const champion = (champEnv.member_id || champEnv.genome_id || champEnv.id || "—");
    const metrics = champEnv.metrics || {};
    const sharpeRaw = metrics.sharpe_ratio != null ? metrics.sharpe_ratio : metrics.sharpe;
    const sharpe = sharpeRaw != null ? Number(sharpeRaw).toFixed(2) : "—";
    const services = envelopeData(data.services) || {};
    const ftStatus = (services.freqtrade && services.freqtrade.up) ? "freqtrade · ok"
                   : (services.freqtrade ? "freqtrade · down" : "—");
    return h("div", { className: "card mountin", style: { padding: "var(--s-3) var(--s-4)" } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "var(--s-2)" } },
        h("span", { className: "metric-label" }, "BOT STATE"),
        h("span", { className: "tb-spacer", style: { flex: 1 } }),
        h("span", { className: "pill " + klass }, h("span", { className: "dot " + klass + " pulse" }), " ", lbl)
      ),
      h("div", { style: { marginTop: 10, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "ENGINE"),    h("div", { className: "num" }, ftStatus),
        h("div", { className: "dim mono" }, "MODE"),      h("div", { className: "num" }, (mode.mode || "—") + (mode.dry_run ? " · dry" : "")),
        h("div", { className: "dim mono" }, "CHAMPION"),  h("div", { className: "num accent" }, champion + " · sh " + sharpe),
        h("div", { className: "dim mono" }, "STRATEGY"),  h("div", { className: "num" }, "EPT")
      )
    );
  }

  function ResearchPulseLive({ data }) {
    const sent = envelopeData(data.sentiment) || {};
    const events = sent.key_events || [];
    const first = events[0];
    const firstTitle = typeof first === "string"
      ? first
      : (first && (first.title || first.headline)) || "";
    return h("div", { className: "card mountin", style: { padding: "var(--s-3) var(--s-4)", position: "relative", overflow: "hidden" } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "var(--s-2)" } },
        h("span", { className: "metric-label" }, "LIVE RESEARCH · SENTIMENT"),
        h("span", { className: "tb-spacer", style: { flex: 1 } }),
        h("span", { className: "pill accent" }, h("span", { className: "dot accent pulse" }), " AGENT")
      ),
      h("div", { style: { marginTop: 10 } },
        h("div", { className: "tl-source accent" },
          sent.score != null ? ("Aggregate " + (sent.score >= 0 ? "+" : "") + Number(sent.score).toFixed(2)) : "—"),
        h("div", { className: "num", style: { fontSize: "var(--t-md)", marginTop: 4, color: "var(--fg-1)" } },
          firstTitle || "no key events"),
        h("div", { className: "dim", style: { fontSize: "var(--t-xs)", marginTop: 4, lineHeight: 1.5 } },
          "Headlines: " + (sent.n_headlines || 0) + " · Fear&Greed: " + (sent.fear_greed != null ? sent.fear_greed : "—")
          + (sent.fear_greed_label ? " (" + sent.fear_greed_label + ")" : ""))
      )
    );
  }

  // ─────────────── AGENT TIMELINE — 24h cron axis ───────────────
  // Real cron jobs from the cron table (reference_trading_bot_paths.md).
  const CRON_JOBS = [
    { h:  0, dur: 8,  name: "Genome cycle",         kind: "evo", desc: "EPT genome cycle" },
    { h:  1, dur: 4,  name: "Sentiment sweep",      kind: "rsh", desc: "Sentiment fast pass" },
    { h:  2, dur: 6,  name: "On-chain pull",        kind: "rsh", desc: "Glassnode / on-chain" },
    { h:  4, dur: 4,  name: "Sentiment sweep",      kind: "rsh", desc: "Sentiment fast pass" },
    { h:  6, dur: 12, name: "Macro brief",          kind: "rsh", desc: "WSJ / FT / Reuters" },
    { h:  9, dur: 6,  name: "Retrain TFT",          kind: "ml",  desc: "Rolling TFT retrain" },
    { h: 12, dur: 6,  name: "DRL refresh",          kind: "ml",  desc: "PPO/DQN policy update" },
    { h: 15, dur: 8,  name: "Daily Slack brief",    kind: "rpt", desc: "Hermes assembles + posts" },
    { h: 18, dur: 8,  name: "Walk-forward eval",    kind: "ml",  desc: "OOS Sharpe gate" },
    { h: 21, dur: 4,  name: "Risk rebalance",       kind: "risk",desc: "Pair weights from corr" },
  ];

  function AgentTimeline() {
    const hourNow = new Date().getUTCHours() + new Date().getUTCMinutes() / 60;
    const colorOf = (k) => ({
      rsh: "var(--info)", ml: "var(--accent)", evo: "var(--warn)",
      risk: "var(--down)", rpt: "var(--up)",
    }[k] || "var(--fg-3)");

    return h(Card, {
      num: "03", title: "Agent timeline · 24h",
      sub: "UTC · now " + String(Math.floor(hourNow)).padStart(2, "0") + ":" + String(Math.floor((hourNow % 1) * 60)).padStart(2, "0"),
      right: h("div", { className: "tb-group", style: { display: "flex", gap: 8 } },
        h("span", { className: "pill", style: { borderColor: "var(--info-line)", color: "var(--info)" } }, "● RESEARCH"),
        h("span", { className: "pill", style: { borderColor: "var(--accent-line)", color: "var(--accent)" } }, "● ML"),
        h("span", { className: "pill", style: { borderColor: "var(--warn-line)", color: "var(--warn)" } }, "● EVO"),
        h("span", { className: "pill", style: { borderColor: "var(--down-line)", color: "var(--down)" } }, "● RISK"),
        h("span", { className: "pill", style: { borderColor: "var(--up-line)", color: "var(--up)" } }, "● REPORT")
      )
    },
      h("div", { style: { position: "relative", height: 80, marginTop: 4 } },
        Array.from({ length: 25 }).map((_, hi) =>
          h("div", { key: hi, style: {
            position: "absolute", left: ((hi / 24) * 100) + "%", top: 0, bottom: 0,
            width: 1, background: hi % 6 === 0 ? "var(--line-2)" : "var(--line-1)",
          } })
        ),
        CRON_JOBS.map((j, i) => {
          const top = 8 + (i % 5) * 12;
          const left = (j.h / 24) * 100;
          const w = (j.dur / 60) * (100 / 24);
          const passed = j.h < hourNow;
          return h("div", {
            key: i, className: "tt",
            "data-tt": String(j.h).padStart(2, "0") + ":00 · " + j.name + " · " + j.desc,
            style: {
              position: "absolute", left: left + "%", top, width: "max(28px, " + (w * 4) + "%)", height: 8,
              background: colorOf(j.kind), opacity: passed ? 0.5 : 1, borderRadius: 2,
            }
          });
        }),
        h("div", { style: {
          position: "absolute", left: (hourNow / 24) * 100 + "%", top: -4, bottom: -4,
          width: 2, background: "var(--accent)", boxShadow: "0 0 12px var(--accent)",
        } },
          h("div", { style: {
            position: "absolute", top: -12, left: -22, fontFamily: "var(--mono)",
            fontSize: "var(--t-2xs)", color: "var(--accent)", letterSpacing: ".1em",
          } }, "NOW")
        )
      ),
      h("div", { style: { display: "flex", justifyContent: "space-between", marginTop: 8, fontFamily: "var(--mono)", fontSize: "var(--t-2xs)", color: "var(--fg-3)" } },
        ["00","04","08","12","16","20","24"].map(hh => h("span", { key: hh }, hh + ":00"))
      ),
      h("div", { className: "hr" }),
      h("div", { style: { display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "var(--s-4)" } },
        CRON_JOBS.filter(j => j.h > hourNow).slice(0, 3).map((j, i) =>
          h("div", { key: i },
            h("div", { className: "tl-source", style: { color: colorOf(j.kind) } },
              "NEXT · " + String(j.h).padStart(2, "0") + ":00 UTC"),
            h("div", { className: "num", style: { marginTop: 4 } }, j.name),
            h("div", { className: "dim", style: { fontSize: "var(--t-xs)", marginTop: 2 } }, j.desc)
          )
        )
      )
    );
  }

  // ─────────────── RESEARCH STREAM — real activity feed ───────────────
  // Ported from templates/ops.html "Research stream · synthesises real activity".
  // Synthesises a unified event log from six endpoints:
  //   /api/ops/regime          — transitions_24h
  //   /api/ops/live_trades     — currently-open positions
  //   /api/ops/mcp             — last_call
  //   /api/ops/sentiment       — current aggregate
  //   /api/ops/ollama_health   — current health
  //   /api/ops/circuit_breakers — any not-CLOSED breaker
  // Each item is { src, title, body, cites, level, ts (ms), age_s }.
  function buildResearchFeed(data) {
    const nowMs = Date.now();
    const items = [];

    // Regime transitions
    const reg = envelopeData(data.regime) || {};
    (reg.transitions_24h || []).slice(0, 6).forEach(t => {
      const ts = new Date(t.ts).getTime();
      items.push({
        ts,
        age_s: Math.max(0, Math.floor((nowMs - ts) / 1000)),
        src: "BTC HMM",
        level: t.regime === "trending_up" ? "up" : t.regime === "trending_down" ? "down" : "warn",
        title: "Regime → " + (t.regime || "?").replace(/_/g, " "),
        body: "Held for " + (t.duration_h != null ? t.duration_h.toFixed(1) + "h" : "—") + " before transition.",
        cites: ["ts: " + t.ts, "regime: " + t.regime, "duration_h: " + t.duration_h],
      });
    });

    // Active trades
    const tl = (envelopeData(data.live_trades) || {}).trades || [];
    tl.forEach(t => {
      let ts = nowMs;
      if (t.opened_at) {
        const raw = String(t.opened_at).replace(" ", "T");
        const parsed = new Date(raw.endsWith("Z") || raw.includes("+") ? raw : raw + "Z").getTime();
        if (!isNaN(parsed)) ts = parsed;
      }
      const pnlPct = t.pnl_pct;
      const pnlUsd = t.pnl_usd;
      items.push({
        ts,
        age_s: Math.max(0, Math.floor((nowMs - ts) / 1000)),
        src: (t.kind || "trade").toUpperCase(),
        level: pnlPct == null ? "accent" : pnlPct >= 0 ? "up" : "down",
        title: "Open · " + (t.label || t.pair || "?"),
        body: (t.subkind || "long") + " @ " + (t.entry || 0).toLocaleString("en-US", { maximumFractionDigits: 4 })
              + " · now " + (t.current || 0).toLocaleString("en-US", { maximumFractionDigits: 4 })
              + (pnlPct != null
                  ? (" · " + (pnlPct >= 0 ? "+" : "") + pnlPct.toFixed(2) + "% ("
                     + (pnlUsd >= 0 ? "+" : "") + Number(pnlUsd || 0).toFixed(2) + ")")
                  : ""),
        cites: [
          "opened_at: " + (t.opened_at || "—"),
          "entry: " + t.entry,
          "current: " + t.current,
          "pnl_pct: " + pnlPct,
          "regime@entry: " + (t.extra || "—"),
        ],
      });
    });

    // MCP last call
    const lc = (envelopeData(data.mcp) || {}).last_call;
    if (lc && lc.ts) {
      const raw = String(lc.ts);
      const ts = new Date(raw.endsWith("Z") || raw.includes("+") ? raw : raw + "Z").getTime();
      items.push({
        ts,
        age_s: Math.max(0, Math.floor((nowMs - ts) / 1000)),
        src: "HERMES MCP",
        level: "accent",
        title: "Tool called · " + (lc.tool || "?"),
        body: (lc.raw || "").slice(0, 240),
        cites: ["ts: " + lc.ts, "tool: " + lc.tool],
      });
    }

    // Sentiment aggregate
    const s = envelopeData(data.sentiment);
    if (s) {
      const score = Number(s.score || 0);
      const direction = score > 0.1 ? "bullish" : score < -0.1 ? "bearish" : "neutral";
      const ts = s.ts ? new Date(s.ts).getTime() : nowMs - 30_000;
      items.push({
        ts,
        age_s: Math.max(0, Math.floor((nowMs - ts) / 1000)),
        src: "SENTIMENT",
        level: score > 0 ? "up" : score < 0 ? "down" : "accent",
        title: "Aggregate " + direction + " (" + (score >= 0 ? "+" : "") + score.toFixed(2) + ")",
        body: (s.n_headlines || 0) + " headlines · agreement " + (s.agreement ? "yes" : "no")
              + (s.fear_greed != null ? " · F&G " + s.fear_greed + " " + (s.fear_greed_label || "") : ""),
        cites: [
          "score: " + s.score,
          "confidence: " + s.confidence,
          "fast_score: " + s.fast_score,
          "deep_score: " + s.deep_score,
          "n_headlines: " + s.n_headlines,
        ],
      });
    }

    // Ollama health
    const oh = envelopeData(data.ollama_health);
    if (oh) {
      const lat = oh.last_probe_latency_s;
      const ts = oh.timestamp ? new Date(oh.timestamp).getTime() : nowMs - 120_000;
      items.push({
        ts,
        age_s: Math.max(0, Math.floor((nowMs - ts) / 1000)),
        src: "OLLAMA",
        level: oh.healthy ? "up" : "warn",
        title: "Health probe · " + (oh.healthy ? "OK" : "DEGRADED"),
        body: "Latency " + (lat != null ? Number(lat).toFixed(2) + "s" : "—")
              + " · consecutive failures " + (oh.consecutive_failures || 0),
        cites: [
          "healthy: " + oh.healthy,
          "latency_s: " + lat,
          "models_missing: " + ((oh.models_missing || []).join(", ") || "—"),
        ],
      });
    }

    // Circuit breakers — only those that aren't CLOSED
    const cb = envelopeData(data.circuit_breakers) || {};
    const breakers = cb.breakers || [];
    breakers
      .filter(b => {
        const st = String(b.state || "").toUpperCase();
        return st && st !== "CLOSED";
      })
      .forEach(b => {
        const ts = b.last_failure ? new Date(b.last_failure).getTime() : nowMs - 60_000;
        const st = String(b.state || "").toUpperCase();
        items.push({
          ts,
          age_s: Math.max(0, Math.floor((nowMs - ts) / 1000)),
          src: "CIRCUIT BREAKER",
          level: st === "OPEN" ? "down" : "warn",
          title: (b.name || b.id || "breaker") + " → " + st,
          body: "Consecutive failures " + (b.consecutive_failures || b.failure_count || 0) + ".",
          cites: [
            "name: " + (b.name || b.id || "—"),
            "state: " + b.state,
            "last_failure: " + (b.last_failure || "—"),
          ],
        });
      });

    // Most recent first
    items.sort((a, b) => (a.age_s || 0) - (b.age_s || 0));
    return items;
  }

  function ResearchFeedLive({ data }) {
    const [expanded, setExpanded] = useState(null);
    const items = useMemo(() => buildResearchFeed(data), [
      data.regime, data.live_trades, data.mcp, data.sentiment,
      data.ollama_health, data.circuit_breakers,
    ]);
    // Pick the most stale fetched_at across the 6 sources for the head ticker.
    const fetchedAts = [
      data.regime_fetched_at, data.live_trades_fetched_at, data.mcp_fetched_at,
      data.sentiment_fetched_at, data.ollama_health_fetched_at, data.circuit_breakers_fetched_at,
    ].filter(Boolean).map(t => new Date(t).getTime()).sort();
    const oldest = fetchedAts.length ? new Date(fetchedAts[0]).toISOString() : null;

    return h(Card, {
      num: "04", title: "Research stream · how the agent thinks",
      sub: "live · synthesises 6 endpoints · click to expand",
      right: cardRight(oldest,
        h("span", { className: "pill accent" }, h("span", { className: "dot accent pulse" }), " ", items.length, " EVENTS · 24h"))
    },
      h("div", { style: { display: "flex", flexDirection: "column", maxHeight: 420, overflowY: "auto" } },
        items.length === 0
          ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)", padding: "var(--s-3) 0" } }, "no recent activity")
          : items.map((r, i) => {
              const dot = r.level === "warn" ? "warn"
                        : r.level === "down" ? "down"
                        : r.level === "up" ? "up"
                        : "accent";
              const srcVar = "var(--" + dot + ")";
              // Stable identity prevents React from reusing DOM nodes when
              // a new event unshifts at the top — expanded state previously
              // pointed at index N which mapped to a different event after
              // the refresh tick.
              const stableKey = `${r.ts}:${r.src}:${(r.title || '').slice(0, 32)}`;
              const open = expanded === stableKey;
              return h("div", {
                key: stableKey,
                style: {
                  display: "grid", gridTemplateColumns: "60px 12px 1fr", gap: "var(--s-3)",
                  padding: "var(--s-3) 0", borderBottom: "1px solid var(--line-1)", cursor: "pointer",
                },
                onClick: () => setExpanded(open ? null : stableKey)
              },
                h("div", { className: "mono dim", style: { fontSize: "var(--t-xs)", paddingTop: 2 } },
                  h(TimeSince, { ts: r.ts })),
                h("div", { style: { position: "relative", paddingTop: 6 } },
                  h("span", { className: "dot " + dot, style: { position: "relative", zIndex: 1 } }),
                  h("div", { style: { position: "absolute", left: 2, top: 12, bottom: -16, width: 1, background: "var(--line-2)" } })
                ),
                h("div", null,
                  h("div", { style: { display: "flex", alignItems: "baseline", gap: "var(--s-2)" } },
                    h("span", { className: "tl-source", style: { color: srcVar } }, r.src),
                    h("span", { style: { color: "var(--fg-1)", fontSize: "var(--t-sm)", fontWeight: 500 } }, r.title),
                    h("span", { className: "tb-spacer", style: { flex: 1 } }),
                    h("span", { className: "dim mono", style: { fontSize: "var(--t-xs)" } }, open ? "−" : "+")
                  ),
                  h("div", { className: "dim", style: { fontSize: "var(--t-sm)", marginTop: 4, lineHeight: 1.55 } }, r.body),
                  open && h("div", {
                    style: {
                      marginTop: 8, background: "var(--bg-inset)", padding: 10, borderRadius: 4,
                      fontFamily: "var(--mono)", fontSize: "var(--t-xs)", animation: "mountIn 180ms var(--ease-out)",
                    }
                  },
                    h("div", { className: "dim", style: { marginBottom: 4, letterSpacing: ".08em", textTransform: "uppercase", fontSize: "var(--t-2xs)" } }, "CITATIONS · INPUTS"),
                    (r.cites || []).map((c, j) => h("div", { key: j, style: { padding: "2px 0", color: "var(--fg-2)" } }, "→ " + c))
                  )
                )
              );
            })
      )
    );
  }

  // ─────────────── ENTRY GATES — live from /api/ops/gates ───────────────
  function GateDot({ state, label, detail }) {
    // tiny inline dot used in EntryGatesLive's per-pair gate-strip. hover
    // title surfaces gate name + detail so operator gets per-gate context
    // without expanding the row.
    const color = state === true ? "var(--c-up)"
      : state === false ? "var(--c-down)"
      : "color-mix(in srgb, var(--fg-3) 60%, transparent)";
    return h("span", {
      title: label + " — " + (state === true ? "PASS" : state === false ? "BLOCK" : "n/a") + (detail ? " · " + detail : ""),
      style: { width: 9, height: 9, borderRadius: "50%", background: color, display: "inline-block", flexShrink: 0 },
    });
  }

  function EntryGatesLive({ data }) {
    const [expand, setExpand] = useState(null);
    const slot = slotState(data, "gates");
    const env = envelopeData(slot.env) || {};
    const crypto = env.crypto || [];
    const stocks = env.stocks || [];
    const all = crypto.concat(stocks).map(r => ({
      sym: r.pair,
      regime: r.regime,
      blocking: r.n_blocking || 0,
      first_blocker: r.first_blocker,
      gates: r.gates || [],
      snapshot: r.snapshot || {},
    }));
    const passing = all.filter(p => (p.blocking || 0) === 0).length;
    const blocked = all.length - passing;

    // Aggregate which gate is the most common blocker (operator wants
    // "what's keeping everything offline" at a glance).
    const blockerCounts = {};
    all.forEach(p => p.gates.filter(g => g.pass === false).forEach(g => {
      blockerCounts[g.gate] = (blockerCounts[g.gate] || 0) + 1;
    }));
    const topBlockers = Object.entries(blockerCounts).sort((a, b) => b[1] - a[1]).slice(0, 2);

    if (slot.phase !== "ok") {
      return h(Card, {
        num: "05", title: "Entry gates · why isn't anything trading?",
        sub: slot.phase === "loading" ? "loading…" : "endpoint unavailable",
        right: cardRight(slot.fetchedAt)
      },
        slot.phase === "loading"
          ? h(LoadingState)
          : h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }

    return h(Card, {
      num: "05", title: "Entry gates · why isn't anything trading?",
      sub: passing + "/" + all.length + " pair" + (all.length === 1 ? "" : "s") + " eligible",
      right: cardRight(slot.fetchedAt,
        h("span", { className: "pill " + (blocked > 0 ? "down" : "up"), style: { height: 18 } },
          h("span", { className: "dot " + (blocked > 0 ? "down pulse" : "up") }), " ",
          blocked > 0 ? (blocked + " BLOCKED") : "ALL CLEAR"))
    },
      // ── aggregate banner: tells operator "why is everything off" in one line ──
      blocked > 0 && topBlockers.length > 0 && h("div", {
        style: { fontSize: "var(--t-xs)", padding: "var(--s-2) var(--s-3)",
          marginBottom: "var(--s-2)", borderLeft: "2px solid var(--c-down)",
          background: "color-mix(in srgb, var(--c-down) 6%, transparent)" }
      },
        h("span", { style: { color: "var(--fg-1)" } }, blocked + " of " + all.length + " pairs blocked"),
        h("span", { className: "dim", style: { marginLeft: 8 } }, "most common: "),
        topBlockers.map(([g, n], i) => h("span", { key: g, className: "mono", style: { marginLeft: 6 } },
          (i > 0 ? "· " : "") + g + " (" + n + "×)"))
      ),

      // ── per-pair rows: pair · regime · gate-strip dots · n/M · first blocker · ▸ ──
      all.length === 0
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)", padding: "var(--s-3)" } },
            "no gate data — endpoint returned empty")
        : h("div", { style: { display: "flex", flexDirection: "column", gap: 0 } },
            all.map((p, i) => h(F, { key: p.sym }, [
              h("div", {
                key: "row",
                onClick: () => setExpand(expand === i ? null : i),
                style: { cursor: "pointer", display: "grid",
                  gridTemplateColumns: "minmax(80px,1fr) minmax(110px,1fr) minmax(120px,2fr) minmax(60px,80px) minmax(120px,1fr) 18px",
                  gap: "var(--s-2)", alignItems: "center",
                  padding: "var(--s-2) var(--s-2)",
                  borderBottom: "1px solid var(--line-1)",
                  fontSize: "var(--t-xs)" }
              },
                h("strong", { style: { color: "var(--fg-1)" } }, p.sym),
                h("span", { className: "pill " + (p.regime === "trending_up" ? "up" : p.regime === "trending_down" ? "down" : "info"),
                  style: { height: 18, justifySelf: "start" } }, p.regime || "—"),
                h("span", { style: { display: "inline-flex", gap: 4, alignItems: "center", flexWrap: "wrap" } },
                  p.gates.map((g, gi) => h(GateDot, { key: gi, state: g.pass, label: g.gate, detail: g.detail }))),
                h("span", { className: "mono dim", style: { fontSize: "var(--t-2xs)" } },
                  (p.gates.length - p.blocking) + "/" + p.gates.length + " pass"),
                h("span", { className: p.first_blocker ? "mono" : "dim", style: { fontSize: "var(--t-xs)", color: p.first_blocker ? "var(--c-down)" : undefined } },
                  p.first_blocker || "—"),
                h("span", { className: "dim mono", style: { fontSize: "var(--t-xs)" } }, expand === i ? "▾" : "▸")
              ),
              expand === i && h("div", {
                key: "exp",
                style: { background: "var(--bg-inset)", padding: "var(--s-3) var(--s-4)",
                  borderBottom: "1px solid var(--line-1)" }
              },
                h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--s-2) var(--s-4)" } },
                  p.gates.map((g, gi) => h("div", { key: gi,
                    style: { display: "flex", alignItems: "center", gap: 8, fontSize: "var(--t-xs)" } },
                    h(GateBadge, { state: g.pass === true ? "PASS" : g.pass === false ? "BLOCK" : "NA" }),
                    h("span", { style: { color: "var(--fg-1)", minWidth: 140 } }, g.gate),
                    h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)", flex: 1, textAlign: "right" } }, g.detail)
                  )))
              ),
            ].filter(Boolean)))
          )
    );
  }

  // ─────────────── PAIR TELEMETRY — sparklines live ───────────────
  function PairTelemetryLive({ data }) {
    const slot = slotState(data, "sparklines");
    const env = envelopeData(slot.env) || {};
    const pairs = env.pairs || {};
    const entries = Object.entries(pairs);

    if (slot.phase === "down") {
      return h(Card, {
        num: "06", title: "Pair telemetry · 5m closes · trailing 24h",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt)
      },
        h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }

    return h(Card, {
      num: "06", title: "Pair telemetry · 5m closes · trailing 24h",
      sub: entries.length + " pairs · auto-refresh 10s",
      right: cardRight(slot.fetchedAt)
    },
      slot.phase === "loading"
        ? h(LoadingState)
        : entries.length === 0
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no sparkline data")
        : h("div", { className: "grid g-4", style: { gap: "var(--s-3)" } },
            entries.map(([sym, p]) => {
              const data = p.closes || [];
              const pct = Number(p.pct_24h || 0);
              const px = Number(p.current || 0);
              const href = "/?pair=" + encodeURIComponent(sym) + "&venue=crypto";
              return h("a", {
                key: sym, href, className: "card interactive",
                style: { padding: "var(--s-3)", textDecoration: "none", color: "inherit" }
              },
                h("div", { style: { display: "flex", alignItems: "baseline", gap: 8 } },
                  h("strong", { className: "mono" }, sym),
                  h("span", { className: "pill " + (pct >= 0 ? "up" : "down"), style: { height: 16, fontSize: "var(--t-2xs)" } }, fmtPct(pct)),
                  h("span", { className: "tb-spacer", style: { flex: 1 } })
                ),
                h("div", { style: { marginTop: 6 } },
                  data.length ? h(Sparkline, { data, color: pct >= 0 ? "--up" : "--down", height: 32 })
                              : h("div", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, "no closes")),
                h("div", { style: { display: "flex", justifyContent: "space-between", marginTop: 4 } },
                  h("span", { className: "num", style: { fontSize: "var(--t-sm)" } },
                    px < 10 ? px.toFixed(4) : fmtUSD(px)),
                  h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, data.length + " bars"))
              );
            })
          )
    );
  }

  // ─────────────── STOCKS PAIR TELEMETRY — sparklines live ───────────────
  // Stocks-side parity to PairTelemetryLive. Reads from /api/ops/stocks_sparklines
  // (5Min × 78 bars ≈ one US trading session by default). NYSE-closed window
  // dims the card and swaps the sub-line to "last session close".
  function StocksPairTelemetryLive({ data }) {
    const slot = slotState(data, "stocks_sparklines");
    const env = envelopeData(slot.env) || {};
    const symbols = env.symbols || {};
    const basket = Array.isArray(env.basket) ? env.basket : Object.keys(symbols);
    const marketOpen = env.market_open;
    const tfLabel = env.timeframe || "5Min";

    if (slot.phase === "down") {
      return h(Card, {
        num: "23", title: "Stocks pair telemetry · " + tfLabel + " · session-to-date",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt),
      },
        h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }
    if (slot.phase === "loading") {
      return h(Card, {
        num: "23", title: "Stocks pair telemetry · " + tfLabel + " · session-to-date",
        sub: "loading…",
        right: cardRight(slot.fetchedAt),
      }, h(LoadingState));
    }

    const subLine = marketOpen
      ? basket.length + " symbols · NYSE open · auto-refresh 10s"
      : basket.length + " symbols · NYSE closed · last session close";

    const wrapperStyle = marketOpen
      ? null
      : { opacity: 0.78 };  // visually dim when market closed, per spec

    return h(Card, {
      num: "23", title: "Stocks pair telemetry · " + tfLabel + " · session window",
      sub: subLine,
      right: cardRight(slot.fetchedAt),
    },
      h("div", { style: wrapperStyle },
        basket.length === 0
          ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no stock symbols configured")
          : h("div", { className: "grid g-4", style: { gap: "var(--s-3)" } },
              basket.map(sym => {
                const p = symbols[sym] || {};
                const closes = p.closes || [];
                const pct = (p.pct_session == null) ? null : Number(p.pct_session);
                const px = (p.current == null) ? null : Number(p.current);
                const err = p.error;
                const cellStyle = { padding: "var(--s-3)", textDecoration: "none", color: "inherit" };
                const sparkColor = pct == null ? "--fg-3" : (pct >= 0 ? "--up" : "--down");
                const pctCls = pct == null ? "" : (pct >= 0 ? "up" : "down");

                return h("div", {
                  key: sym,
                  className: "card",
                  style: cellStyle,
                  "data-test": "stocks-spark-" + sym,
                },
                  h("div", { style: { display: "flex", alignItems: "baseline", gap: 8 } },
                    h("strong", { className: "mono" }, sym),
                    pct == null
                      ? h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, "—")
                      : h("span", { className: "pill " + pctCls, style: { height: 16, fontSize: "var(--t-2xs)" } }, fmtPct(pct)),
                    h("span", { className: "tb-spacer", style: { flex: 1 } })
                  ),
                  h("div", { style: { marginTop: 6 } },
                    closes.length >= 2
                      ? h(Sparkline, { data: closes, color: sparkColor, height: 32 })
                      : h("div", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } },
                          err ? err : "no closes")
                  ),
                  h("div", { style: { display: "flex", justifyContent: "space-between", marginTop: 4 } },
                    h("span", { className: "num", style: { fontSize: "var(--t-sm)" } },
                      px == null ? "—" : "$" + fmtUSD(px)),
                    h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } },
                      (p.bars_count != null ? p.bars_count : closes.length) + " bars")
                  )
                );
              })
            )
      )
    );
  }

  // ─────────────── SERVICES — 8-row health probe ───────────────
  function ServicesLive({ data }) {
    const slot = slotState(data, "services");
    const services = envelopeData(slot.env) || {};
    const rows = Object.entries(services);
    const totalUp = rows.filter(([, info]) => info && info.up).length;

    if (slot.phase !== "ok") {
      return h(Card, {
        num: "07a", title: "Service health · probes",
        sub: slot.phase === "loading" ? "loading…" : "endpoint unavailable",
        right: cardRight(slot.fetchedAt)
      },
        slot.phase === "loading"
          ? h(LoadingState)
          : h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }

    return h(Card, {
      num: "07a", title: "Service health · " + rows.length + " probes",
      sub: totalUp + "/" + rows.length + " up",
      right: cardRight(slot.fetchedAt)
    },
      h("div", { style: { display: "flex", flexDirection: "column" } },
        rows.length === 0
          ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no probes registered")
          : rows.map(([name, info]) => h(StatusRow, {
              key: name,
              status: info && info.up ? "up" : "down",
              name: name,
              sub: info ? ("via " + (info.via || "?") + (info.code != null ? " · " + info.code : "")) : "",
              value: h("span", null,
                info && info.age_s != null ? h("span", { className: "dim", style: { marginRight: 10 } }, Math.round(info.age_s) + "s") : null,
                info && info.endpoint ? h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, info.endpoint) : null
              )
            }))
      )
    );
  }

  // ─────────────── LLM PROVIDERS + CIRCUIT BREAKERS ───────────────
  function LLMHealthLive({ data }) {
    const ohSlot = slotState(data, "ollama_health");
    const cbSlot = slotState(data, "circuit_breakers");
    const statsSlot = slotState(data, "llm_stats");
    const oh = envelopeData(ohSlot.env) || {};
    const cb = envelopeData(cbSlot.env) || {};
    const stats = envelopeData(statsSlot.env) || {};
    const saved = (stats.shark && stats.shark.total_api_cost_saved_usd) || stats.total_api_cost_saved_usd || 0;
    const cryptoCalls = stats.crypto && stats.crypto.calls_24h;

    const ollamaModels = Array.isArray(oh.models_available)
      ? oh.models_available
      : (Array.isArray(oh.models) ? oh.models : Object.values(oh.models || {}));
    const ollamaLatencyMs = oh.last_probe_latency_s != null
      ? Math.round(oh.last_probe_latency_s * 1000)
      : (oh.latency_ms != null ? oh.latency_ms : null);
    const breakers = cb.breakers || [];
    const allDown = ohSlot.phase === "down" && cbSlot.phase === "down" && statsSlot.phase === "down";

    if (allDown) {
      return h(Card, {
        num: "07", title: "LLM providers · Ollama primary · Anthropic fallback",
        sub: "endpoint unavailable",
        right: cardRight(statsSlot.fetchedAt || ohSlot.fetchedAt)
      },
        h(EmptyState, { reason: statsSlot.reason || ohSlot.reason, fetchedAt: statsSlot.fetchedAt || ohSlot.fetchedAt, period: 10 })
      );
    }

    return h(Card, {
      num: "07", title: "LLM providers · Ollama primary · Anthropic fallback",
      sub: cryptoCalls != null ? (cryptoCalls + " crypto calls · 24h") : "cost saved vs all-Anthropic baseline (24h)",
      right: h("div", null,
        h(TimeSince, { ts: data.llm_stats_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        h("span", { className: "metric-label", style: { marginRight: 8 } }, "SAVED · 24h"),
        h("span", { className: "up num", style: { fontSize: "var(--t-lg)" } }, "$" + fmtUSD(saved, 2))
      )
    },
      h("div", { style: { display: "flex", flexDirection: "column" } },
        h(StatusRow, {
          status: oh.healthy ? "up" : "down",
          name: "Ollama (primary)",
          sub: oh.healthy
            ? (ollamaModels.length + " models" + (oh.status_age_seconds != null ? " · probed " + Math.round(oh.status_age_seconds) + "s ago" : ""))
            : (oh.error || "down"),
          value: h("span", null, h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, "lat ", ollamaLatencyMs != null ? ollamaLatencyMs + "ms" : "—"))
        }),
        breakers.length === 0
          ? h(StatusRow, { status: "up", name: "Anthropic (fallback)", sub: "no breakers tripped", value: h("span", { className: "dim mono" }, "armed") })
          : breakers.map(b => h(StatusRow, {
              key: b.name || b.id,
              status: b.state === "open" ? "down" : b.state === "half_open" ? "warn" : "up",
              name: b.name || b.id,
              sub: "state " + (b.state || "?") + " · failures " + (b.failure_count || 0),
              value: h("span", null,
                b.opened_at ? h("span", { className: "dim mono" }, "opened ", b.opened_at) : "—")
            }))
      )
    );
  }

  // ─────────────── POSITIONS — live trades + wheel ───────────────
  function PositionsLive({ data }) {
    const slot = slotState(data, "live_trades");
    const env = envelopeData(slot.env) || {};
    const trades = env.trades || [];

    if (slot.phase === "down") {
      return h(Card, {
        num: "08", title: "Open positions",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt)
      },
        h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }

    return h(Card, {
      num: "08", title: "Open positions", sub: "crypto + stocks · " + trades.length + " active",
      right: cardRight(slot.fetchedAt)
    },
      h("table", { className: "t" },
        h("thead", null, h("tr", null,
          h("th", null, "Symbol"), h("th", null, "Venue"), h("th", null, "Side"),
          h("th", { style: { textAlign: "right" } }, "Qty"),
          h("th", { style: { textAlign: "right" } }, "Entry"),
          h("th", { style: { textAlign: "right" } }, "Mark"),
          h("th", { style: { textAlign: "right" } }, "uPnL %"),
          h("th", null, "Note")
        )),
        h("tbody", null,
          trades.length === 0
            ? h("tr", null, h("td", { colSpan: 8, className: "dim", style: { fontSize: "var(--t-xs)", padding: "var(--s-3)" } }, "no open positions"))
            : trades.map((t, i) => h("tr", { key: i },
                h("td", null, h("strong", { className: "mono" }, t.label)),
                h("td", { className: "dim" }, t.kind === "crypto" ? "Coinbase" : t.kind === "wheel" ? "Alpaca" : t.kind),
                h("td", { className: "mono " + ((t.subkind || "").includes("short") ? "down" : "up") }, (t.subkind || "—").toUpperCase()),
                h("td", { className: "num", style: { textAlign: "right" } }, t.qty != null ? t.qty : "—"),
                h("td", { className: "num", style: { textAlign: "right" } }, t.entry != null ? fmtUSD(t.entry, t.entry < 10 ? 4 : 2) : "—"),
                h("td", { className: "num", style: { textAlign: "right" } }, t.current != null ? fmtUSD(t.current, t.current < 10 ? 4 : 2) : "—"),
                h("td", { className: "num " + ((t.pnl_pct || 0) >= 0 ? "up" : "down"), style: { textAlign: "right" } },
                  t.pnl_pct != null ? fmtPct(t.pnl_pct) : "—"),
                h("td", { className: "dim", style: { fontSize: "var(--t-xs)" } }, t.extra || "")
              ))
        )
      )
    );
  }

  // ─────────────── STOCKS ML — Shark TFT status (live training banner) ───────────────
  function StocksMLLive({ data }) {
    const slot = slotState(data, "stocks_ml");
    const env = envelopeData(slot.env) || {};
    const live = env.training_state === "running";
    const cur = env.current_epoch;
    const tot = env.epochs_target;
    const progress = (cur && tot) ? (cur / tot) * 100 : 0;

    if (slot.phase === "down") {
      return h(Card, {
        num: "09", title: "Stocks · Shark TFT",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt)
      },
        h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }

    return h(Card, {
      num: "09", title: "Stocks · Shark TFT",
      sub: env.weights_present ? "weights present" : "no model yet (Sun 11 PM ET)",
      right: cardRight(slot.fetchedAt,
        live
          ? h("span", { className: "pill accent" }, h("span", { className: "dot accent pulse" }), " LIVE TRAINING")
          : env.ml_enabled
            ? h("span", { className: "pill up" }, "ML ENABLED")
            : h("span", { className: "pill" }, "ML ALPHA"))
    },
      live && h("div", null,
        h("div", { className: "metric-label" }, "EPOCH " + cur + " / " + tot + " · loss " + (env.current_loss || "—") + " · val_acc " + (env.current_val_acc || "—")),
        h(ProgressBar, { value: progress, max: 100, cls: "accent" }),
        h("div", { className: "hr" })
      ),
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "BEST VAL_ACC"),
        h("div", { className: "num" }, env.best_val_acc != null ? env.best_val_acc.toFixed(3) : "—"),
        h("div", { className: "dim mono" }, "BEST EPOCH"),
        h("div", { className: "num" }, env.best_epoch != null ? env.best_epoch : "—"),
        h("div", { className: "dim mono" }, "N TRAIN"),
        h("div", { className: "num" }, env.n_train != null ? env.n_train : "—"),
        h("div", { className: "dim mono" }, "N TICKERS"),
        h("div", { className: "num" }, env.n_tickers != null ? env.n_tickers : "—"),
        h("div", { className: "dim mono" }, "DEVICE"),
        h("div", { className: "num" }, env.device || "—"),
        h("div", { className: "dim mono" }, "AGE"),
        h("div", { className: "num" }, env.weights_age_seconds != null ? Math.floor(env.weights_age_seconds / 3600) + "h" : "—"),
        h("div", { className: "dim mono" }, "NEXT CRON"),
        h("div", { className: "num" }, env.next_train_cron || "—")
      ),
      env.log_tail && env.log_tail.length > 0 && h("div", null,
        h("div", { className: "hr" }),
        h("div", { className: "metric-label" }, "TRAIN LOG · LAST LINES"),
        h("pre", { style: { background: "var(--bg-inset)", padding: 8, marginTop: 6, fontFamily: "var(--mono)", fontSize: "var(--t-2xs)", color: "var(--fg-2)", maxHeight: 100, overflow: "auto" } },
          env.log_tail.slice(-6).join("\n"))
      )
    );
  }

  // /api/ops/market_hours — NYSE session state. Cache 60s; the response only
  // changes at 09:30 / 16:00 ET so polling more often is wasted work.
  function useMarketHours() {
    const [mh, setMh] = useState(null);
    useEffect(() => {
      let cancelled = false;
      const fetchNow = () => safeJsonFetch("/api/ops/market_hours")
        .then(j => { if (!cancelled) setMh(envelopeData(j) || null); })
        .catch(() => { /* leave null — pill renders "—" placeholder */ });
      fetchNow();
      const iv = setInterval(fetchNow, 60_000);
      return () => { cancelled = true; clearInterval(iv); };
    }, []);
    return mh;
  }

  // ─────────────── STOCKS — wheel + shark Alpaca state ───────────────
  function StocksLive({ data }) {
    const slot = slotState(data, "stocks");
    const env = envelopeData(slot.env) || {};
    const alpaca = env.alpaca || {};
    const wheel = env.wheel || {};
    const shark = env.shark || {};
    const mh = useMarketHours();

    // Market hours pill — formats NYSE session state next to the card title.
    // Shows OPEN/CLOSED/EXT with a title attribute carrying the next
    // open/close time so hovering surfaces the schedule without a banner.
    let marketPill = null;
    if (mh) {
      const isOpen = !!mh.is_open;
      const isExt = !!mh.is_extended;
      const label = isOpen ? "OPEN" : isExt ? "EXT" : "CLOSED";
      const cls = isOpen ? "up" : isExt ? "warn" : "down";
      const fmtEt = (iso) => {
        if (!iso) return "—";
        try { return new Date(iso).toLocaleString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit", month: "short", day: "numeric" }); }
        catch (_) { return iso; }
      };
      const titleText = isOpen
        ? "NYSE open · closes " + fmtEt(mh.next_close_utc) + " ET"
        : "NYSE closed · opens " + fmtEt(mh.next_open_utc) + " ET";
      marketPill = h("span", { className: "pill " + cls, title: titleText, style: { height: 18 } },
        h("span", { className: "dot " + cls }), " NYSE ", label);
    } else {
      marketPill = h("span", { className: "pill", title: "loading market hours" }, "NYSE —");
    }

    if (slot.phase === "down") {
      return h(Card, {
        num: "10", title: "Stocks · Wheel + Shark",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt, marketPill)
      },
        h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }

    return h(Card, {
      num: "10", title: "Stocks · Wheel + Shark",
      sub: alpaca.paper ? "Alpaca · paper" : "Alpaca · live",
      right: cardRight(slot.fetchedAt, marketPill)
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "PORTFOLIO"),
        h("div", { className: "num" }, "$" + fmtUSD(alpaca.portfolio_value || 0)),
        h("div", { className: "dim mono" }, "CASH"),
        h("div", { className: "num" }, "$" + fmtUSD(alpaca.cash || 0)),
        h("div", { className: "dim mono" }, "BP"),
        h("div", { className: "num" }, "$" + fmtUSD(alpaca.buying_power || 0)),
        h("div", { className: "dim mono" }, "AGE"),
        h("div", { className: "num" }, alpaca.age_seconds != null ? Math.floor(alpaca.age_seconds / 60) + "m" : "—")
      ),
      h("div", { className: "hr" }),
      (() => {
        const positions = wheel.open_positions || [];
        const totalPremium = positions.reduce((s, p) => s + Number(p.entry_credit || 0) * Number(p.qty || 1), 0);
        const totalCollateral = positions
          .filter(p => p.kind === "short_put")
          .reduce((s, p) => s + Number(p.strike || 0) * Number(p.qty || 1) * 100, 0);
        return h(F, null,
          h("div", { style: { display: "flex", alignItems: "baseline", gap: "var(--s-3)" } },
            h("div", { className: "metric-label" }, "WHEEL · " + positions.length + " open"),
            h("span", { className: "tb-spacer", style: { flex: 1 } }),
            h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } },
              "premium $", fmtUSD(totalPremium), " · collateral $", fmtUSD(totalCollateral))
          ),
          positions.length > 0 && h("table", { className: "t", style: { marginTop: "var(--s-2)", fontSize: "var(--t-xs)" } },
            h("thead", null, h("tr", null,
              h("th", null, "Sym"),
              h("th", null, "Type"),
              h("th", null, "Qty"),
              h("th", { style: { textAlign: "right" } }, "Strike"),
              h("th", null, "Expiry"),
              h("th", { style: { textAlign: "right" } }, "Premium")
            )),
            h("tbody", null, positions.map((p, i) => h("tr", { key: i },
              h("td", null, h("strong", null, p.underlying)),
              h("td", null, h("span", { className: "pill " + (p.kind === "short_put" ? "warn" : p.kind === "short_call" ? "warn" : "up"), style: { height: 16, fontSize: "var(--t-2xs)" } },
                p.kind === "short_put" ? "SHORT PUT" : p.kind === "short_call" ? "SHORT CALL" : p.kind === "long_shares" ? "LONG" : (p.kind || "—"))),
              h("td", null, p.qty),
              h("td", { className: "mono", style: { textAlign: "right" } }, "$" + Number(p.strike || 0).toFixed(2)),
              h("td", { className: "mono dim", style: { fontSize: "var(--t-2xs)" } }, (p.expiry || "—").slice(0, 10)),
              h("td", { className: "num up", style: { textAlign: "right" } },
                "$" + fmtUSD(Number(p.entry_credit || 0) * Number(p.qty || 1)))
            )))
          ),
          h("div", { style: { fontSize: "var(--t-xs)", marginTop: 4 } },
            "cumulative P&L: ", h("span", { className: "num " + ((wheel.cumulative_pnl_usd || 0) >= 0 ? "up" : "down") },
              "$", fmtUSD(wheel.cumulative_pnl_usd || 0))
          )
        );
      })(),
      h("div", { className: "hr" }),
      h("div", { className: "metric-label" }, "SHARK"),
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 6, fontSize: "var(--t-xs)", marginTop: 4 } },
        h("div", { className: "dim mono" }, "MODE"),       h("div", { className: "num" }, shark.mode || "—"),
        h("div", { className: "dim mono" }, "TRADES"),     h("div", { className: "num" }, (shark.stats && shark.stats.total_trades) || 0),
        h("div", { className: "dim mono" }, "WIN RATE"),   h("div", { className: "num" }, shark.stats ? ((shark.stats.win_rate || 0) * 100).toFixed(0) + "%" : "—"),
        h("div", { className: "dim mono" }, "BREAKER"),    h("div", { className: "num " + (shark.circuit_breaker ? "down" : "up") }, shark.circuit_breaker ? "TRIPPED" : "armed")
      )
    );
  }

  // ─────────────── MCP — wire status ───────────────
  function MCPCardLive({ data }) {
    const slot = slotState(data, "mcp");
    const env = envelopeData(slot.env) || {};
    const probe = env.probe || {};
    const reachable = !!probe.ok_for_streamable_http;
    const lastCall = env.last_call || {};

    if (slot.phase === "down") {
      return h(Card, {
        num: "11", title: "MCP · wire status",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt)
      },
        h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }

    return h(Card, {
      num: "11", title: "MCP · wire status",
      sub: reachable ? "Hermes MCP reachable" : "MCP unreachable",
      right: cardRight(slot.fetchedAt,
        h("span", { className: "pill " + (reachable ? "up" : "down") }, h("span", { className: "dot " + (reachable ? "up" : "down") + " pulse" }), " ", reachable ? "OK" : "DOWN"))
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 2fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "URL"),
        h("div", { className: "num mono", style: { fontSize: "var(--t-2xs)", wordBreak: "break-all" } }, env.endpoint || "—"),
        h("div", { className: "dim mono" }, "TRANSPORT"),
        h("div", { className: "num mono", style: { fontSize: "var(--t-2xs)" } }, env.transport || "—"),
        h("div", { className: "dim mono" }, "PROBE"),
        h("div", { className: "num" },
          (probe.via || "—") + (probe.age_s != null ? " · " + Math.round(probe.age_s) + "s" : "")),
        h("div", { className: "dim mono" }, "TOOLS"),
        h("div", { className: "num" }, env.tools_count != null ? env.tools_count : "—"),
        h("div", { className: "dim mono" }, "LAST CALL"),
        h("div", { className: "num mono", style: { fontSize: "var(--t-2xs)" } },
          lastCall.tool ? (lastCall.tool + (lastCall.ts ? " · " + lastCall.ts.replace("T", " ").slice(0, 19) : "")) : "—")
      )
    );
  }

  // ─────────────── QUICK ACTIONS — fully wired ───────────────
  // Each button shows a status indicator (success/error/info) below the button row.
  function QuickActions({ setKillState, killState }) {
    const [status, setStatus] = useState({ msg: "", level: "info", ts: 0 });
    const toast = (msg, level) => setStatus({ msg, level: level || "info", ts: Date.now() });

    const postJSON = (url, body) => fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });

    const doPause = () => postJSON("/api/ops/pause", { reason: "operator manual pause via spa" })
      .then(r => r.ok ? toast("PAUSED · dry_run=true", "ok") : toast("PAUSE failed · HTTP " + r.status, "warn"))
      .catch(e => toast("PAUSE error · " + e.message, "warn"));

    // RESUME re-enables order placement; it's irreversible-on-fill, so we
    // require an explicit operator confirmation. Pause is one-click by design
    // (always safe to pause), but resume is two-step.
    const doResume = () => {
      if (!window.confirm("Resume trading? This re-enables order placement on the live freqtrade instance.")) {
        toast("RESUME cancelled", "info");
        return;
      }
      return postJSON("/api/ops/resume", { reason: "operator manual resume via spa", confirm: true })
        .then(r => r.ok ? toast("RESUMED · dry_run=false", "ok") : r.json().then(j => toast("RESUME refused · " + (j.detail || ("HTTP " + r.status)), "warn")))
        .catch(e => toast("RESUME error · " + e.message, "warn"));
    };

    const doEvolve = () => postJSON("/api/ops/mcp/trigger_evolution_cycle", {})
      .then(r => r.ok ? toast("Evolution cycle kicked off · check EPT card", "ok") : toast("evolution trigger failed · HTTP " + r.status, "warn"))
      .catch(e => toast("evolution trigger error · " + e.message, "warn"));

    const doRebalance = () => fetch("/api/ops/rebalance", { method: "GET" })
      .then(r => r.json())
      .then(env => {
        const d = (env && env.data) || {};
        const n = d.n_changes || 0;
        if (n === 0) { toast("REBALANCE · no changes (sharpe-gated)", "info"); return; }
        const summary = (d.changes || []).map(c => c.pair + " " + (c.from * 100).toFixed(1) + "%→" + (c.to * 100).toFixed(1) + "%").join(", ");
        if (!confirm("Apply rebalance? " + n + " changes: " + summary)) { toast("rebalance cancelled", "info"); return; }
        return postJSON("/api/ops/rebalance", { confirm: true })
          .then(r => r.ok ? toast("REBALANCE applied · " + n + " weights updated", "ok") : toast("rebalance apply failed · HTTP " + r.status, "warn"));
      })
      .catch(e => toast("rebalance error · " + e.message, "warn"));

    const doSlackBrief = () => toast("Daily Slack brief fires automatically at 00:00 UTC · Hermes cron", "info");

    const dotCls = status.level === "ok" ? "up" : status.level === "warn" ? "down" : "info";

    return h(Card, {
      num: "12", title: "Quick actions · control panel",
      sub: "atomic config writes · snapshots auto-saved"
    },
      h("div", { className: "grid g-2", style: { gap: "var(--s-3)" } },
        h("button", { className: "btn", onClick: doPause, "aria-label": "Pause trading" }, "PAUSE TRADING"),
        h("button", { className: "btn", onClick: doResume, "aria-label": "Resume trading" }, "RESUME"),
        h("button", { className: "btn warn", onClick: doEvolve, "aria-label": "Trigger evolution cycle" }, "TRIGGER EVOLUTION"),
        h("button", { className: "btn", onClick: doRebalance, "aria-label": "Rebalance portfolio weights" }, "REBALANCE WEIGHTS"),
        h("button", { className: "btn", onClick: doSlackBrief, "aria-label": "Generate daily Slack brief" }, "DAILY SLACK BRIEF")
      ),
      status.msg && h("div", {
        style: {
          marginTop: "var(--s-3)", padding: "6px 10px", background: "var(--bg-inset)",
          borderRadius: 4, display: "flex", alignItems: "center", gap: 8,
          fontSize: "var(--t-xs)", fontFamily: "var(--mono)",
        }
      },
        h("span", { className: "dot " + dotCls }),
        h("span", { className: dotCls }, status.msg)
      ),
      h("div", { className: "hr" }),
      h("div", { style: { display: "flex", alignItems: "center", gap: "var(--s-3)" } },
        h("span", { className: "metric-label" }, "DESTRUCTIVE"),
        h(KillSwitch, {
          state: killState,
          onArm: () => setKillState("armed"),
          // The wrapper around setKillState (OpsApp.setKillState) already
          // POSTs /api/ops/pause when next === "killed" and /api/ops/resume
          // when next === "normal" after "killed". This handler used to
          // *also* POST /api/ops/pause directly, producing two simultaneous
          // pause requests per kill press. Toasts now mirror the wrapper's
          // single call: read window state via setTimeout(0) to give the
          // wrapper time to fire, then surface result.
          onKill: () => {
            setKillState("killed");
            toast("KILL · trading halt requested (dry_run=true)", "ok");
          },
          onResume: () => {
            setKillState("normal");
            toast("RESUMED · trading resume requested", "ok");
          }
        }),
        h("span", { className: "dim", style: { fontSize: "var(--t-xs)", flex: 1, textAlign: "right" } },
          "ARM, then hold 1.5s to flatten all positions, cancel orders, halt strategy.")
      )
    );
  }

  // ─────────────── TRAINING — FreqAI / TFT retrain status (data-num 17) ───────────────
  function TrainingCardLive({ data }) {
    const env = envelopeData(data.training) || {};
    const tft = env.tft || {};
    const ept = env.ept || {};
    const pairs = tft.pairs || [];
    const cur = pairs.find(p => p.status === "training");
    const done = pairs.filter(p => p.status === "done");
    const etaMin = tft.current_pair_eta_s != null ? Math.round(tft.current_pair_eta_s / 60) : null;
    return h(Card, {
      num: "17", title: "Training · FreqAI / TFT retrain status",
      sub: cur ? ("training " + cur.pair + " · epoch " + cur.last_epoch + "/" + cur.max_epoch) : (done.length + " pairs trained"),
      right: h(F, null,
        h(TimeSince, { ts: data.training_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        cur
          ? h("span", { className: "pill accent" }, h("span", { className: "dot accent pulse" }), " LIVE")
          : h("span", { className: "pill up" }, "IDLE")
      )
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "CURRENT PAIR"),
        h("div", { className: "num accent" }, (cur && cur.pair) || "—"),
        h("div", { className: "dim mono" }, "EPOCH"),
        h("div", { className: "num" }, cur ? (cur.last_epoch + " / " + cur.max_epoch) : "—"),
        h("div", { className: "dim mono" }, "VAL SHARPE"),
        h("div", { className: "num " + ((cur && cur.val_sharpe >= 0) ? "up" : "down") }, cur && cur.val_sharpe != null ? Number(cur.val_sharpe).toFixed(3) : "—"),
        h("div", { className: "dim mono" }, "LOSS"),
        h("div", { className: "num" }, cur && cur.loss != null ? Number(cur.loss).toFixed(4) : "—"),
        h("div", { className: "dim mono" }, "AVG EPOCH"),
        h("div", { className: "num" }, tft.avg_epoch_seconds != null ? tft.avg_epoch_seconds + "s" : "—"),
        h("div", { className: "dim mono" }, "ETA"),
        h("div", { className: "num" }, etaMin != null ? etaMin + "m" : "—"),
        h("div", { className: "dim mono" }, "DICT READY"),
        h("div", { className: "num " + (tft.pair_dict_ready ? "up" : "warn") }, tft.pair_dict_ready ? "yes" : "no"),
        h("div", { className: "dim mono" }, "EPT GEN"),
        h("div", { className: "num" }, ept.generation != null ? ("gen " + ept.generation + " · " + (ept.champion_id || "—")) : "—")
      ),
      pairs.length > 0 && h("div", null,
        h("div", { className: "hr" }),
        h("div", { className: "metric-label" }, "PER-PAIR SUB-TRAIN · " + pairs.length + " pairs"),
        h("div", { style: { marginTop: 6 } },
          pairs.map((p, i) => h("div", {
            key: i,
            style: { display: "grid", gridTemplateColumns: "50px 80px 60px 60px 1fr", gap: 6, fontSize: "var(--t-2xs)", padding: "2px 0" }
          },
            h("span", { className: "mono" }, p.pair),
            h("span", { className: "pill " + (p.status === "done" ? "up" : p.status === "training" ? "accent" : "info"), style: { height: 16 } }, p.status),
            h("span", { className: "num" }, "ep " + (p.last_epoch != null ? p.last_epoch : "—")),
            h("span", { className: "num " + ((p.val_sharpe || 0) >= 0 ? "up" : "down") }, p.val_sharpe != null ? Number(p.val_sharpe).toFixed(2) : "—"),
            h("span", { className: "dim mono" }, p.early_stopped ? "early-stop" : (p.end_ts || p.start_ts || ""))
          ))
        )
      )
    );
  }

  // ─────────────── READINESS — validation gate matrix (data-num 18) ───────────────
  function ReadinessCardLive({ data }) {
    const env = envelopeData(data.readiness) || {};
    const checks = env.checks || [];
    const allPass = env.ready === true;
    const labelOf = (name) => ({
      sharpe: "Sharpe", max_drawdown: "MaxDD", profit_factor: "PF",
      win_rate: "Win rate", total_trades: "Trades",
    }[name] || name);
    const fmtVal = (name, v) => {
      if (v == null) return "—";
      if (name === "max_drawdown" || name === "win_rate") return (v * 100).toFixed(1) + "%";
      if (name === "total_trades") return String(Math.round(v));
      return Number(v).toFixed(2);
    };
    const fmtTh = (name, v, op) => {
      if (v == null) return "—";
      if (name === "max_drawdown" || name === "win_rate") return op + " " + (v * 100).toFixed(0) + "%";
      return op + " " + Number(v).toFixed(2);
    };
    return h(Card, {
      num: "18", title: "Readiness · validation gate matrix",
      sub: env.mode ? ("mode " + env.mode + " · " + env.n_trades + " trades") : "—",
      right: h(F, null,
        h(TimeSince, { ts: data.readiness_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        h("span", { className: "pill " + (allPass ? "up" : "warn") },
          h("span", { className: "dot " + (allPass ? "up" : "warn") + " pulse" }),
          " ", allPass ? "READY" : "NOT READY")
      )
    },
      checks.length === 0
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no readiness data")
        : h("table", { className: "t" },
            h("thead", null, h("tr", null,
              h("th", null, "Gate"),
              h("th", { style: { textAlign: "right" } }, "Current"),
              h("th", { style: { textAlign: "right" } }, "Threshold"),
              h("th", null, "Status")
            )),
            h("tbody", null, checks.map((c, i) => h("tr", { key: i },
              h("td", null, labelOf(c.name)),
              h("td", { className: "num " + (c.passed ? "up" : "down"), style: { textAlign: "right" } }, fmtVal(c.name, c.value)),
              h("td", { className: "dim mono", style: { textAlign: "right" } }, fmtTh(c.name, c.threshold, c.op)),
              h("td", null, h(GateBadge, { state: c.passed ? "PASS" : "BLOCK" }))
            )))
          ),
      env.diagnostics && h("div", null,
        h("div", { className: "hr" }),
        h("div", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } },
          "buckets " + (env.diagnostics.daily_buckets || 0) +
          " · starting equity proxy $" + fmtUSD(env.diagnostics.starting_equity_proxy || 0, 2))
      )
    );
  }

  // ─────────────── REGIME CONFIG EDITOR (data-num 19) ───────────────
  // Operator education block, ported verbatim from legacy ops.html:1204-1278
  // with italic tags (<i>, <em>) and decorative emojis stripped per the
  // operator design spec. Regime-name spans flattened to <code> since the
  // legacy regime-tag-* CSS classes don't live in quanta.css.
  const REGIME_PARAMS_GUIDE_HTML = (
    '<h4 style="font-family:var(--mono);font-size:var(--t-2xs);font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--fg-3);margin:8px 0 6px;">The 5 market regimes</h4>' +
    '<p style="margin:0 0 10px;">A 4-state HMM classifies each candle into one of these regimes per pair. The strategy adapts entries, exits, sizing, and trailing-stop behaviour to the active regime.</p>' +
    '<dl style="display:grid;grid-template-columns:160px 1fr;gap:4px 14px;margin:0 0 6px;">' +
      '<dt><code>trending_up</code></dt>' +
      '<dd>Sustained uptrend. <strong>Strategy:</strong> loosen entries, hold longer, activate trailing stop on winners.</dd>' +
      '<dt><code>trending_down</code></dt>' +
      '<dd>Sustained downtrend. <strong>Strategy:</strong> longs are <strong>hard-blocked</strong> — bot waits for regime change. The <code>entry_delta</code> here is belt-and-suspenders.</dd>' +
      '<dt><code>mean_reverting</code></dt>' +
      '<dd>Range-bound, oscillating market. <strong>Strategy:</strong> quick scalps with tight take-profit (<code>mean_rev_take_profit</code>).</dd>' +
      '<dt><code>high_volatility</code></dt>' +
      '<dd>Whippy, hard-to-predict. <strong>Strategy:</strong> shrink position size (<code>high_vol_stake_factor</code>) and require higher conviction (<code>high_vol_min_confidence</code>).</dd>' +
      '<dt><code>unknown</code></dt>' +
      '<dd>HMM uncertain. <strong>Strategy:</strong> conservative defaults — neither blocked nor preferred.</dd>' +
    '</dl>' +
    '<h4 style="font-family:var(--mono);font-size:var(--t-2xs);font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--fg-3);margin:14px 0 6px;">Entry &amp; exit deltas</h4>' +
    '<p style="margin:0 0 10px;">These add a per-regime offset to the base thresholds. Base entry = <code>0.62</code> (TFT up-probability needed to fire a long); base exit = <code>0.55</code> (down-probability needed to close).</p>' +
    '<dl style="display:grid;grid-template-columns:160px 1fr;gap:4px 14px;margin:0 0 6px;">' +
      '<dt><code>entry_delta = +0.15</code></dt>' +
      '<dd>Require <code>up_prob ≥ 0.62 + 0.15 = 0.77</code>. <strong>Harder to enter</strong> in this regime.</dd>' +
      '<dt><code>entry_delta = −0.05</code></dt>' +
      '<dd>Require <code>up_prob ≥ 0.62 − 0.05 = 0.57</code>. <strong>Easier to enter</strong>.</dd>' +
      '<dt><code>entry_delta = blank</code></dt>' +
      '<dd><strong>Hard-block</strong> — no longs allowed in this regime. Same as setting threshold to ∞.</dd>' +
      '<dt><code>exit_delta = −0.20</code></dt>' +
      '<dd>Require <code>down_prob ≥ 0.55 − 0.20 = 0.35</code>. <strong>Faster exits</strong> — close on weaker signals.</dd>' +
      '<dt><code>exit_delta = +0.05</code></dt>' +
      '<dd>Require <code>down_prob ≥ 0.60</code>. <strong>Hold longer</strong>, only exit on strong reversal.</dd>' +
    '</dl>' +
    '<h4 style="font-family:var(--mono);font-size:var(--t-2xs);font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--fg-3);margin:14px 0 6px;">Scalar parameters</h4>' +
    '<dl style="display:grid;grid-template-columns:160px 1fr;gap:4px 14px;margin:0 0 6px;">' +
      '<dt><code>high_vol_stake_factor</code></dt>' +
      '<dd>In <code>high_volatility</code>, multiply position size by this. Default <code>0.7</code> (30% smaller). Set <code>0.5</code> for half-size, <code>0</code> to skip entries entirely. Lower if drawdowns spike in volatile markets.</dd>' +
      '<dt><code>high_vol_min_confidence</code></dt>' +
      '<dd>In <code>high_volatility</code>, require <code>up_prob ≥ this</code> on top of the regular threshold. Default <code>0.65</code>. Higher = fewer but higher-conviction trades.</dd>' +
      '<dt><code>mean_rev_take_profit</code></dt>' +
      '<dd>In <code>mean_reverting</code>, exit immediately when profit reaches this fraction. Default <code>0.012</code> = +1.2%. Lower = quicker scalps; higher = let winners run further.</dd>' +
      '<dt><code>trending_up_trail_trigger</code></dt>' +
      '<dd>In <code>trending_up</code>, when profit exceeds this, activate trailing stop. Default <code>0.025</code> = 2.5%. Lower = trail sooner (lock in smaller wins); higher = wait for bigger wins before trailing.</dd>' +
      '<dt><code>trending_up_trail_distance</code></dt>' +
      '<dd>Once trailing is active, trail this far below the high-water mark (must be negative). Default <code>−0.02</code> = 2% below peak. More negative (e.g. <code>−0.03</code>) = wider trail, more room for noise; closer to <code>0</code> = tighter trail, gives back less but stops out sooner.</dd>' +
      '<dt><code>tft_min_confidence</code></dt>' +
      '<dd>TFT model\'s quantile-spread confidence floor. Default <code>0.35</code>. Below this, no entries fire in any regime. Raise to <code>0.45</code>+ to filter out low-conviction signals at the cost of fewer trades.</dd>' +
      '<dt><code>meta_min_confidence</code></dt>' +
      '<dd>When the DRL meta-agent (PPO + A2C + DQN ensemble) is active, require this confidence on the <code>meta_signal</code>. Same logic for entries (<code>signal=+1</code>) and exits (<code>signal=−1</code>). Default <code>0.35</code>.</dd>' +
    '</dl>' +
    '<h4 style="font-family:var(--mono);font-size:var(--t-2xs);font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--fg-3);margin:14px 0 6px;">Recommended tuning order</h4>' +
    '<ol style="margin:0 0 10px;padding-left:20px;">' +
      '<li><strong>Start with defaults</strong> — they\'re calibrated to work end-to-end.</li>' +
      '<li><strong>Too many losing entries:</strong> raise <code>tft_min_confidence</code> (+0.05 increments) or <code>meta_min_confidence</code>.</li>' +
      '<li><strong>No trades firing:</strong> lower <code>tft_min_confidence</code>, then check that no allowed regime has <code>entry_delta = blank</code>.</li>' +
      '<li><strong>Drawdowns in volatile markets:</strong> drop <code>high_vol_stake_factor</code> to <code>0.4</code>, raise <code>high_vol_min_confidence</code> to <code>0.8</code>.</li>' +
      '<li><strong>Profits get given back in trends:</strong> tighten <code>trending_up_trail_distance</code> closer to <code>0</code> (e.g. <code>−0.015</code>).</li>' +
      '<li><strong>Whipsawing in chop:</strong> raise <code>mean_rev_take_profit</code> to <code>0.018</code>+ to ignore tiny moves.</li>' +
    '</ol>' +
    '<div style="margin-top:10px;padding:8px 12px;background:var(--warn-bg);border-left:3px solid var(--warn);border-radius:4px;color:var(--fg-1);">' +
      '<strong>Apply changes</strong> writes <code>config.json</code> atomically (with timestamped backup) and triggers a freqtrade reload — the bot keeps running, but new candles will use the updated values. Open trades are not affected mid-flight; only future entries/exits use the new parameters.' +
    '</div>'
  );

  function RegimeConfigEditor({ data }) {
    const env = envelopeData(data.regime_config) || {};
    const cfg = env.regime_gating || {};
    const schema = env.schema || {};
    const regimes = (schema.regimes || []).filter(r => r !== "unknown");
    const scalars = [
      "high_vol_stake_factor", "high_vol_min_confidence", "mean_rev_take_profit",
      "trending_up_trail_trigger", "trending_up_trail_distance",
      "tft_min_confidence", "meta_min_confidence",
    ];
    const [form, setForm] = useState(null);
    const [toastMsg, setToastMsg] = useState({ msg: "", level: "info" });

    // Initialize form on first load when cfg appears
    useEffect(() => {
      if (cfg && Object.keys(cfg).length && form == null) {
        setForm(JSON.parse(JSON.stringify(cfg)));
      }
    }, [env.config_path]);

    if (form == null) {
      return h(Card, { num: "19", title: "Regime config editor", sub: "loading…" },
        h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "waiting for /api/ops/regime_config…"));
    }

    const setDelta = (group, regime, v) => {
      const f = JSON.parse(JSON.stringify(form));
      f[group][regime] = v;
      setForm(f);
    };
    const setScalar = (k, v) => {
      const f = JSON.parse(JSON.stringify(form));
      f[k] = v;
      setForm(f);
    };
    const reset = () => setForm(JSON.parse(JSON.stringify(cfg)));
    const submit = () => {
      // Compute diff
      const diff = [];
      regimes.forEach(r => {
        const oldE = (cfg.entry_delta || {})[r];
        const newE = form.entry_delta[r];
        if (Number(oldE) !== Number(newE)) diff.push("entry_delta[" + r + "] " + oldE + " → " + newE);
        const oldX = (cfg.exit_delta || {})[r];
        const newX = form.exit_delta[r];
        if (Number(oldX) !== Number(newX)) diff.push("exit_delta[" + r + "] " + oldX + " → " + newX);
      });
      scalars.forEach(k => {
        if (Number(cfg[k]) !== Number(form[k])) diff.push(k + " " + cfg[k] + " → " + form[k]);
      });
      if (diff.length === 0) { setToastMsg({ msg: "no changes to write", level: "info" }); return; }
      if (!confirm("Apply " + diff.length + " change(s)?\n\n" + diff.join("\n"))) {
        setToastMsg({ msg: "submission cancelled", level: "info" });
        return;
      }
      fetch("/api/ops/regime_config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      })
        .then(r => r.json())
        .then(env => {
          if (env.status === "ok") {
            setToastMsg({ msg: "wrote " + diff.length + " change(s) · " + diff[0] + (diff.length > 1 ? " (+ " + (diff.length - 1) + " more)" : ""), level: "ok" });
          } else {
            setToastMsg({ msg: "write failed · " + (env.error || "unknown"), level: "warn" });
          }
        })
        .catch(e => setToastMsg({ msg: "POST error · " + e.message, level: "warn" }));
    };

    const numCell = (val, range, onChange, ariaLabel) => h("input", {
      type: "number",
      value: val != null ? val : 0,
      step: 0.01,
      min: range ? range[0] : undefined,
      max: range ? range[1] : undefined,
      onChange: e => onChange(Number(e.target.value)),
      className: "select",
      "aria-label": ariaLabel,
      style: { width: 86, fontFamily: "var(--mono)", fontSize: "var(--t-xs)", textAlign: "right" },
    });

    return h(Card, {
      num: "19", title: "Regime config editor",
      sub: "atomic write · " + (env.config_path || "config.json"),
      right: h(TimeSince, { ts: data.regime_config_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)" } })
    },
      h("details", { className: "decision-guide", style: {
        marginBottom: "var(--s-3)",
        background: "var(--bg-inset)",
        border: "1px solid var(--line-1)",
        borderRadius: 4,
      } },
        h("summary", { style: {
          padding: "var(--s-3) var(--s-4)",
          cursor: "pointer",
          fontSize: "var(--t-sm)",
          fontWeight: 600,
          color: "var(--fg-1)",
        } }, "Parameter guide · read before changing values"),
        h("div", {
          className: "guide-body",
          style: {
            padding: "var(--s-2) var(--s-4) var(--s-4)",
            borderTop: "1px solid var(--line-1)",
            fontSize: "var(--t-xs)",
            lineHeight: 1.6,
            color: "var(--fg-2)",
          },
          // Static template literal embedded above in this file; no user
          // input ever flows here — XSS surface is nil.
          dangerouslySetInnerHTML: { __html: REGIME_PARAMS_GUIDE_HTML }
        })
      ),
      h("div", { className: "metric-label" }, "ENTRY DELTA · per regime"),
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 6, marginTop: 4 } },
        regimes.map(r => h("label", { key: r, style: { display: "flex", flexDirection: "column", gap: 4 } },
          h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, r),
          numCell(form.entry_delta[r], schema.delta_range, (v) => setDelta("entry_delta", r, v), `entry delta for ${r}`)
        ))
      ),
      h("div", { className: "hr" }),
      h("div", { className: "metric-label" }, "EXIT DELTA · per regime"),
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 6, marginTop: 4 } },
        regimes.map(r => h("label", { key: r, style: { display: "flex", flexDirection: "column", gap: 4 } },
          h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, r),
          numCell(form.exit_delta[r], schema.delta_range, (v) => setDelta("exit_delta", r, v), `exit delta for ${r}`)
        ))
      ),
      h("div", { className: "hr" }),
      h("div", { className: "metric-label" }, "SCALAR PARAMS"),
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginTop: 4 } },
        scalars.map(k => h("label", { key: k, style: { display: "grid", gridTemplateColumns: "1fr auto", gap: 6, alignItems: "center" } },
          h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, k),
          numCell(form[k], (schema.scalar_ranges || {})[k], (v) => setScalar(k, v), k)
        ))
      ),
      h("div", { className: "hr" }),
      h("div", { style: { display: "flex", gap: "var(--s-3)", alignItems: "center" } },
        h("button", { className: "btn", onClick: submit }, "APPLY"),
        h("button", { className: "btn", onClick: reset }, "RESET"),
        toastMsg.msg && h("span", {
          className: toastMsg.level === "ok" ? "up" : toastMsg.level === "warn" ? "down" : "dim",
          style: { fontSize: "var(--t-xs)", fontFamily: "var(--mono)", flex: 1, textAlign: "right" }
        }, toastMsg.msg)
      )
    );
  }

  // ─────────────── SLACK PREVIEW — next daily report (data-num 20) ───────────────
  function SlackPreviewLive({ data }) {
    const env = envelopeData(data.slack_preview) || {};
    const sign = (env.pnl_usd || 0) >= 0 ? "+" : "−";
    const pnlAbs = Math.abs(Number(env.pnl_usd || 0));
    const emoji = (env.pnl_usd || 0) >= 0 ? "📈" : "📉";
    const regimeRows = env.regime_distribution || [];
    return h(Card, {
      num: "20", title: "Slack preview · next daily brief",
      sub: "fires at 00:00 UTC · " + (env.date_utc || ""),
      right: h(F, null,
        h(TimeSince, { ts: data.slack_preview_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        h("span", { className: "pill accent" }, h("span", { className: "dot accent pulse" }), " PREVIEW")
      )
    },
      h("div", {
        style: {
          background: "var(--bg-inset)", padding: "var(--s-3) var(--s-4)", borderRadius: 4,
          fontFamily: "var(--mono)", fontSize: "var(--t-xs)", lineHeight: 1.7, color: "var(--fg-1)",
          borderLeft: "3px solid var(--accent)",
        }
      },
        h("div", { style: { fontWeight: 600 } },
          emoji + " Quanta · daily P&L · " + (env.date_utc || "")),
        h("div", null,
          "• Day P&L: ",
          h("span", { className: (env.pnl_usd || 0) >= 0 ? "up" : "down" },
            sign + "$" + fmtUSD(pnlAbs, 2) + "  (" + fmtPct(env.pnl_pct || 0) + ")")),
        h("div", null, "• Trades: " + (env.trades || 0) + " · wins " + (env.wins || 0) + " · losses " + (env.losses || 0) + " · win rate " + (env.win_rate_pct || 0).toFixed(1) + "%"),
        h("div", null, "• Sharpe (trailing): " + (env.sharpe_trailing != null ? Number(env.sharpe_trailing).toFixed(2) : "—") +
          " · MaxDD: " + (env.max_dd_trailing != null ? Number(env.max_dd_trailing).toFixed(2) + "%" : "—")),
        env.best && h("div", null, "• Best pair: " + env.best.pair + " · $" + fmtUSD(env.best.pnl, 2) + " (n=" + env.best.n + ")"),
        env.worst && h("div", null, "• Worst pair: " + env.worst.pair + " · $" + fmtUSD(env.worst.pnl, 2) + " (n=" + env.worst.n + ")"),
        regimeRows.length > 0 && h("div", null,
          "• Regime distribution (24h): ",
          regimeRows.map(r => r.regime + " ×" + r.n).join(" · "))
      )
    );
  }

  // ─────────────── MCP TOOL CONSOLE (data-num 21) ───────────────
  function MCPToolConsole({ data }) {
    const env = envelopeData(data.tools) || {};
    const tools = env.tools || [];
    const [selected, setSelected] = useState("");
    const [argsText, setArgsText] = useState("{}");
    const [result, setResult] = useState(null);
    const [running, setRunning] = useState(false);
    const [err, setErr] = useState(null);

    useEffect(() => {
      if (!selected && tools.length) setSelected(tools[0].name);
    }, [tools.length]);

    const cur = tools.find(t => t.name === selected);
    useEffect(() => {
      // Generate a default args body matching the tool's params
      if (!cur) return;
      const defaults = {};
      (cur.params || []).forEach(p => {
        if (p.default !== null && p.default !== undefined) defaults[p.name] = p.default;
        else if (p.type === "int") defaults[p.name] = 0;
        else if (p.type === "bool") defaults[p.name] = false;
        else defaults[p.name] = "";
      });
      setArgsText(JSON.stringify(defaults, null, 2));
      setResult(null);
      setErr(null);
    }, [selected]);

    const run = () => {
      if (!selected) return;
      let body;
      try { body = JSON.parse(argsText || "{}"); }
      catch (e) { setErr("invalid JSON: " + e.message); return; }
      setRunning(true);
      setErr(null);
      setResult(null);
      fetch("/api/ops/mcp/" + selected, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })
        .then(r => r.json().then(j => ({ ok: r.ok, status: r.status, j })))
        .then(({ ok, status, j }) => {
          setRunning(false);
          if (!ok) setErr("HTTP " + status + " · " + (j && j.error ? j.error : ""));
          setResult(j);
        })
        .catch(e => { setRunning(false); setErr("fetch error: " + e.message); });
    };

    return h(Card, {
      num: "21", title: "MCP tool console",
      sub: tools.length + " tools · POST /api/ops/mcp/{name}",
      right: h(F, null,
        h(TimeSince, { ts: data.tools_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        cur && cur.mutating
          ? h("span", { className: "pill warn" }, h("span", { className: "dot warn pulse" }), " MUTATING")
          : h("span", { className: "pill" }, "read-only")
      )
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "260px 1fr", gap: "var(--s-3)" } },
        h("div", null,
          h("div", { className: "metric-label" }, "TOOL"),
          h("select", {
            className: "select",
            value: selected,
            onChange: e => setSelected(e.target.value),
            style: { width: "100%", marginTop: 4, fontFamily: "var(--mono)", fontSize: "var(--t-xs)" }
          },
            tools.map(t => h("option", { key: t.name, value: t.name }, (t.mutating ? "❗ " : "") + t.name))
          ),
          cur && h("div", { style: { marginTop: 8, fontSize: "var(--t-xs)", color: "var(--fg-2)" } }, cur.doc),
          cur && (cur.params || []).length > 0 && h("div", { style: { marginTop: 8 } },
            h("div", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, "PARAMS"),
            (cur.params || []).map(p => h("div", {
              key: p.name,
              style: { fontFamily: "var(--mono)", fontSize: "var(--t-2xs)", color: "var(--fg-3)", padding: "2px 0" }
            },
              p.name + " · " + p.type + (p.required ? " · required" : "") +
              (p.default !== undefined ? " · default=" + JSON.stringify(p.default) : "")
            ))
          )
        ),
        h("div", null,
          h("div", { className: "metric-label" }, "ARGS · JSON BODY"),
          h("textarea", {
            value: argsText,
            onChange: e => setArgsText(e.target.value),
            spellCheck: false,
            style: {
              width: "100%", height: 100, marginTop: 4,
              fontFamily: "var(--mono)", fontSize: "var(--t-xs)",
              background: "var(--bg-inset)", color: "var(--fg-1)",
              border: "1px solid var(--line-2)", borderRadius: 4, padding: 8,
              boxSizing: "border-box", resize: "vertical",
            }
          }),
          h("div", { style: { display: "flex", gap: 8, marginTop: 6, alignItems: "center" } },
            h("button", {
              className: "btn " + (cur && cur.mutating ? "warn" : ""),
              onClick: run, disabled: running,
            }, running ? "RUNNING…" : "EXECUTE"),
            err && h("span", { className: "down", style: { fontSize: "var(--t-xs)", fontFamily: "var(--mono)" } }, err)
          ),
          result && h("div", { style: { marginTop: 8 } },
            h("div", { className: "metric-label" }, "RESULT"),
            h("pre", {
              style: {
                background: "var(--bg-inset)", padding: 10, marginTop: 4,
                fontFamily: "var(--mono)", fontSize: "var(--t-2xs)", color: "var(--fg-2)",
                maxHeight: 240, overflow: "auto", borderRadius: 4,
              }
            }, JSON.stringify(result, null, 2))
          )
        )
      )
    );
  }

  // ─────────────── SENTIMENT card (compact) ───────────────
  function SentimentLive({ data }) {
    const slot = slotState(data, "sentiment");
    const env = envelopeData(slot.env) || {};
    const score = env.score;
    const klass = score == null ? "info" : score >= 0 ? "up" : "down";

    if (slot.phase === "down") {
      return h(Card, {
        num: "13", title: "Sentiment aggregate",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt)
      },
        h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }

    return h(Card, {
      num: "13", title: "Sentiment aggregate",
      sub: score != null ? "net " + (score >= 0 ? "+" : "") + score.toFixed(2) : "—",
      right: cardRight(slot.fetchedAt,
        h("span", { className: "pill " + klass }, score == null ? "—" : score >= 0 ? "BULLISH" : "BEARISH"))
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "DEEP (Claude)"),
        h("div", { className: "num " + ((env.deep_score || 0) >= 0 ? "up" : "down") },
          env.deep_score != null ? ((env.deep_score >= 0 ? "+" : "") + Number(env.deep_score).toFixed(2)) : "—"),
        h("div", { className: "dim mono" }, "FAST (Llama)"),
        h("div", { className: "num " + ((env.fast_score || 0) >= 0 ? "up" : "down") },
          env.fast_score != null ? ((env.fast_score >= 0 ? "+" : "") + Number(env.fast_score).toFixed(2)) : "—"),
        h("div", { className: "dim mono" }, "F&G"),
        h("div", { className: "num" }, env.fear_greed != null ? (env.fear_greed + (env.fear_greed_label ? " · " + env.fear_greed_label : "")) : "—"),
        h("div", { className: "dim mono" }, "AGREEMENT"),
        h("div", { className: "num " + (env.agreement ? "up" : "warn") }, env.agreement ? "yes" : "no"),
        h("div", { className: "dim mono" }, "HEADLINES"),
        h("div", { className: "num" }, env.n_headlines != null ? env.n_headlines : "—"),
        h("div", { className: "dim mono" }, "AGE"),
        h("div", { className: "num" }, env.age_s != null ? Math.floor(env.age_s / 60) + "m" : "—")
      )
    );
  }

  // ─────────────── SHARK BRIEFING (today's pre-market + market-open decisions) ───────────────
  // Surfaces Shark's actual decision flow — confirmed/skipped candidates,
  // market regime, macro context — read from stocks/memory/DAILY-HANDOFF.md
  // via the new /api/ops/shark_briefing endpoint. The operator's morning
  // question was "why no stocks trades?" — this card answers it inline.
  function SharkBriefingLive({ data }) {
    const slot = slotState(data, "shark_briefing");
    const env = envelopeData(slot.env) || {};
    const phases = env.phases || [];
    const latest = phases[phases.length - 1] || {};
    const dateLabel = env.handoff_date || "—";
    const regime = latest.regime || "—";
    const macro = latest.macro || "—";
    const regimeKlass = regime.startsWith("BULL") ? "up"
                      : regime.startsWith("BEAR") ? "down" : "info";
    const macroKlass = macro === "CLEAR" ? "up" : macro === "ELEVATED" ? "warn" : "info";

    if (slot.phase !== "ok") {
      return h(Card, {
        num: "13c", title: "Shark briefing · today's decisions",
        sub: slot.phase === "loading" ? "loading…" : "endpoint unavailable",
        right: cardRight(slot.fetchedAt)
      },
        slot.phase === "loading"
          ? h(LoadingState)
          : h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 30 })
      );
    }

    return h(Card, {
      num: "13c", title: "Shark briefing · " + dateLabel,
      sub: phases.length + " phase" + (phases.length === 1 ? "" : "s") + " logged",
      right: cardRight(slot.fetchedAt,
        h(F, null,
          h("span", { className: "pill " + regimeKlass, title: "Shark's market regime classifier (ATR + trend_score)" }, regime),
          " ",
          h("span", { className: "pill " + macroKlass, title: "Macro calendar (CPI/FOMC/NFP today or next day)" }, "MACRO " + macro)
        )
      )
    },
      phases.length === 0
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no phase blocks for today yet")
        : h(F, null,
            // Per-phase rows
            phases.map((p, i) => h("div", { key: i, style: { display: "flex", gap: 12, padding: "6px 0", borderBottom: "1px solid var(--line-2)", alignItems: "flex-start" } },
              h("div", { className: "mono dim", style: { fontSize: "var(--t-xs)", minWidth: 100 } }, p.phase + " · " + p.time + " " + p.tz),
              h("div", { style: { flex: 1, fontSize: "var(--t-xs)" } },
                p.confirmed.length
                  ? h("div", { style: { color: "var(--up)" } }, "✓ confirmed: ", p.confirmed.join(", "))
                  : h("div", { className: "dim" }, "✓ confirmed: (none)"),
                p.skipped.length
                  ? h("div", { className: "dim" }, "✗ skipped: " + p.skipped.join(", "))
                  : null,
                p.market_summary
                  ? h("div", { className: "dim", style: { fontSize: "var(--t-2xs)" } }, p.market_summary)
                  : null,
              ),
            )),
            // Trade-block explanation block — clarifies WHY no entries fired
            env.trade_block_explanation
              ? h("div", { style: { marginTop: 10, padding: "8px 10px", background: "var(--bg-inset)", borderRadius: 4, fontSize: "var(--t-2xs)", color: "var(--fg-2)", lineHeight: 1.5 } }, env.trade_block_explanation)
              : null
          )
    );
  }

  // ─────────────── CHAMPION GENOME (slow card, 60s) ───────────────
  function ChampionCardLive({ data }) {
    const slot = slotState(data, "ept_champion");
    const env = envelopeData(slot.env) || {};
    const id = env.member_id || env.genome_id || env.id || "—";
    const metrics = env.metrics || {};
    const sharpe = metrics.sharpe_ratio != null ? metrics.sharpe_ratio : metrics.sharpe;
    const maxDd = metrics.max_drawdown;
    const profitFactor = metrics.profit_factor;
    const nTrades = metrics.num_trades != null ? metrics.num_trades : metrics.n_trades;
    const fitness = env.fitness;
    const genome = env.genome || {};

    if (slot.phase === "down") {
      return h(Card, {
        num: "14", title: "EPT · champion genome",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt)
      },
        h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 60 })
      );
    }

    return h(Card, {
      num: "14", title: "EPT · champion genome",
      sub: "evolution head · refresh 60s",
      right: cardRight(slot.fetchedAt)
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "ID"),            h("div", { className: "num accent" }, id),
        h("div", { className: "dim mono" }, "FITNESS"),       h("div", { className: "num up" }, fitness != null ? Number(fitness).toFixed(3) : "—"),
        h("div", { className: "dim mono" }, "SHARPE"),        h("div", { className: "num" }, sharpe != null ? Number(sharpe).toFixed(2) : "—"),
        h("div", { className: "dim mono" }, "MAX DD"),        h("div", { className: "num down" }, maxDd != null ? "−" + (Number(maxDd) * 100).toFixed(2) + "%" : "—"),
        h("div", { className: "dim mono" }, "PROFIT FACTOR"), h("div", { className: "num" }, profitFactor != null ? Number(profitFactor).toFixed(2) : "—"),
        h("div", { className: "dim mono" }, "N TRADES"),      h("div", { className: "num" }, nTrades != null ? nTrades : "—"),
        h("div", { className: "dim mono" }, "STOP/TP"),       h("div", { className: "num mono", style: { fontSize: "var(--t-2xs)" } },
          (genome.stop_loss != null ? (Number(genome.stop_loss) * 100).toFixed(2) + "%" : "—") + " / "
          + (genome.take_profit != null ? (Number(genome.take_profit) * 100).toFixed(2) + "%" : "—")),
        h("div", { className: "dim mono" }, "FEATURES"),      h("div", { className: "num" }, (genome.feature_subset || []).length)
      )
    );
  }

  // ─────────────── TRADES RISK — daily PnL, DD, breaker ───────────────
  function TradesRiskLive({ data }) {
    const slot = slotState(data, "trades_risk");
    const env = envelopeData(slot.env) || {};
    // daily_pnl_pct, drawdown_pct_30d, live_tape[].pnl_pct are all fractional
    // ratios (e.g. -0.012305 = -1.23%) — multiply by 100 before fmtPct.
    const dayPnl = Number(env.daily_pnl_usd || 0);
    const dayPct = Number(env.daily_pnl_pct || 0) * 100;
    const dd30 = env.drawdown_pct_30d != null ? Number(env.drawdown_pct_30d) * 100 : null;
    const cb = env.circuit_breaker || {};
    const cbActive = cb.active === true;

    if (slot.phase === "down") {
      return h(Card, {
        num: "15", title: "Trades & risk · 24h",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt)
      },
        h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }

    return h(Card, {
      num: "15", title: "Trades & risk · 24h",
      sub: (env.open_count || 0) + " / " + (env.max_open || 0) + " open · " + (env.closed_today || 0) + " closed today",
      right: cardRight(slot.fetchedAt,
        cbActive
          ? h("span", { className: "pill down" }, h("span", { className: "dot down pulse" }), " BREAKER")
          : h("span", { className: "pill up" }, h("span", { className: "dot up" }), " OK"))
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "DAY PNL"),
        h("div", { className: "num " + (dayPnl >= 0 ? "up" : "down") }, (dayPnl >= 0 ? "+$" : "−$") + fmtUSD(Math.abs(dayPnl))),
        h("div", { className: "dim mono" }, "DAY %"),
        h("div", { className: "num " + (dayPct >= 0 ? "up" : "down") }, fmtPct(dayPct)),
        h("div", { className: "dim mono" }, "DD 30d"),
        h("div", { className: "num " + (dd30 != null && dd30 < 0 ? "down" : "dim") }, dd30 != null ? fmtPct(dd30) : "—"),
        h("div", { className: "dim mono" }, "OPEN"),
        h("div", { className: "num" }, (env.open_count || 0) + " / " + (env.max_open || 0))
      ),
      env.live_tape && env.live_tape.length > 0 && h("div", null,
        h("div", { className: "hr" }),
        h("div", { className: "metric-label" }, "LAST CLOSED · TAPE"),
        h("div", { style: { fontSize: "var(--t-xs)", maxHeight: 120, overflowY: "auto", marginTop: 4 } },
          env.live_tape.slice(0, 8).map((r, i) => {
            const tapePct = Number(r.pnl_pct || 0) * 100;
            return h("div", {
              key: i, style: { display: "grid", gridTemplateColumns: "1fr 60px 70px 1fr", gap: 6, padding: "2px 0" }
            },
              h("span", { className: "mono" }, r.pair),
              h("span", { className: "mono dim" }, r.side),
              h("span", { className: "num " + (tapePct >= 0 ? "up" : "down"), style: { textAlign: "right" } }, fmtPct(tapePct)),
              h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, r.regime_at_entry || "—")
            );
          })
        )
      )
    );
  }

  // ─────────────── BREAKERS detail card ───────────────
  function CircuitBreakersLive({ data }) {
    const slot = slotState(data, "circuit_breakers");
    const env = envelopeData(slot.env) || {};
    const breakers = env.breakers || [];
    const summary = env.summary || {};

    // Portfolio breaker (the one operator sees in unified_risk) — separate
    // registry from the LLM/MCP service breakers below. Reads the same
    // combined_portfolio envelope that the hero + topbar use, so the
    // tripped/armed state stays in lock-step with the rest of the page.
    const cpSlot = slotState(data, "combined_portfolio");
    const cp = envelopeData(cpSlot.env) || {};
    const portfolioTripped = !!cp.circuit_breaker_active;
    const dd = Number(cp.combined_drawdown_pct || 0);
    const ddThreshold = Number(cp.threshold_pct || 10);
    const stocksStale = !!cp.stocks_data_stale;
    const stocksUntrusted = !!cp.stocks_data_untrusted;
    const snapAge = Number(cp.snapshot_age_seconds || 0);
    const portfolioConditions = [
      { name: "combined drawdown", tripped: dd >= ddThreshold,
        detail: dd.toFixed(2) + "% / " + ddThreshold.toFixed(1) + "% threshold" },
      { name: "stocks data stale", tripped: stocksStale,
        detail: stocksStale ? ("snapshot " + Math.round(snapAge) + "s old (limit 600s)") : "snapshot fresh" },
      { name: "stocks data untrusted", tripped: stocksUntrusted,
        detail: stocksUntrusted ? ("snapshot >2h old — combined-dd fail-safe") : "trust window OK" },
    ];

    if (slot.phase === "down" && cpSlot.phase === "down") {
      return h(Card, {
        num: "16", title: "Circuit breakers",
        sub: "endpoints unavailable",
        right: cardRight(slot.fetchedAt)
      },
        h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }

    return h(Card, {
      num: "16", title: "Circuit breakers",
      sub: (portfolioTripped ? "PORTFOLIO TRIPPED · " : "portfolio armed · ")
        + (summary.open || 0) + " service open · " + (summary.total || 0) + " total",
      right: cardRight(slot.fetchedAt,
        h("span", { className: "pill " + (portfolioTripped ? "down" : "up"), style: { height: 18 } },
          h("span", { className: "dot " + (portfolioTripped ? "down pulse" : "up") }), " ",
          portfolioTripped ? "TRIPPED" : "ARMED"))
    },
      // ── Section A: portfolio breaker (unified_risk) ──
      h("div", { style: { marginBottom: "var(--s-3)" } },
        h("div", { className: "dim2 mono", style: { fontSize: "var(--t-2xs)", letterSpacing: ".08em", textTransform: "uppercase", marginBottom: "var(--s-2)" } },
          "Portfolio · unified_risk"),
        h("div", { style: { display: "flex", flexDirection: "column", gap: "var(--s-1)" } },
          portfolioConditions.map((c, i) => h("div", { key: i,
            style: { display: "flex", alignItems: "center", gap: 8, fontSize: "var(--t-xs)",
              padding: "var(--s-1) var(--s-2)", borderLeft: "2px solid " + (c.tripped ? "var(--c-down)" : "var(--c-up)"),
              background: c.tripped ? "color-mix(in srgb, var(--c-down) 7%, transparent)" : "transparent" } },
            h(GateBadge, { state: c.tripped ? "BLOCK" : "PASS" }),
            h("span", { style: { color: "var(--fg-1)" } }, c.name),
            h("span", { className: "tb-spacer", style: { flex: 1 } }),
            h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, c.detail)
          )))
      ),
      // ── Section B: service breakers (LLM / MCP / Anthropic) ──
      h("div", null,
        h("div", { className: "dim2 mono", style: { fontSize: "var(--t-2xs)", letterSpacing: ".08em", textTransform: "uppercase", marginBottom: "var(--s-2)" } },
          "Service · LLM / MCP"),
        breakers.length === 0
          ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)", padding: "var(--s-1) 0" } }, "no service breakers registered · all paths armed")
          : breakers.map((b, i) => h(StatusRow, {
              key: i,
              status: b.state === "open" ? "down" : b.state === "half_open" ? "warn" : "up",
              name: b.name || b.id || "breaker",
              sub: "failures " + (b.failure_count || 0) + " / threshold " + (b.failure_threshold || "—"),
              value: h("span", { className: "mono dim", style: { fontSize: "var(--t-2xs)" } },
                b.state, b.cooldown_remaining_s ? " · " + Math.round(b.cooldown_remaining_s) + "s" : "")
            }))
      )
    );
  }

  // ─────────────── DECISION AUDIT — per-pair why-trade rationale ───────────────
  // Mirrors the legacy /ops "Decision audit" card. Fetches the pair list from
  // /api/pairs and the last 5 decisions for the selected pair from
  // /api/ops/explainability/{base}/{quote}?limit=5. Decisions come in two
  // kinds: "entered" (full TFT/DRL/sentiment context) and "blocked" (risk
  // governor refused, with constraint name + reason).
  function ExplainabilityCardLive() {
    const [pairs, setPairs] = useState([]);
    const [selected, setSelected] = useState("");
    const [env, setEnv] = useState(null);
    const [fetchedAt, setFetchedAt] = useState(null);
    const [err, setErr] = useState(null);

    useEffect(() => {
      safeJsonFetch("/api/pairs")
        .then(j => {
          const list = (j && j.pairs) || [];
          setPairs(list);
          if (list.length && !selected) setSelected(list[0]);
        })
        .catch(() => { /* leave pairs empty — card renders the empty placeholder */ });
    }, []);

    useEffect(() => {
      if (!selected) return;
      const [base, quote] = selected.split("/");
      if (!base || !quote) return;
      const url = "/api/ops/explainability/"
        + encodeURIComponent(base) + "/" + encodeURIComponent(quote)
        + "?limit=5";
      const fetchNow = () => {
        setErr(null);
        safeJsonFetch(url)
          .then(j => { setEnv(j); setFetchedAt(new Date().toISOString()); })
          .catch(e => { setErr(String(e && e.message || e)); setFetchedAt(new Date().toISOString()); });
      };
      fetchNow();
      const iv = setInterval(fetchNow, 30_000);
      return () => clearInterval(iv);
    }, [selected]);

    const data = envelopeData(env) || {};
    const status = envelopeStatus(env);
    const decisions = data.decisions || [];
    const placeholder = (status === "degraded" || status === "down" || err || decisions.length === 0);

    return h(Card, {
      num: "22", title: "Decision audit",
      sub: selected ? ("last " + decisions.length + " decisions · " + selected) : "pick a pair…",
      right: cardRight(fetchedAt,
        h("select", {
          className: "select",
          value: selected,
          onChange: e => setSelected(e.target.value),
          "aria-label": "Pair selector for decision audit",
          style: { fontFamily: "var(--mono)", fontSize: "var(--t-xs)", minWidth: 110 },
        }, pairs.map(p => h("option", { key: p, value: p }, p))))
    },
      placeholder
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)", padding: "var(--s-2) 0" } },
            err ? "—" : (data.decisions != null ? "no recent decisions for this pair" : "—"))
        : h("div", { style: { display: "flex", flexDirection: "column", gap: "var(--s-3)" } },
            decisions.map((d, i) => {
              const isBlocked = d.kind === "blocked";
              const verdictCls = isBlocked ? "warn" : "up";
              const verdict = isBlocked ? "NO ENTRY · blocked" : ("ENTRY · " + (d.side || "long"));
              const reason = isBlocked
                ? (d.reason || "—") + " (constraint=" + (d.constraint || "—") + ")"
                : (d.reasoning || ((d.regime || "—") + " · conf " + (d.confidence != null ? Number(d.confidence).toFixed(2) : "—")));
              const ts = (d.ts || "").replace("T", " ").slice(0, 19);
              return h("div", {
                key: i,
                style: {
                  border: "1px solid var(--line-1)", borderRadius: 4,
                  padding: "var(--s-2) var(--s-3)",
                  display: "flex", flexDirection: "column", gap: 4,
                }
              },
                h("div", { style: { display: "flex", alignItems: "baseline", gap: "var(--s-2)" } },
                  h("span", { className: "mono dim", style: { fontSize: "var(--t-2xs)" } }, ts || "—"),
                  h("span", { className: "tb-spacer", style: { flex: 1 } }),
                  h("span", { className: "pill " + verdictCls, style: { height: 18 } }, verdict)),
                h("div", { className: "dim", style: { fontSize: "var(--t-xs)", lineHeight: 1.45 } }, reason)
              );
            })
          )
    );
  }

  // ─────────────── MAIN ───────────────
  function OpsApp() {
    const [killState, setKillStateRaw] = useState("normal");
    const { state: data } = useOpsData();

    // Wrap setKillState so any "killed" transition (Topbar OR Quick Actions card)
    // fires POST /api/ops/pause with the operator-kill reason. The KillSwitch
    // component handles its own 1500ms hold-to-confirm + pointermove-cancel.
    //
    // RESUME path was missing — once the operator hit the kill switch, the
    // visual chip flipped back to "normal" but no /api/ops/resume call was
    // ever made, so trading stayed paused. Mirror the pause call here so the
    // "normal" transition actually unpauses freqtrade.
    const killStateRef = useRef("normal");
    const setKillState = useCallback((next) => {
      const prev = killStateRef.current;
      killStateRef.current = next;
      setKillStateRaw(next);
      if (next === "killed" && prev !== "killed") {
        fetch("/api/ops/pause", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: "operator kill switch via spa" }),
        }).catch(() => { /* surfaced via Quick Actions toast if used there */ });
      } else if (next === "normal" && prev === "killed") {
        fetch("/api/ops/resume", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            reason: "operator kill switch resume via spa",
            confirm: true,
          }),
        }).catch(() => { /* surfaced via Quick Actions toast if used there */ });
      }
    }, []);

    useEffect(() => {
      // Theme + density are now seeded from localStorage by an inline boot
      // script in templates/ops_spa.html before React mounts (B-5).
      document.documentElement.style.setProperty("--accent", "#7c5cff");
    }, []);

    return h(F, null,
      h("div", { className: "app" },
        h(Topbar, { killState, setKillState, active: "ops" }),
        h(Sidebar, { active: "ops" }),
        h("main", { className: "main" },
          h("div", { className: "page-title" },
            h("h1", null, "Operations console"),
            h("span", { className: "breadcrumb" }, "/ ops_spa"),
            h("span", { className: "tb-spacer", style: { flex: 1 } }),
            h("span", { className: "mono dim", style: { fontSize: "var(--t-xs)" } }, "scroll · sections snap to view")
          ),
          // TODAY SCOREBOARD — operator's at-a-glance: capital, day P&L,
          // trades done, open positions, drawdown. Mounted ABOVE the hero
          // so it's the first thing the eye lands on (top of the page).
          h(TodayScoreboard, { data }),
          // HERO
          h(HeroLive, { data, killState }),
          // TRAINING ROW — crypto FreqAI + stocks Shark TFT side-by-side.
          // Operator wants both pipelines visible in one glance near the top
          // of the page (not buried at row 17). Wired data-num 17 (crypto)
          // and 13 (stocks) cards to a single training band.
          h("div", { id: "training", className: "grid g-12 anchor", style: { gap: "var(--gap-grid)" } },
            h("div", { style: { gridColumn: "span 6" } }, h(TrainingCardLive, { data })),
            h("div", { style: { gridColumn: "span 6" } }, h(StocksMLLive, { data }))
          ),
          // AGENT TIMELINE + RESEARCH FEED
          h("div", { id: "agent", className: "grid g-12 anchor", style: { gap: "var(--gap-grid)" } },
            h("div", { style: { gridColumn: "span 7" } }, h(AgentTimeline)),
            h("div", { id: "research", className: "anchor", style: { gridColumn: "span 5" } }, h(ResearchFeedLive, { data }))
          ),
          // GATES + LLM
          h("div", { id: "risk", className: "grid g-12 anchor", style: { gap: "var(--gap-grid)" } },
            h("div", { style: { gridColumn: "span 8" } }, h(EntryGatesLive, { data })),
            h("div", { id: "llm", className: "anchor", style: { gridColumn: "span 4", display: "flex", flexDirection: "column", gap: "var(--gap-grid)" } },
              h(LLMHealthLive, { data }),
              h(SentimentLive, { data })
              // StocksSentimentLive removed — superseded by SharkBriefingLive
              // (full-width card mounted below the LLM column).
            )
          ),
          // SHARK BRIEFING — full-width because the candidate lists can be long
          h(SharkBriefingLive, { data }),
          // PAIR TELEMETRY — crypto then stocks, both full-width.
          h(PairTelemetryLive, { data }),
          h(StocksPairTelemetryLive, { data }),
          // SERVICES + POSITIONS
          h("div", { className: "grid g-12", style: { gap: "var(--gap-grid)" } },
            h("div", { style: { gridColumn: "span 4" } }, h(ServicesLive, { data })),
            h("div", { style: { gridColumn: "span 8" } }, h(PositionsLive, { data }))
          ),
          // STOCKS ML banner moved to top training row.
          // STOCKS + MCP
          h("div", { className: "grid g-12", style: { gap: "var(--gap-grid)" } },
            h("div", { style: { gridColumn: "span 7" } }, h(StocksLive, { data })),
            h("div", { style: { gridColumn: "span 5" } }, h(MCPCardLive, { data }))
          ),
          // TRADES RISK + CHAMPION
          h("div", { className: "grid g-12", style: { gap: "var(--gap-grid)" } },
            h("div", { style: { gridColumn: "span 8" } }, h(TradesRiskLive, { data })),
            h("div", { style: { gridColumn: "span 4" } }, h(ChampionCardLive, { data }))
          ),
          // DECISION AUDIT — per-pair why-trade rationale (parity port from legacy /ops)
          h("div", { id: "decision-audit", className: "anchor" }, h(ExplainabilityCardLive)),
          // BREAKERS detail + CONTROL PANEL
          h("div", { id: "config", className: "grid g-12 anchor", style: { gap: "var(--gap-grid)" } },
            h("div", { style: { gridColumn: "span 6" } }, h(CircuitBreakersLive, { data })),
            h("div", { style: { gridColumn: "span 6" } }, h(QuickActions, { killState, setKillState }))
          ),
          // Agent C · 5 new cards (data-num 17..21). TrainingCard moved
          // to top training row alongside StocksML; ReadinessCard keeps
          // its place here next to the regime config + Slack preview row.
          h("div", { id: "readiness", className: "grid g-12 anchor", style: { gap: "var(--gap-grid)" } },
            h("div", { style: { gridColumn: "span 12" } }, h(ReadinessCardLive, { data }))
          ),
          h("div", { id: "regime-config", className: "grid g-12 anchor", style: { gap: "var(--gap-grid)" } },
            h("div", { style: { gridColumn: "span 7" } }, h(RegimeConfigEditor, { data })),
            h("div", { style: { gridColumn: "span 5" } }, h(SlackPreviewLive, { data }))
          ),
          h("div", { id: "mcp-console", className: "anchor" }, h(MCPToolConsole, { data })),
          h("div", { style: { padding: "var(--s-4) 0", textAlign: "center", color: "var(--fg-4)", fontSize: "var(--t-xs)", fontFamily: "var(--mono)" } },
            "QUANTA v2.6 · build " + new Date().toISOString().slice(0, 10))
        )
      )
    );
  }

  // Mount
  const root = ReactDOM.createRoot(document.getElementById("root"));
  root.render(h(OpsApp));
})();
