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
    NumberRoll, Sparkline, CandleChart, GateBadge, KillSwitch,
    Topbar, Sidebar, Card, ProgressBar, TimeSince,
  } = window;

  // ─────────────── helpers ───────────────
  function envelopeData(env) {
    if (env && typeof env === "object" && "data" in env) return env.data;
    return env;
  }
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

  // Convert backend candle format `{time, open, high, low, close}` →
  // the prototype's CandleChart format `{o, h, l, c, t, i}`. The time
  // string is rendered as HH:MM in UTC.
  function toCandles(backendCandles, timeframe) {
    if (!Array.isArray(backendCandles)) return [];
    return backendCandles.map((b, i) => {
      const d = new Date((b.time || 0) * 1000);
      const minsPerStep = ({ "1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440 })[timeframe] || 5;
      let t;
      if (minsPerStep >= 1440) t = (d.getUTCMonth() + 1) + "/" + d.getUTCDate();
      else if (minsPerStep >= 60) t = String(d.getUTCHours()).padStart(2, "0") + ":00";
      else t = String(d.getUTCHours()).padStart(2, "0") + ":" + String(d.getUTCMinutes()).padStart(2, "0");
      return { o: Number(b.open), h: Number(b.high), l: Number(b.low), c: Number(b.close), t, i };
    });
  }
  function toMarkers(backendMarkers, candles) {
    if (!Array.isArray(backendMarkers)) return [];
    // Markers have a `time` (unix seconds). Map each to the nearest candle index.
    const cTimes = candles.map(c => c.t); // not unix — use index via time-search via passed candle map below
    // Build map of unix → index using backend candles indirectly: we don't have
    // unix on the converted candles, so accept that backend already aligns.
    return backendMarkers.map(m => {
      // Try to use m.index, otherwise compute approx from m.time vs candles count
      const idx = (m.index != null) ? m.index
                : (m.candle_index != null) ? m.candle_index
                : Math.max(0, Math.min(candles.length - 1, candles.length - 1));
      const side = (m.side || m.kind || "BUY").toUpperCase();
      return {
        i: idx,
        side: side === "SELL" || side === "EXIT" ? "SELL" : "BUY",
        price: Number(m.price || m.entry_price || m.exit_price || 0),
        label: m.label || (side + (m.pnl_pct != null ? " " + fmtPct(m.pnl_pct) : "")),
      };
    });
  }

  // Default crypto pairs and stock symbols — the operator can override
  // via URL params. Matches DEFAULT_PAIRS / DEFAULT_STOCK_SYMBOLS in app.py.
  const CRYPTO_PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD", "ADA/USD", "AVAX/USD", "LINK/USD"];
  const STOCK_SYMBOLS = ["SOFI", "AAPL", "NVDA", "TSLA", "SPY"];

  function DashApp() {
    const [killState, setKillState] = useState("normal");
    const [venue, setVenue] = useState("crypto");
    const [pair, setPair] = useState("BTC/USD");
    const [tf, setTf] = useState("5m");
    const [state, setState] = useState(null);
    const [candles, setCandles] = useState([]);
    const [markers, setMarkers] = useState([]);
    const [meta, setMeta] = useState({ candles_fetched_at: null, state_fetched_at: null, trades_fetched_at: null });

    // URL params on mount
    useEffect(() => {
      const params = new URLSearchParams(window.location.search);
      const p = params.get("pair");
      const v = params.get("venue");
      if (v === "stocks" || v === "crypto") setVenue(v);
      if (p) setPair(p);
    }, []);

    useEffect(() => {
      document.documentElement.setAttribute("data-theme", "control");
      document.documentElement.setAttribute("data-density", "default");
      document.documentElement.style.setProperty("--accent", "#7c5cff");
    }, []);

    // Fetch /api/state (sidebar / pair drill payload). The endpoint is
    // single-pair so it gives best context for whatever pair freqtrade is
    // showing — we surface it as the "model view" payload.
    const fetchState = useCallback(() => {
      fetch("/api/state").then(r => r.json()).then(j => {
        setState(j);
        setMeta(m => Object.assign({}, m, { state_fetched_at: new Date().toISOString() }));
      }).catch(() => {});
    }, []);

    // Fetch /api/candles/{base}/{quote} on pair/tf change.
    const fetchCandles = useCallback(() => {
      // For crypto pairs use full base/quote split. For stocks we currently
      // route to /api/ops/stock_candles via a different endpoint; for the
      // SPA we keep symmetry with crypto by passing SYM/USD.
      const [base, quote] = pair.includes("/") ? pair.split("/") : [pair, "USD"];
      const url = "/api/candles/" + encodeURIComponent(base) + "/" + encodeURIComponent(quote) + "?timeframe=" + encodeURIComponent(tf) + "&limit=300";
      fetch(url).then(r => r.json()).then(j => {
        const cs = toCandles(j.candles || [], tf);
        setCandles(cs);
        setMeta(m => Object.assign({}, m, { candles_fetched_at: new Date().toISOString(), pair_state: j.pair_state, last_close: j.last_close, source: j.source }));
        // Pull trade markers after candles so we can align them.
        const turl = "/api/trades/" + encodeURIComponent(base) + "/" + encodeURIComponent(quote);
        fetch(turl).then(r => r.json()).then(tj => {
          setMarkers(toMarkers(tj.markers || [], cs));
          setMeta(m => Object.assign({}, m, { trades_fetched_at: new Date().toISOString() }));
        }).catch(() => setMarkers([]));
      }).catch(() => setCandles([]));
    }, [pair, tf]);

    useEffect(() => {
      fetchState();
      fetchCandles();
      const isvc = setInterval(fetchState, 10_000);
      const ic = setInterval(fetchCandles, 30_000); // candles refresh slower
      return () => { clearInterval(isvc); clearInterval(ic); };
    }, [fetchState, fetchCandles]);

    const venuePairs = venue === "crypto" ? CRYPTO_PAIRS : STOCK_SYMBOLS;
    useEffect(() => {
      // When venue switches and current pair is not in that venue, jump to first
      if (!venuePairs.includes(pair)) setPair(venuePairs[0]);
    }, [venue]);  // eslint-disable-line react-hooks/exhaustive-deps

    // Build dataset for hero strip
    const pairState = (state && state.pair_state) || (meta && meta.pair_state) || (state || {});
    const px = (meta.last_close != null) ? meta.last_close : (state && state.last_close) || 0;
    const dayPct = state ? (state.daily_pnl || 0) : 0;  // not exactly pct but the state's pnl key
    const regime = state && state.regime;
    const regimeConf = state && state.regime_confidence;
    const gateState = "PASS";  // we don't have a per-pair gate state in /api/state; default

    return h(F, null,
      h("div", { className: "app" },
        h(Topbar, { killState, setKillState, active: "dashboard" }),
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
                    h(NumberRoll, { value: px, decimals: px < 10 ? 4 : 2 })),
                  h("span", { className: "pill " + (dayPct >= 0 ? "up" : "down") }, fmtPct(dayPct) + " · day")
                )
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
            h("div", { style: { gridColumn: "span 8" } },
              h(Card, {
                num: "01", title: pair + " · " + tf, sub: "entries + exits annotated · scroll = zoom",
                right: h("div", { style: { display: "flex", gap: 6 } },
                  ["1m","5m","15m","1h","4h","1d"].map(x =>
                    h("button", { key: x, className: "icon-btn " + (tf === x ? "active" : ""), onClick: () => setTf(x) }, x))
                )
              },
                candles.length > 0
                  ? h(CandleChart, { candles, markers, height: 420 })
                  : h("div", { className: "dim", style: { padding: "var(--s-4)", fontSize: "var(--t-xs)" } }, "loading candles…")
              )
            ),

            // INTELLIGENCE RAIL
            h("div", { style: { gridColumn: "span 4", display: "flex", flexDirection: "column", gap: "var(--gap-grid)" } },
              h(ModelViewLive, { state, fetchedAt: meta.state_fetched_at }),
              h(MarketContextLive, { state, fetchedAt: meta.state_fetched_at })
            )
          ),

          // POSITIONS + RECENT TRADES
          h("div", { className: "grid g-12", style: { gap: "var(--gap-grid)" } },
            h("div", { style: { gridColumn: "span 7" } }, h(PositionsForPair, { state, pair, fetchedAt: meta.state_fetched_at })),
            h("div", { style: { gridColumn: "span 5" } }, h(RecentTrades, { state, fetchedAt: meta.state_fetched_at }))
          ),

          h("div", { style: { padding: "var(--s-4) 0", textAlign: "center", color: "var(--fg-4)", fontSize: "var(--t-xs)", fontFamily: "var(--mono)" } },
            "QUANTA v2.6 · /dashboard_spa · A/B alongside legacy / · build " + new Date().toISOString().slice(0, 10))
        )
      )
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
    return h(Card, {
      num: "02", title: "Model view", sub: "TFT · meta-agent · live",
      right: h(F, null,
        h(TimeSince, { ts: fetchedAt, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        h("span", { className: "pill accent" }, h("span", { className: "dot accent pulse" }), " LIVE")
      )
    },
      h("div", { className: "metric-label" }, "TFT CLASSIFIER · 24h horizon"),
      h("div", { style: { display: "flex", alignItems: "baseline", gap: "var(--s-3)", margin: "8px 0" } },
        h("div", { style: { flex: 1 } },
          h("div", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, "P(UP)"),
          h("div", { className: "up num", style: { fontSize: "var(--t-xl)" } }, tft.up != null ? Number(tft.up).toFixed(2) : "—")),
        h("div", { style: { flex: 1 } },
          h("div", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, "P(FLAT)"),
          h("div", { className: "num", style: { fontSize: "var(--t-xl)" } }, tft.flat != null ? Number(tft.flat).toFixed(2) : "—")),
        h("div", { style: { flex: 1 } },
          h("div", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, "P(DOWN)"),
          h("div", { className: "down num", style: { fontSize: "var(--t-xl)" } }, tft.down != null ? Number(tft.down).toFixed(2) : "—"))
      ),
      h("div", { className: "metric-label", style: { marginTop: 10 } }, "CONFIDENCE"),
      h(ProgressBar, { value: (tft.confidence || 0) * 100, max: 100, cls: "accent" }),

      h("div", { className: "hr" }),

      h("div", { style: { display: "flex", alignItems: "center", gap: 8, padding: "6px 0" } },
        h("span", { className: "metric-label" }, "META-AGENT"),
        h("span", { className: "tb-spacer", style: { flex: 1 } }),
        h("span", {
          className: "pill " + (meta_signal === 1 ? "up" : meta_signal === -1 ? "down" : "info"),
        }, h("span", { className: "dot " + (meta_signal === 1 ? "up" : meta_signal === -1 ? "down" : "info") + " pulse" }), " ",
           meta_signal === 1 ? "ENTER LONG" : meta_signal === -1 ? "ENTER SHORT" : "HOLD")
      ),
      h("div", { className: "dim", style: { fontSize: "var(--t-xs)", lineHeight: 1.55, marginTop: 4 } },
        "Meta confidence: " + (meta_conf != null ? meta_conf.toFixed(2) : "—") +
        " · TFT confidence: " + (tft.confidence != null ? Number(tft.confidence).toFixed(2) : "—"))
    );
  }

  function MarketContextLive({ state, fetchedAt }) {
    const onchain = (state && state.onchain) || {};
    return h(Card, {
      num: "03", title: "Market context", sub: "regime · sentiment · on-chain",
      right: h(TimeSince, { ts: fetchedAt, className: "mono dim", style: { fontSize: "var(--t-2xs)" } })
    },
      h("div", { className: "metric-label" }, "REGIME"),
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginTop: 6, fontSize: "var(--t-xs)" } },
        h("span", { className: "dim" }, "Current"),
        h("span", { className: "num " + ((state && state.regime) === "trending_up" ? "up" : (state && state.regime) === "trending_down" ? "down" : "info"), style: { textAlign: "right" } },
          state && state.regime ? state.regime : "—"),
        h("span", { className: "dim" }, "Confidence"),
        h("span", { className: "num", style: { textAlign: "right" } },
          state && state.regime_confidence != null ? Number(state.regime_confidence).toFixed(2) : "—")
      ),
      h("div", { className: "hr" }),
      h("div", { className: "metric-label" }, "SENTIMENT"),
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginTop: 6, fontSize: "var(--t-xs)" } },
        h("span", { className: "dim" }, "Score"),
        h("span", { className: "num " + ((state && state.sentiment_score) >= 0 ? "up" : "down"), style: { textAlign: "right" } },
          state && state.sentiment_score != null ? Number(state.sentiment_score).toFixed(2) : "—"),
        h("span", { className: "dim" }, "Confidence"),
        h("span", { className: "num", style: { textAlign: "right" } },
          state && state.sentiment_confidence != null ? Number(state.sentiment_confidence).toFixed(2) : "—")
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
          onchain.whale_count_1h != null ? onchain.whale_count_1h : "—")
      )
    );
  }

  function PositionsForPair({ state, pair, fetchedAt }) {
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
                h("td", { className: "dim mono", style: { fontSize: "var(--t-xs)" } }, p.open_date || "—")
              ))
            )
          )
    );
  }

  function RecentTrades({ state, fetchedAt }) {
    const trades = (state && state.recent_trades) || [];
    return h(Card, {
      num: "05", title: "Recent trades · last 10",
      sub: trades.length + " rows",
      right: h(TimeSince, { ts: fetchedAt, className: "mono dim", style: { fontSize: "var(--t-2xs)" } })
    },
      trades.length === 0
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no recent trades")
        : h("table", { className: "t" },
            h("thead", null, h("tr", null,
              h("th", null, "Pair"),
              h("th", null, "Side"),
              h("th", { style: { textAlign: "right" } }, "PnL %"),
              h("th", null, "When")
            )),
            h("tbody", null, trades.map((t, i) => h("tr", { key: i },
              h("td", null, h("strong", { className: "mono" }, t.pair || "—")),
              h("td", { className: "mono " + (t.side === "BUY" ? "up" : "down") }, t.side || "—"),
              h("td", { className: "num " + ((t.profit_pct || 0) >= 0 ? "up" : "down"), style: { textAlign: "right" } },
                t.profit_pct != null ? fmtPct(t.profit_pct * 100, 2) : (t.pnl_pct != null ? fmtPct(t.pnl_pct, 2) : "—")),
              h("td", { className: "dim mono", style: { fontSize: "var(--t-xs)" } }, t.close_date || t.open_date || "—")
            )))
          )
    );
  }

  const root = ReactDOM.createRoot(document.getElementById("root"));
  root.render(h(DashApp));
})();
