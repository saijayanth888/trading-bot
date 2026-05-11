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
    trades_risk: "/api/ops/trades_risk",
    live_trades: "/api/ops/live_trades",
    stocks_ml: "/api/ops/stocks_ml",
    stocks: "/api/ops/stocks",
    ollama_health: "/api/ops/ollama_health",
    circuit_breakers: "/api/ops/circuit_breakers",
    llm_stats: "/api/ops/llm_stats",
    mcp: "/api/ops/mcp",
    sentiment: "/api/ops/sentiment",
  };
  const SLOW_ENDPOINTS = {
    ept_champion: { url: "/api/ops/mcp/get_champion_genome", method: "POST", body: {} },
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

  // ─────────────── HERO — combined equity + 3-cell status ───────────────
  function HeroLive({ data, killState }) {
    const cp = envelopeData(data.combined_portfolio) || {};
    const crypto = cp.crypto || {};
    const stocks = cp.stocks || {};
    const equity = cp.total_equity_usd != null ? cp.total_equity_usd
                 : (Number(crypto.equity_usd || 0) + Number(stocks.equity_usd || 0));
    const dayPnl = cp.combined_day_pnl_usd != null ? cp.combined_day_pnl_usd
                 : (Number(crypto.day_pnl_usd || 0) + Number(stocks.day_pnl_usd || 0));
    const dayPct = cp.combined_day_pnl_pct != null ? cp.combined_day_pnl_pct
                 : (equity > 0 ? (dayPnl / equity) * 100 : 0);
    const dd = cp.combined_drawdown_pct != null ? cp.combined_drawdown_pct : 0;
    const sparks = envelopeData(data.sparklines);
    const seriesPair = sparks && sparks.pairs && Object.values(sparks.pairs)[0];
    const series = (seriesPair && seriesPair.closes && seriesPair.closes.length)
                  ? seriesPair.closes : [equity, equity, equity];

    const tr = envelopeData(data.trades_risk) || {};
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
    const modeCls = mode.mode === "live" ? "down" : mode.mode === "paused" ? "warn" : "warn";

    return h(F, null,
      ticker.length ? h(LiveTicker, { items: ticker }) : null,
      h("section", { className: "grid g-12", style: { gap: "var(--gap-grid)" } },
        h("div", { className: "card mountin", style: { gridColumn: "span 6", position: "relative", overflow: "hidden" } },
          h("div", { style: { padding: "var(--s-4) var(--s-5) 0", display: "flex", alignItems: "baseline", gap: "var(--s-3)" } },
            h("span", { className: "metric-label" }, "COMBINED EQUITY · CRYPTO + STOCKS"),
            h("span", { className: "pill " + modeCls }, h("span", { className: "dot " + modeCls + " pulse" }), " ", modeLabel),
            h("span", { className: "tb-spacer", style: { flex: 1 } }),
            h(TimeSince, { t: data.combined_portfolio_fetched_at, ts: data.combined_portfolio_fetched_at, className: "mono dim", style: { fontSize: "var(--t-xs)" } })
          ),
          h("div", { style: { padding: "var(--s-2) var(--s-5) var(--s-2)", display: "flex", alignItems: "flex-end", gap: "var(--s-6)" } },
            h("div", { style: { fontSize: "var(--t-hero)", fontWeight: 300, lineHeight: 1, letterSpacing: "-.025em" } },
              h(NumberRoll, { value: equity, prefix: "$", className: "num" })
            ),
            h("div", { style: { display: "flex", flexDirection: "column", gap: 6, paddingBottom: 14 } },
              h("span", { className: (dayPnl >= 0 ? "up" : "down") + " num", style: { fontSize: "var(--t-xl)" } },
                (dayPnl >= 0 ? "+$" : "−$") + fmtUSD(Math.abs(dayPnl))),
              h("span", { className: (dayPnl >= 0 ? "up" : "down") + " num", style: { fontSize: "var(--t-base)" } },
                fmtPct(dayPct) + " · day"),
              h("span", { className: "dim mono", style: { fontSize: "var(--t-xs)" } },
                "DD " + (dd || 0).toFixed(2) + "% / pause 8% / kill 10%"),
              h("div", { style: { width: 180, marginTop: 4 } },
                h(ProgressBar, { value: dd || 0, max: 10, ticks: [8, 10], cls: (dd >= 8 ? "warn" : "up") })
              )
            )
          ),
          h("div", { style: { height: 90, padding: "0 var(--s-3) var(--s-3)" } },
            h(Sparkline, { data: series, color: "--up", height: 90 })
          ),
          h("div", { style: { display: "flex", padding: "var(--s-3) var(--s-5)", borderTop: "1px solid var(--line-1)", gap: "var(--s-6)", flexWrap: "wrap" } },
            h(Mini, { lbl: "CRYPTO", v: "$" + fmtUSD(crypto.equity_usd || 0), d: fmtPct(crypto.day_pnl_pct || 0), up: (crypto.day_pnl_pct || 0) >= 0 }),
            h(Mini, { lbl: "STOCKS", v: "$" + fmtUSD(stocks.equity_usd || 0), d: fmtPct(stocks.day_pnl_pct || 0), up: (stocks.day_pnl_pct || 0) >= 0 }),
            h(Mini, { lbl: "OPEN", v: (liveSummary.total_active || 0) + " positions", d: (liveSummary.crypto_active || 0) + " cr · " + (liveSummary.wheel_active || 0) + " st" }),
            h(Mini, { lbl: "CLOSED 24h", v: (tr.closed_today || 0) + " trades", d: fmtUSD(tr.daily_pnl_usd || 0, 2) + " USD", up: (tr.daily_pnl_usd || 0) >= 0 }),
            h(Mini, { lbl: "BREAKER", v: cp.circuit_breaker_active ? "TRIPPED" : "armed", d: "pause " + (cp.pause_threshold_pct || 8) + "%" })
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
          (conf * 100).toFixed(0),
          h("span", { style: { fontSize: "var(--t-md)", color: "var(--fg-3)" } }, "%")
        ),
        h("span", { className: "mono dim", style: { fontSize: "var(--t-xs)" } },
          "conf · " + (dur != null ? durToHM(dur) : "—"))
      ),
      h(RegimeRibbon, { segments: segments })
    );
  }

  function BotStateCellLive({ mode, killState, data }) {
    const cls = killState === "killed" ? "down" : killState === "armed" ? "warn"
              : mode.state === "running" ? "up"
              : mode.state === "paused" ? "warn" : "info";
    const lbl = killState === "killed" ? "KILLED" : killState === "armed" ? "ARMED"
              : (mode.state || "—").toUpperCase();
    const champEnv = envelopeData(data.ept_champion) || {};
    const champion = (champEnv.genome_id || champEnv.id || "—");
    const sharpe = champEnv.sharpe != null ? champEnv.sharpe.toFixed(2) : "—";
    const services = envelopeData(data.services) || {};
    const ftStatus = (services.freqtrade && services.freqtrade.up) ? "freqtrade · ok"
                   : (services.freqtrade ? "freqtrade · down" : "—");
    return h("div", { className: "card mountin", style: { padding: "var(--s-3) var(--s-4)" } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "var(--s-2)" } },
        h("span", { className: "metric-label" }, "BOT STATE"),
        h("span", { className: "tb-spacer", style: { flex: 1 } }),
        h("span", { className: "pill " + cls }, h("span", { className: "dot " + cls + " pulse" }), " ", lbl)
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
    return h("div", { className: "card mountin", style: { padding: "var(--s-3) var(--s-4)", position: "relative", overflow: "hidden" } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "var(--s-2)" } },
        h("span", { className: "metric-label" }, "LIVE RESEARCH · SENTIMENT"),
        h("span", { className: "tb-spacer", style: { flex: 1 } }),
        h("span", { className: "pill accent" }, h("span", { className: "dot accent pulse" }), " AGENT")
      ),
      h("div", { style: { marginTop: 10 } },
        h("div", { className: "tl-source accent" },
          sent.score != null ? ("Aggregate " + (sent.score >= 0 ? "+" : "") + sent.score.toFixed(2)) : "—"),
        h("div", { className: "num", style: { fontSize: "var(--t-md)", marginTop: 4, color: "var(--fg-1)" } },
          first ? (first.title || first.headline || JSON.stringify(first).slice(0, 80)) : "no key events"),
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

  // ─────────────── RESEARCH STREAM — live from /api/ops/sentiment ───────────────
  function ResearchFeedLive({ data }) {
    const [expanded, setExpanded] = useState(null);
    const sent = envelopeData(data.sentiment) || {};
    const hourly = sent.hourly_24h || [];
    const events = sent.key_events || [];
    // Build a unified feed: hourly sentiment scores + key events
    const items = [];
    events.forEach((e, i) => {
      const title = (typeof e === "string") ? e : (e.title || e.headline || JSON.stringify(e));
      items.push({
        src: "Sentiment · Key event",
        title,
        body: (e.body || e.summary || "Notable shift surfaced by aggregator."),
        cites: e.sources || ["sentiment_log.key_events"],
        level: "info",
        ts: e.ts || sent.ts,
      });
    });
    hourly.slice(-12).reverse().forEach((row) => {
      items.push({
        src: "Aggregate sentiment",
        title: (row.score >= 0 ? "Bullish" : "Bearish") + " net " + row.score.toFixed(2),
        body: row.n + " headlines this hour",
        cites: ["sentiment_log", "hour=" + row.hour],
        level: row.score >= 0 ? "ok" : "warn",
        ts: row.hour,
      });
    });

    return h(Card, {
      num: "04", title: "Research stream · how the agent thinks",
      sub: "live · click to expand",
      right: h(F, null,
        h(TimeSince, { ts: data.sentiment_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        h("span", { className: "pill accent" }, h("span", { className: "dot accent pulse" }), " ", items.length, " EVENTS · 24h")
      )
    },
      h("div", { style: { display: "flex", flexDirection: "column", maxHeight: 380, overflowY: "auto" } },
        items.length === 0
          ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)", padding: "var(--s-3) 0" } }, "no sentiment events yet")
          : items.map((r, i) => {
              const open = expanded === i;
              const dotColor = r.level === "warn" ? "warn" : r.level === "ok" ? "up" : "accent";
              return h("div", {
                key: i,
                style: {
                  display: "grid", gridTemplateColumns: "60px 12px 1fr", gap: "var(--s-3)",
                  padding: "var(--s-3) 0", borderBottom: "1px solid var(--line-1)", cursor: "pointer",
                },
                onClick: () => setExpanded(open ? null : i)
              },
                h("div", { className: "mono dim", style: { fontSize: "var(--t-xs)", paddingTop: 2 } },
                  h(TimeSince, { ts: r.ts })),
                h("div", { style: { position: "relative", paddingTop: 6 } },
                  h("span", { className: "dot " + dotColor, style: { position: "relative", zIndex: 1 } }),
                  h("div", { style: { position: "absolute", left: 2, top: 12, bottom: -16, width: 1, background: "var(--line-2)" } })
                ),
                h("div", null,
                  h("div", { style: { display: "flex", alignItems: "baseline", gap: "var(--s-2)" } },
                    h("span", { className: "tl-source", style: { color: "var(--" + (dotColor === "up" ? "up" : dotColor === "warn" ? "warn" : "accent") + ")" } }, r.src),
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
  function EntryGatesLive({ data }) {
    const [expand, setExpand] = useState(null);
    const env = envelopeData(data.gates) || {};
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

    return h(Card, {
      num: "05", title: "Entry gates · why isn't anything trading?",
      sub: passing + "/" + all.length + " pairs eligible",
      right: h(F, null,
        h(TimeSince, { ts: data.gates_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        h("span", { className: "pill" }, h("span", { className: "dot warn" }), " ", blocked, " BLOCKED")
      )
    },
      h("table", { className: "t" },
        h("thead", null, h("tr", null,
          h("th", null, "Pair"),
          h("th", null, "Regime"),
          h("th", null, "Blocking"),
          h("th", null, "First blocker"),
          h("th", null, "")
        )),
        h("tbody", null, all.length === 0
          ? h("tr", null, h("td", { colSpan: 5, className: "dim", style: { fontSize: "var(--t-xs)", padding: "var(--s-3)" } }, "no gate data — endpoint returned empty"))
          : all.map((p, i) => h(F, { key: p.sym },
              h("tr", { onClick: () => setExpand(expand === i ? null : i), style: { cursor: "pointer" } },
                h("td", null, h("strong", null, p.sym)),
                h("td", null, h("span", { className: "pill " + (p.regime === "trending_up" ? "up" : p.regime === "trending_down" ? "down" : "info"), style: { height: 18 } }, p.regime || "—")),
                h("td", null, h(GateBadge, { state: p.blocking === 0 ? "PASS" : "BLOCK" })),
                h("td", { className: "dim", style: { fontSize: "var(--t-xs)" } }, p.first_blocker || "—"),
                h("td", { className: "dim mono", style: { fontSize: "var(--t-xs)" } }, expand === i ? "▾" : "▸")
              ),
              expand === i && h("tr", null, h("td", { colSpan: 5, style: { background: "var(--bg-inset)", padding: "var(--s-3) var(--s-4)" } },
                h("div", { className: "grid g-2", style: { gap: "var(--s-2)" } },
                  p.gates.map((g, gi) => h("div", { key: gi, style: { display: "flex", alignItems: "center", gap: 8 } },
                    h(GateBadge, { state: g.pass === true ? "PASS" : g.pass === false ? "BLOCK" : "NA" }),
                    h("span", { style: { fontSize: "var(--t-xs)", color: "var(--fg-1)" } }, g.gate),
                    h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)", marginLeft: "auto" } }, g.detail)
                  ))
                )
              ))
            ))
        )
      )
    );
  }

  // ─────────────── PAIR TELEMETRY — sparklines live ───────────────
  function PairTelemetryLive({ data }) {
    const env = envelopeData(data.sparklines) || {};
    const pairs = env.pairs || {};
    const entries = Object.entries(pairs);
    return h(Card, {
      num: "06", title: "Pair telemetry · 5m closes · trailing 24h",
      sub: entries.length + " pairs · auto-refresh 10s",
      right: h(TimeSince, { ts: data.sparklines_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)" } })
    },
      entries.length === 0
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "waiting for sparklines…")
        : h("div", { className: "grid g-4", style: { gap: "var(--s-3)" } },
            entries.map(([sym, p]) => {
              const data = p.closes || [];
              const pct = Number(p.pct_24h || 0);
              const px = Number(p.current || 0);
              const href = "/dashboard_spa?pair=" + encodeURIComponent(sym) + "&venue=crypto";
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

  // ─────────────── SERVICES — 8-row health probe ───────────────
  function ServicesLive({ data }) {
    const env = data.services || {};
    const services = envelopeData(env) || {};
    const rows = Object.entries(services);
    return h(Card, {
      num: "07a", title: "Service health · 8 probes",
      sub: env.status === "ok" ? "all green" : (env.error || "—"),
      right: h(TimeSince, { ts: data.services_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)" } })
    },
      h("div", { style: { display: "flex", flexDirection: "column" } },
        rows.length === 0
          ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "waiting…")
          : rows.map(([name, info]) => h(StatusRow, {
              key: name,
              status: info && info.up ? "up" : "down",
              name: name,
              sub: (info && info.detail) || "",
              value: h("span", null,
                info && info.latency_ms != null ? h("span", { className: "dim", style: { marginRight: 10 } }, info.latency_ms + "ms") : null,
                info && info.url ? h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, info.url) : null
              )
            }))
      )
    );
  }

  // ─────────────── LLM PROVIDERS + CIRCUIT BREAKERS ───────────────
  function LLMHealthLive({ data }) {
    const oh = envelopeData(data.ollama_health) || {};
    const cb = envelopeData(data.circuit_breakers) || {};
    const stats = envelopeData(data.llm_stats) || {};
    const saved = (stats.shark && stats.shark.total_api_cost_saved_usd) || stats.total_api_cost_saved_usd || 0;

    const ollamaModels = Array.isArray(oh.models) ? oh.models : Object.values(oh.models || {});
    const breakers = cb.breakers || [];

    return h(Card, {
      num: "07", title: "LLM providers · Ollama primary · Anthropic fallback",
      sub: "cost saved vs all-Anthropic baseline (24h)",
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
          sub: oh.healthy ? (ollamaModels.length + " models · " + (oh.host || "")) : (oh.error || "down"),
          value: h("span", null, h("span", { className: "dim", style: { marginRight: 10 } }, "lat ", oh.latency_ms || "—", "ms"))
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
    const env = envelopeData(data.live_trades) || {};
    const trades = env.trades || [];
    return h(Card, {
      num: "08", title: "Open positions", sub: "crypto + stocks · " + trades.length + " active",
      right: h(TimeSince, { ts: data.live_trades_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)" } })
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
    const env = envelopeData(data.stocks_ml) || {};
    const live = env.training_state === "running";
    const cur = env.current_epoch;
    const tot = env.epochs_target;
    const progress = (cur && tot) ? (cur / tot) * 100 : 0;
    return h(Card, {
      num: "09", title: "Stocks · Shark TFT",
      sub: env.weights_present ? "weights present" : "no model yet (Sun 11 PM ET)",
      right: h(F, null,
        h(TimeSince, { ts: data.stocks_ml_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        live
          ? h("span", { className: "pill accent" }, h("span", { className: "dot accent pulse" }), " LIVE TRAINING")
          : env.ml_enabled
            ? h("span", { className: "pill up" }, "ML ENABLED")
            : h("span", { className: "pill" }, "ML ALPHA")
      )
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

  // ─────────────── STOCKS — wheel + shark Alpaca state ───────────────
  function StocksLive({ data }) {
    const env = envelopeData(data.stocks) || {};
    const alpaca = env.alpaca || {};
    const wheel = env.wheel || {};
    const shark = env.shark || {};
    return h(Card, {
      num: "10", title: "Stocks · Wheel + Shark",
      sub: alpaca.paper ? "Alpaca · paper" : "Alpaca · live",
      right: h(TimeSince, { ts: data.stocks_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)" } })
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
      h("div", { className: "metric-label" }, "WHEEL · " + (wheel.open_positions || []).length + " open"),
      h("div", { style: { fontSize: "var(--t-xs)", marginTop: 4 } },
        "cumulative P&L: ", h("span", { className: "num " + ((wheel.cumulative_pnl_usd || 0) >= 0 ? "up" : "down") },
          "$", fmtUSD(wheel.cumulative_pnl_usd || 0))
      ),
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
    const env = envelopeData(data.mcp) || {};
    const probe = env.probe || {};
    const reachable = !!probe.ok_for_streamable_http;
    return h(Card, {
      num: "11", title: "MCP · wire status",
      sub: reachable ? "Hermes MCP reachable" : "MCP unreachable",
      right: h(F, null,
        h(TimeSince, { ts: data.mcp_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        h("span", { className: "pill " + (reachable ? "up" : "down") }, h("span", { className: "dot " + (reachable ? "up" : "down") + " pulse" }), " ", reachable ? "OK" : "DOWN")
      )
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "URL"),
        h("div", { className: "num mono", style: { fontSize: "var(--t-2xs)" } }, probe.url || "—"),
        h("div", { className: "dim mono" }, "STATUS"),
        h("div", { className: "num" }, probe.status_code != null ? probe.status_code : "—"),
        h("div", { className: "dim mono" }, "TOOLS"),
        h("div", { className: "num" }, (env.tools && env.tools.length) || 0)
      )
    );
  }

  // ─────────────── QUICK ACTIONS — kill switch ───────────────
  function QuickActions({ setKillState, killState }) {
    return h(Card, {
      num: "12", title: "Quick actions · control panel",
      sub: "atomic config writes · snapshots auto-saved"
    },
      h("div", { className: "grid g-2", style: { gap: "var(--s-3)" } },
        h("button", { className: "btn", onClick: () => fetch("/api/ops/pause", { method: "POST" }) }, "⏸ PAUSE TRADING"),
        h("button", { className: "btn", onClick: () => fetch("/api/ops/resume", { method: "POST" }) }, "▶ RESUME"),
        h("button", { className: "btn" }, "↻ RELOAD CONFIG"),
        h("button", { className: "btn warn" }, "⚡ TRIGGER EVOLUTION"),
        h("button", { className: "btn" }, "⚖ REBALANCE WEIGHTS"),
        h("button", { className: "btn" }, "⇣ DAILY SLACK BRIEF")
      ),
      h("div", { className: "hr" }),
      h("div", { style: { display: "flex", alignItems: "center", gap: "var(--s-3)" } },
        h("span", { className: "metric-label" }, "DESTRUCTIVE"),
        h(KillSwitch, {
          state: killState,
          onArm: () => setKillState("armed"),
          onKill: () => { setKillState("killed"); fetch("/api/ops/pause", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason: "kill switch" }) }); },
          onResume: () => setKillState("normal")
        }),
        h("span", { className: "dim", style: { fontSize: "var(--t-xs)", flex: 1, textAlign: "right" } },
          "ARM, then hold 1.5s to flatten all positions, cancel orders, halt strategy.")
      )
    );
  }

  // ─────────────── SENTIMENT card (compact) ───────────────
  function SentimentLive({ data }) {
    const env = envelopeData(data.sentiment) || {};
    const score = env.score;
    const klass = score == null ? "info" : score >= 0 ? "up" : "down";
    return h(Card, {
      num: "13", title: "Sentiment aggregate",
      sub: score != null ? "net " + (score >= 0 ? "+" : "") + score.toFixed(2) : "—",
      right: h(F, null,
        h(TimeSince, { ts: data.sentiment_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        h("span", { className: "pill " + klass }, score == null ? "—" : score >= 0 ? "BULLISH" : "BEARISH")
      )
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "DEEP (Claude)"),
        h("div", { className: "num " + (env.deep_score >= 0 ? "up" : "down") }, env.deep_score != null ? fmtPct(env.deep_score, 2).replace("%", "") : "—"),
        h("div", { className: "dim mono" }, "FAST (Llama)"),
        h("div", { className: "num " + (env.fast_score >= 0 ? "up" : "down") }, env.fast_score != null ? fmtPct(env.fast_score, 2).replace("%", "") : "—"),
        h("div", { className: "dim mono" }, "F&G"),
        h("div", { className: "num" }, env.fear_greed != null ? (env.fear_greed + (env.fear_greed_label ? " · " + env.fear_greed_label : "")) : "—"),
        h("div", { className: "dim mono" }, "AGREEMENT"),
        h("div", { className: "num " + (env.agreement ? "up" : "warn") }, env.agreement ? "yes" : "no")
      )
    );
  }

  // ─────────────── CHAMPION GENOME (slow card, 60s) ───────────────
  function ChampionCardLive({ data }) {
    const env = envelopeData(data.ept_champion) || {};
    const id = env.genome_id || env.id || "—";
    return h(Card, {
      num: "14", title: "EPT · champion genome",
      sub: "evolution head · refresh 60s",
      right: h(TimeSince, { ts: data.ept_champion_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)" } })
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "ID"),         h("div", { className: "num accent" }, id),
        h("div", { className: "dim mono" }, "SHARPE"),     h("div", { className: "num" }, env.sharpe != null ? env.sharpe.toFixed(2) : "—"),
        h("div", { className: "dim mono" }, "MAX DD"),     h("div", { className: "num down" }, env.max_drawdown != null ? "−" + (env.max_drawdown * 100).toFixed(2) + "%" : "—"),
        h("div", { className: "dim mono" }, "WIN RATE"),   h("div", { className: "num" }, env.win_rate != null ? (env.win_rate * 100).toFixed(0) + "%" : "—"),
        h("div", { className: "dim mono" }, "N TRADES"),   h("div", { className: "num" }, env.n_trades != null ? env.n_trades : "—"),
        h("div", { className: "dim mono" }, "GENERATION"), h("div", { className: "num" }, env.generation != null ? env.generation : "—")
      )
    );
  }

  // ─────────────── TRADES RISK — daily PnL, DD, breaker ───────────────
  function TradesRiskLive({ data }) {
    const env = envelopeData(data.trades_risk) || {};
    return h(Card, {
      num: "15", title: "Trades & risk · 24h",
      sub: env.open_count + " / " + env.max_open + " open · " + (env.closed_today || 0) + " closed today",
      right: h(F, null,
        h(TimeSince, { ts: data.trades_risk_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        env.circuit_breaker
          ? h("span", { className: "pill down" }, h("span", { className: "dot down pulse" }), " BREAKER")
          : h("span", { className: "pill up" }, h("span", { className: "dot up" }), " OK")
      )
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "DAY PNL"),
        h("div", { className: "num " + ((env.daily_pnl_usd || 0) >= 0 ? "up" : "down") }, "$" + fmtUSD(env.daily_pnl_usd || 0)),
        h("div", { className: "dim mono" }, "DAY %"),
        h("div", { className: "num " + ((env.daily_pnl_pct || 0) >= 0 ? "up" : "down") }, fmtPct(env.daily_pnl_pct || 0)),
        h("div", { className: "dim mono" }, "DD 30d"),
        h("div", { className: "num down" }, env.drawdown_pct_30d != null ? "−" + Math.abs(env.drawdown_pct_30d).toFixed(2) + "%" : "—"),
        h("div", { className: "dim mono" }, "OPEN"),
        h("div", { className: "num" }, env.open_count + " / " + env.max_open)
      ),
      env.live_tape && env.live_tape.length > 0 && h("div", null,
        h("div", { className: "hr" }),
        h("div", { className: "metric-label" }, "LAST CLOSED · TAPE"),
        h("div", { style: { fontSize: "var(--t-xs)", maxHeight: 120, overflowY: "auto", marginTop: 4 } },
          env.live_tape.slice(0, 8).map((r, i) => h("div", {
            key: i, style: { display: "grid", gridTemplateColumns: "1fr 60px 60px 1fr", gap: 6, padding: "2px 0" }
          },
            h("span", { className: "mono" }, r.pair),
            h("span", { className: "mono dim" }, r.side),
            h("span", { className: "num " + ((r.pnl_pct || 0) >= 0 ? "up" : "down"), style: { textAlign: "right" } }, fmtPct(r.pnl_pct || 0)),
            h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, r.regime_at_entry || "—")
          ))
        )
      )
    );
  }

  // ─────────────── BREAKERS detail card ───────────────
  function CircuitBreakersLive({ data }) {
    const env = envelopeData(data.circuit_breakers) || {};
    const breakers = env.breakers || [];
    const summary = env.summary || {};
    return h(Card, {
      num: "16", title: "Circuit breakers",
      sub: (summary.open || 0) + " open · " + (summary.half_open || 0) + " half-open · " + (summary.total || 0) + " total",
      right: h(TimeSince, { ts: data.circuit_breakers_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)" } })
    },
      breakers.length === 0
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no breakers registered")
        : breakers.map((b, i) => h(StatusRow, {
            key: i,
            status: b.state === "open" ? "down" : b.state === "half_open" ? "warn" : "up",
            name: b.name || b.id || "breaker",
            sub: "failures " + (b.failure_count || 0) + " / threshold " + (b.failure_threshold || "—"),
            value: h("span", { className: "mono dim", style: { fontSize: "var(--t-2xs)" } },
              b.state, b.cooldown_remaining_s ? " · " + Math.round(b.cooldown_remaining_s) + "s" : "")
          }))
    );
  }

  // ─────────────── MAIN ───────────────
  function OpsApp() {
    const [killState, setKillState] = useState("normal");
    const { state: data } = useOpsData();

    useEffect(() => {
      document.documentElement.setAttribute("data-theme", "control");
      document.documentElement.setAttribute("data-density", "default");
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
          // HERO
          h(HeroLive, { data, killState }),
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
            )
          ),
          // PAIR TELEMETRY
          h(PairTelemetryLive, { data }),
          // SERVICES + POSITIONS
          h("div", { className: "grid g-12", style: { gap: "var(--gap-grid)" } },
            h("div", { style: { gridColumn: "span 4" } }, h(ServicesLive, { data })),
            h("div", { style: { gridColumn: "span 8" } }, h(PositionsLive, { data }))
          ),
          // STOCKS ML banner (big when training)
          h(StocksMLLive, { data }),
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
          // BREAKERS detail + CONTROL PANEL
          h("div", { id: "config", className: "grid g-12 anchor", style: { gap: "var(--gap-grid)" } },
            h("div", { style: { gridColumn: "span 6" } }, h(CircuitBreakersLive, { data })),
            h("div", { style: { gridColumn: "span 6" } }, h(QuickActions, { killState, setKillState }))
          ),
          h("div", { style: { padding: "var(--s-4) 0", textAlign: "center", color: "var(--fg-4)", fontSize: "var(--t-xs)", fontFamily: "var(--mono)" } },
            "QUANTA v2.6 · /ops_spa · A/B alongside legacy /ops · build " + new Date().toISOString().slice(0, 10))
        )
      )
    );
  }

  // Mount
  const root = ReactDOM.createRoot(document.getElementById("root"));
  root.render(h(OpsApp));
})();
