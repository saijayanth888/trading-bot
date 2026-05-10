// Dashboard: chart + sidebar + WebSocket. Pure-vanilla, no build step.
//
// Lightweight Charts v4 is loaded via the CDN script in index.html.
//
// State machine:
//   1. on load → fetch /api/candles + /api/trades + /api/state for the
//      default pair.
//   2. open WS to /ws → push sidebar updates every 30s.
//   3. on pair / timeframe change → reload candles.
//   4. WS disconnect → reconnect with exponential backoff.

(() => {
  "use strict";

  const cfg = window.DASHBOARD_CONFIG || {};

  // ---- Element refs ------------------------------------------------------

  const $ = (id) => document.getElementById(id);
  const els = {
    pair:        $("pair-select"),
    tf:          $("timeframe-select"),
    title:       $("chart-title"),
    meta:        $("chart-meta"),
    main:        $("chart-main"),
    rsi:         $("chart-rsi"),
    macd:        $("chart-macd"),
    regime:      $("regime-ribbon"),
    wsIndicator: $("ws-indicator"),
    modeBadge:   $("mode-badge"),
    meters: {
      regime:    $("meter-regime"),
      sentiment: $("meter-sentiment"),
      meta:      $("meter-meta"),
      tft:       $("meter-tft"),
    },
    state: {
      regime:        $("state-regime"),
      sentiment:     $("state-sentiment"),
      meta:          $("state-meta"),
      tft:           $("state-tft"),
      tftConf:       $("state-tft-conf"),
      netflow:       $("state-netflow"),
      mvrv:          $("state-mvrv"),
      whale:         $("state-whale"),
      gen:           $("state-gen"),
      champ:         $("state-champ"),
      champFitness:  $("state-champ-fit"),
      runner:        $("state-runner"),
      dailyPnl:      $("state-daily-pnl"),
    },
    positions: $("positions"),
    pnlHist:   $("pnl-history"),
    recent:    $("recent-trades"),
  };

  if (cfg.defaultTimeframe) els.tf.value = cfg.defaultTimeframe;

  // ---- DOM helpers (textContent only — never innerHTML) ------------------

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null) continue;
      if (k === "className") node.className = v;
      else if (k === "title") node.title = v;
      else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
      else if (k.startsWith("on") && typeof v === "function") {
        node.addEventListener(k.slice(2).toLowerCase(), v);
      } else {
        node.setAttribute(k, v);
      }
    }
    for (const child of children) {
      if (child == null || child === false) continue;
      if (Array.isArray(child)) {
        child.forEach((c) => c != null && node.appendChild(
          c instanceof Node ? c : document.createTextNode(String(c))
        ));
      } else if (child instanceof Node) {
        node.appendChild(child);
      } else {
        node.appendChild(document.createTextNode(String(child)));
      }
    }
    return node;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  // ---- Charts ------------------------------------------------------------

  const REGIME_COLORS = {
    "trending_up":      "rgba(34, 197, 94, 0.18)",
    "trending_down":    "rgba(239, 68, 68, 0.18)",
    "mean_reverting":   "rgba(59, 130, 246, 0.18)",
    "high_volatility":  "rgba(245, 158, 11, 0.18)",
    "unknown":          "rgba(138, 147, 179, 0.10)",
  };
  const REGIME_LABELS = {
    "trending_up":     "↗ trending up",
    "trending_down":   "↘ trending down",
    "mean_reverting":  "↔ mean-reverting",
    "high_volatility": "⚡ high vol",
    "unknown":         "—",
  };

  const baseChartOpts = {
    layout: { background: { type: "solid", color: "transparent" }, textColor: "#a8b1cb" },
    grid:   { vertLines: { color: "rgba(138,147,179,0.08)" }, horzLines: { color: "rgba(138,147,179,0.08)" } },
    rightPriceScale: { borderColor: "rgba(138,147,179,0.2)" },
    timeScale:       { borderColor: "rgba(138,147,179,0.2)", timeVisible: true, secondsVisible: false },
    crosshair: { mode: 1 },
  };

  // Main chart
  const mainChart = LightweightCharts.createChart(els.main, baseChartOpts);
  const candleSeries = mainChart.addCandlestickSeries({
    upColor: "#22c55e", downColor: "#ef4444", borderVisible: false,
    wickUpColor: "#22c55e", wickDownColor: "#ef4444",
  });
  const bbUpperSeries = mainChart.addLineSeries({ color: "rgba(79,141,247,0.7)", lineWidth: 1 });
  const bbMidSeries   = mainChart.addLineSeries({ color: "rgba(79,141,247,0.55)", lineStyle: 1, lineWidth: 1 });
  const bbLowerSeries = mainChart.addLineSeries({ color: "rgba(79,141,247,0.7)", lineWidth: 1 });
  // Trend overlays
  const ema20Series = mainChart.addLineSeries({ color: "#f59e0b", lineWidth: 1.5, lastValueVisible: true, title: "EMA20" });
  const ema50Series = mainChart.addLineSeries({ color: "#8b5cf6", lineWidth: 1.5, lastValueVisible: true, title: "EMA50" });
  const vwapSeries  = mainChart.addLineSeries({ color: "#22d3ee", lineWidth: 1.5, lineStyle: 2, lastValueVisible: true, title: "VWAP" });
  // Volume on a thin overlay scale at the bottom
  const volumeSeries = mainChart.addHistogramSeries({
    priceFormat: { type: "volume" },
    priceScaleId: "vol",
    color: "rgba(255,255,255,0.45)",
  });
  mainChart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

  // RSI subchart
  const rsiChart = LightweightCharts.createChart(els.rsi, baseChartOpts);
  const rsiSeries = rsiChart.addLineSeries({ color: "#f59e0b", lineWidth: 2 });
  rsiChart.priceScale("right").applyOptions({ entireTextOnly: true });
  rsiSeries.createPriceLine({ price: 70, color: "rgba(239,68,68,0.6)", lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: "70" });
  rsiSeries.createPriceLine({ price: 30, color: "rgba(34,197,94,0.6)", lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: "30" });

  // MACD subchart
  const macdChart = LightweightCharts.createChart(els.macd, baseChartOpts);
  const macdHist  = macdChart.addHistogramSeries({});
  const macdLine  = macdChart.addLineSeries({ color: "#4f8df7", lineWidth: 2 });
  const macdSig   = macdChart.addLineSeries({ color: "#f59e0b", lineWidth: 1 });

  // Synchronise time scales across all 3 charts
  function syncCharts() {
    const charts = [mainChart, rsiChart, macdChart];
    charts.forEach((src) => {
      src.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        if (!range) return;
        charts.forEach((dst) => {
          if (dst === src) return;
          dst.timeScale().setVisibleLogicalRange(range);
        });
      });
    });
  }
  syncCharts();

  function resizeCharts() {
    mainChart.applyOptions({ width: els.main.clientWidth, height: els.main.clientHeight });
    rsiChart.applyOptions({  width: els.rsi.clientWidth,  height: els.rsi.clientHeight  });
    macdChart.applyOptions({ width: els.macd.clientWidth, height: els.macd.clientHeight });
  }
  window.addEventListener("resize", resizeCharts);
  setTimeout(resizeCharts, 50);

  // ---- Data loaders ------------------------------------------------------

  async function getJson(path) {
    const r = await fetch(path, { headers: { "Accept": "application/json" } });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return await r.json();
  }

  function pairToPath(pair) {
    const [base, quote] = pair.split("/");
    return [encodeURIComponent(base), encodeURIComponent(quote)];
  }

  // Map chart-page timeframe codes ("1m", "5m", "15m", "1h", "1d") to
  // the Alpaca-style strings the stock-candles endpoint expects.
  const STOCK_TF = { "1m": "1Min", "5m": "5Min", "15m": "15Min", "1h": "1Hour", "1d": "1Day" };

  function isStockSymbol(pair) {
    // Crypto pairs always have a slash ("BTC/USD"); stocks do not ("SOFI").
    return typeof pair === "string" && !pair.includes("/");
  }

  // Track price-line annotations on the main chart so we can clear them
  // before re-drawing on each pair switch.
  let activePriceLines = [];
  function clearPriceLines() {
    activePriceLines.forEach((pl) => {
      try { candleSeries.removePriceLine(pl); } catch (_) { /* no-op */ }
    });
    activePriceLines = [];
  }

  async function loadStockPair(symbol, timeframe) {
    const stockTf = STOCK_TF[timeframe] || "5Min";
    els.title.textContent = `${symbol} — ${timeframe} · Alpaca paper`;
    clear(els.meta);
    els.meta.appendChild(document.createTextNode("loading stock…"));

    let envelope;
    try {
      envelope = await getJson(`/api/ops/stock_candles/${encodeURIComponent(symbol)}?timeframe=${encodeURIComponent(stockTf)}`);
    } catch (exc) {
      clear(els.meta);
      els.meta.appendChild(document.createTextNode(`stock candle fetch failed: ${exc.message}`));
      return;
    }
    if (!envelope || !envelope.data) {
      clear(els.meta);
      els.meta.appendChild(document.createTextNode(envelope?.error || "no data"));
      return;
    }
    const bars = envelope.data.bars || [];
    if (!bars.length) {
      clear(els.meta);
      els.meta.appendChild(document.createTextNode("no bars available — cron may not have run yet"));
      return;
    }

    candleSeries.setData(bars);
    volumeSeries.setData(bars.map((b) => ({
      time: b.time,
      value: b.volume || 0,
      color: b.close >= b.open ? "rgba(63,185,80,0.5)" : "rgba(248,81,73,0.5)",
    })));
    // Stocks have no in-house indicators / regime ribbon — clear them all.
    bbUpperSeries.setData([]);
    bbMidSeries.setData([]);
    bbLowerSeries.setData([]);
    ema20Series.setData([]);
    ema50Series.setData([]);
    vwapSeries.setData([]);
    rsiSeries.setData([]);
    macdHist.setData([]);
    macdLine.setData([]);
    macdSig.setData([]);
    setRegimeShading([], bars);
    renderRegimeRibbon([]);
    candleSeries.setMarkers([]);
    clearPriceLines();

    // Overlay open wheel positions (CSP / CC) on the SOFI chart as
    // horizontal price lines so the operator sees their strikes inline.
    try {
      const stocksEnv = await getJson("/api/ops/stocks");
      const positions = (stocksEnv?.data?.wheel?.open_positions || [])
        .filter((p) => p.underlying === symbol);
      for (const p of positions) {
        const isPut = p.kind === "short_put";
        const isCall = p.kind === "short_call";
        if (!isPut && !isCall) continue;
        const color = isPut ? "#ff9a9a" : "#9ab8ff";
        const titleTag = isPut ? "CSP" : "CC";
        const pl = candleSeries.createPriceLine({
          price: Number(p.strike),
          color,
          lineWidth: 1,
          lineStyle: 2,  // dashed
          axisLabelVisible: true,
          title: `${titleTag} ${p.expiry || ""}`,
        });
        activePriceLines.push(pl);
      }
    } catch (_) { /* if /api/ops/stocks is degraded, still show the chart */ }

    clear(els.meta);
    const last = bars[bars.length - 1];
    els.meta.appendChild(document.createTextNode("last "));
    els.meta.appendChild(el("b", {}, fmtPrice(last.close)));
    els.meta.appendChild(document.createTextNode("  ·  "));
    els.meta.appendChild(el("span", { style: { color: "var(--muted)" } },
      `${bars.length} bars · candles ${envelope.data.age_seconds == null ? "—" : envelope.data.age_seconds + "s"} old`));

    mainChart.timeScale().fitContent();
    rsiChart.timeScale().fitContent();
    macdChart.timeScale().fitContent();
  }

  async function loadPair(pair, timeframe) {
    if (isStockSymbol(pair)) {
      return loadStockPair(pair, timeframe);
    }
    els.title.textContent = `${pair} — ${timeframe}`;
    clear(els.meta);
    els.meta.appendChild(document.createTextNode("loading…"));
    clearPriceLines();
    const [base, quote] = pairToPath(pair);
    let candleData, tradeData;
    try {
      [candleData, tradeData] = await Promise.all([
        getJson(`/api/candles/${base}/${quote}?timeframe=${encodeURIComponent(timeframe)}`),
        getJson(`/api/trades/${base}/${quote}`),
      ]);
    } catch (exc) {
      clear(els.meta);
      els.meta.appendChild(document.createTextNode(`error: ${exc.message}`));
      return;
    }

    // Apply to the main chart
    candleSeries.setData(candleData.candles || []);
    volumeSeries.setData(candleData.volume || []);
    bbUpperSeries.setData(candleData.indicators.bb_upper || []);
    bbMidSeries.setData(candleData.indicators.bb_mid || []);
    bbLowerSeries.setData(candleData.indicators.bb_lower || []);
    ema20Series.setData(candleData.indicators.ema20 || []);
    ema50Series.setData(candleData.indicators.ema50 || []);
    vwapSeries.setData(candleData.indicators.vwap || []);
    candleSeries.setMarkers(tradeData.markers || []);

    // RSI + MACD subcharts
    rsiSeries.setData(candleData.indicators.rsi || []);
    macdHist.setData(candleData.indicators.macd_hist || []);
    macdLine.setData(candleData.indicators.macd || []);
    macdSig.setData(candleData.indicators.macd_signal || []);

    // Regime shading (background bands) + ribbon panel
    setRegimeShading(candleData.regime_segments || [], candleData.candles || []);
    renderRegimeRibbon(candleData.regime_segments || []);

    // Meta line
    clear(els.meta);
    const lc = candleData.last_close;
    if (lc != null) {
      els.meta.appendChild(document.createTextNode("last "));
      els.meta.appendChild(el("b", {}, fmtPrice(lc)));
    }
    const ps = candleData.pair_state || {};
    if (ps.regime) {
      els.meta.appendChild(document.createTextNode("  ·  "));
      els.meta.appendChild(el("span", {
        className: "pill",
        style: { background: REGIME_COLORS[ps.regime] || "#222",
                 padding: "2px 6px", borderRadius: "6px" },
      }, REGIME_LABELS[ps.regime] || ps.regime));
    }
    if (candleData.source) {
      els.meta.appendChild(document.createTextNode("  ·  "));
      els.meta.appendChild(el("span", { style: { color: "var(--muted)" } }, `src=${candleData.source}`));
    }

    // Snap the time view to a sensible default
    mainChart.timeScale().fitContent();
    rsiChart.timeScale().fitContent();
    macdChart.timeScale().fitContent();
  }

  // Background regime shading is implemented as a series of low-opacity
  // area series — one per regime segment — that sit behind the candles.
  let regimeAreaSeriesPool = [];
  function setRegimeShading(segments, candles) {
    regimeAreaSeriesPool.forEach((s) => mainChart.removeSeries(s));
    regimeAreaSeriesPool = [];
    if (!segments.length || !candles.length) return;
    let yMax = -Infinity;
    for (const c of candles) if (c.high > yMax) yMax = c.high;
    if (!isFinite(yMax) || yMax === -Infinity) yMax = 1;
    segments.forEach((seg) => {
      const color = REGIME_COLORS[seg.label] || REGIME_COLORS["unknown"];
      const series = mainChart.addAreaSeries({
        topColor:    color,
        bottomColor: color,
        lineColor:   color,
        lineWidth:   0,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      series.setData([
        { time: seg.start, value: yMax },
        { time: seg.end,   value: yMax },
      ]);
      regimeAreaSeriesPool.push(series);
    });
  }

  function renderRegimeRibbon(segments) {
    clear(els.regime);
    if (!segments.length) {
      els.regime.appendChild(el(
        "div",
        { className: "regime-segment", style: { background: REGIME_COLORS.unknown } },
        "no regime data",
      ));
      return;
    }
    const total = segments[segments.length - 1].end - segments[0].start || 1;
    segments.forEach((seg) => {
      const w = Math.max(0.01, (seg.end - seg.start) / total);
      const span = el("div", {
        className: "regime-segment",
        style: {
          flex: w.toFixed(4),
          background: REGIME_COLORS[seg.label] || REGIME_COLORS.unknown,
        },
        title: `${REGIME_LABELS[seg.label] || seg.label}\n${new Date(seg.start * 1000).toISOString()}\n→ ${new Date(seg.end * 1000).toISOString()}`,
      }, w > 0.06 ? (REGIME_LABELS[seg.label] || seg.label) : "");
      els.regime.appendChild(span);
    });
  }

  // ---- Sidebar -----------------------------------------------------------

  function fmt(x, digits = 2) {
    if (x == null || (typeof x === "number" && (isNaN(x) || !isFinite(x)))) return "—";
    return Number(x).toFixed(digits);
  }
  function fmtPct(x) {
    if (x == null || isNaN(x)) return "—";
    return (Number(x) * 100).toFixed(2) + "%";
  }
  function fmtPrice(x) {
    if (x == null) return "—";
    if (x >= 1000) return Number(x).toLocaleString(undefined, { maximumFractionDigits: 2 });
    if (x >= 1) return Number(x).toFixed(4);
    return Number(x).toFixed(6);
  }
  function fmtMoney(x, signed = false) {
    if (x == null || isNaN(x)) return "—";
    const v = Number(x);
    const sign = signed && v > 0 ? "+" : "";
    return `${sign}$${v.toFixed(2)}`;
  }

  function setBar(bar, fraction, opts = {}) {
    if (!bar) return;
    const f = Math.max(0, Math.min(1, Number(fraction) || 0));
    bar.style.width = `${(f * 100).toFixed(1)}%`;
  }

  function setBipolarBar(bar, value, opts = {}) {
    // value in [-1, 1] — render from the zero tick outward
    if (!bar) return;
    const v = Math.max(-1, Math.min(1, Number(value) || 0));
    if (v >= 0) {
      bar.style.left = "50%";
      bar.style.width = `${(v * 50).toFixed(1)}%`;
      bar.style.background = "linear-gradient(90deg, rgba(34,197,94,0.4), rgba(34,197,94,1.0))";
    } else {
      bar.style.left = `${(50 + v * 50).toFixed(1)}%`;
      bar.style.width = `${(-v * 50).toFixed(1)}%`;
      bar.style.background = "linear-gradient(90deg, rgba(239,68,68,1.0), rgba(239,68,68,0.4))";
    }
  }

  function applyState(state) {
    if (!state || state.error) return;
    const s = els.state;
    s.regime.textContent = state.regime
      ? `${REGIME_LABELS[state.regime] || state.regime} (${fmtPct(state.regime_confidence)})`
      : "—";
    setBar(els.meters.regime, state.regime_confidence);

    s.sentiment.textContent = state.sentiment_score == null
      ? "—"
      : `${fmt(state.sentiment_score, 2)} @ ${fmtPct(state.sentiment_confidence)}`;
    setBipolarBar(els.meters.sentiment, state.sentiment_score);

    if (state.meta_signal == null) {
      s.meta.textContent = "—";
    } else {
      const ms = Number(state.meta_signal);
      const arrow = ms > 0 ? "↑" : (ms < 0 ? "↓" : "·");
      s.meta.textContent = `${arrow} ${ms} @ ${fmtPct(state.meta_confidence)}`;
    }
    setBar(els.meters.meta, state.meta_confidence);

    s.tft.textContent = state.tft && state.tft.up != null
      ? `${fmt(state.tft.up, 2)} / ${fmt(state.tft.flat, 2)} / ${fmt(state.tft.down, 2)}`
      : "—";
    s.tftConf.textContent = state.tft ? fmtPct(state.tft.confidence) : "—";
    setBar(els.meters.tft, state.tft && state.tft.confidence);

    s.netflow.textContent = state.onchain ? fmt(state.onchain.netflow_z) : "—";
    s.mvrv.textContent    = state.onchain ? fmt(state.onchain.mvrv) : "—";
    s.whale.textContent   = state.onchain ? fmt(state.onchain.whale_count_1h, 0) : "—";

    const c = state.champion || {};
    s.gen.textContent          = c.generation == null ? "—" : c.generation;
    s.champ.textContent        = c.champion_id || "—";
    s.champFitness.textContent = c.champion_fitness == null ? "—" : fmt(c.champion_fitness, 3);
    s.runner.textContent       = c.runner_up_id || "—";

    s.dailyPnl.textContent = state.daily_pnl == null ? "—" : fmtMoney(state.daily_pnl, true);
    s.dailyPnl.className   = state.daily_pnl > 0 ? "profit-pos"
                            : state.daily_pnl < 0 ? "profit-neg" : "";

    // Positions
    clear(els.positions);
    const positions = state.positions || [];
    if (!positions.length) {
      els.positions.appendChild(el("div", { className: "muted-row" }, "none"));
    } else {
      positions.forEach((p) => {
        const profit = Number(p.current_profit || 0);
        const profitTxt = profit ? `${(profit * 100).toFixed(2)}%` : "—";
        const profitCls = profit > 0 ? "profit-pos" : profit < 0 ? "profit-neg" : "";
        els.positions.appendChild(el("div", { className: "position-row" },
          el("span", { className: "pair" }, p.pair || "?"),
          el("span", {}, fmtPrice(p.open_rate)),
          el("span", { className: profitCls }, profitTxt),
        ));
      });
    }

    // P&L history (last 7 days)
    clear(els.pnlHist);
    const hist = state.daily_pnl_history || {};
    const days = Object.keys(hist).sort().slice(-7);
    days.forEach((d) => {
      const v = Number(hist[d] || 0);
      const cell = el("div", {
        className: "pnl-cell",
        style: {
          borderColor: v > 0 ? "rgba(34,197,94,0.5)"
                      : v < 0 ? "rgba(239,68,68,0.5)" : "var(--line)",
        },
      },
        el("span", { className: "day" }, d.slice(5)),
        fmtMoney(v, true),
      );
      els.pnlHist.appendChild(cell);
    });

    // Recent trades
    clear(els.recent);
    const trades = state.recent_trades || [];
    if (!trades.length) {
      els.recent.appendChild(el("div", { className: "muted-row" }, "none"));
    } else {
      trades.forEach((t) => {
        const pnl = Number(t.pnl || 0);
        const pnlCls = pnl > 0 ? "profit-pos" : pnl < 0 ? "profit-neg" : "";
        const open  = (t.opened_at || "").slice(11, 19);
        const close = (t.closed_at || "").slice(11, 19);
        els.recent.appendChild(el("div", { className: "row" },
          el("span", { className: "pair" }, t.pair || "?"),
          el("span", { className: "when" }, `${open}${close ? ` → ${close}` : ""}`),
          el("span", { className: `pnl ${pnlCls}` }, fmtMoney(pnl, true)),
          el("span", { className: "reason" }, t.exit_reason || (t.closed_at ? "" : "open")),
        ));
      });
    }
  }

  // ---- WebSocket ---------------------------------------------------------

  let ws = null;
  let wsReconnect = 0;
  function setWsIndicator(state) {
    const node = els.wsIndicator;
    if (state === "on") {
      node.textContent = "◉ live";
      node.classList.remove("ws-off");
      node.classList.add("ws-on");
    } else {
      node.textContent = "◉ offline";
      node.classList.remove("ws-on");
      node.classList.add("ws-off");
    }
  }

  function connectWs() {
    if (ws) try { ws.close(); } catch (_) {}
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${window.location.host}/ws`;
    try { ws = new WebSocket(url); } catch (_) { return scheduleReconnect(); }
    ws.onopen = () => { wsReconnect = 0; setWsIndicator("on"); };
    ws.onmessage = (e) => {
      try {
        const payload = JSON.parse(e.data);
        applyState(payload);
      } catch (_) { /* ignore */ }
    };
    ws.onerror = () => setWsIndicator("off");
    ws.onclose = () => { setWsIndicator("off"); scheduleReconnect(); };
  }
  function scheduleReconnect() {
    wsReconnect = Math.min(wsReconnect + 1, 6);
    const delay = Math.min(30_000, 1500 * (2 ** (wsReconnect - 1)));
    setTimeout(connectWs, delay);
  }

  // ---- Bootstrap ---------------------------------------------------------

  function currentPair() { return els.pair.value; }
  function currentTimeframe() { return els.tf.value; }

  // Support deep-linking from /ops: e.g. http://host:8081/?pair=SOFI&tf=5m
  // Selects the matching dropdown option on first paint if the option exists.
  (function applyUrlParams() {
    const params = new URLSearchParams(window.location.search);
    const wantPair = params.get("pair");
    const wantTf = params.get("tf");
    if (wantPair && [...els.pair.options].some((o) => o.value === wantPair)) {
      els.pair.value = wantPair;
    }
    if (wantTf && [...els.tf.options].some((o) => o.value === wantTf)) {
      els.tf.value = wantTf;
    }
  })();

  els.pair.addEventListener("change", () => loadPair(currentPair(), currentTimeframe()));
  els.tf.addEventListener("change",   () => loadPair(currentPair(), currentTimeframe()));

  function applyMode(m) {
    const node = els.modeBadge;
    if (!node || !m) return;
    node.classList.remove("mode-paper", "mode-live", "mode-paused");
    if (m.mode === "live") {
      node.classList.add("mode-live");
      node.textContent = "🔴 LIVE";
    } else if (m.mode === "paused") {
      node.classList.add("mode-paused");
      node.textContent = "⏸ PAUSED";
    } else {
      node.classList.add("mode-paper");
      node.textContent = "🧪 PAPER";
    }
    node.title = `state=${m.state} dry_run=${m.dry_run}`;
  }

  // ── Stocks sidebar card (Alpaca + wheel + shark) ────────────────────
  function fmtStocksUsd(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return "—";
    return "$" + Number(n).toLocaleString("en-US", { maximumFractionDigits: 0 });
  }
  function fmtStocksUsdSigned(n) {
    if (n === null || n === undefined) return "—";
    const s = (n >= 0 ? "+" : "−") + "$" + Math.abs(Number(n)).toFixed(0);
    return s;
  }
  function fmtStocksAge(seconds) {
    if (seconds === null || seconds === undefined) return "no data";
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
  }
  function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }
  function applyStocks(env) {
    if (!env || !env.data) return;
    const a = env.data.alpaca || {};
    const w = env.data.wheel || {};
    const s = env.data.shark || {};
    setText("stocks-cash", fmtStocksUsd(a.cash));
    setText("stocks-bp",   fmtStocksUsd(a.buying_power));
    setText("stocks-pv",   fmtStocksUsd(a.portfolio_value));
    setText("stocks-wheel-pnl", fmtStocksUsdSigned(w.cumulative_pnl_usd ?? 0));
    setText("stocks-wheel-pos", String((w.open_positions || []).length));
    setText("stocks-shark-week", `${s.weekly_trade_count ?? 0} / 3`);
    setText("stocks-shark-open", `${(s.open_trades || []).length} / 6`);
    const cbEl = document.getElementById("stocks-shark-cb");
    if (cbEl) {
      cbEl.textContent = s.circuit_breaker ? "TRIPPED" : "clear";
      cbEl.style.color = s.circuit_breaker ? "var(--down)" : "var(--up)";
    }
    setText("stocks-snapshot-age", fmtStocksAge(a.age_seconds));
    const pillEl = document.getElementById("stocks-mode-pill");
    if (pillEl) {
      const live = a.paper === false;
      pillEl.classList.toggle("mode-live",  live);
      pillEl.classList.toggle("mode-paper", !live);
      pillEl.textContent = live ? "LIVE" : "PAPER";
    }
  }
  const refreshStocks = () => getJson("/api/ops/stocks").then(applyStocks).catch(() => {});

  // ── Live trades hero strip (top of every page) ─────────────────────
  function pill(kind, label, detail, pnlDir) {
    const node = el("div", { className: "lt-pill" });
    node.dataset.kind = kind || "";
    node.dataset.pnl = pnlDir || "";
    node.appendChild(el("span", { className: "lt-dot" }));
    node.appendChild(el("span", { className: "lt-label" }, label));
    node.appendChild(el("span", { className: "lt-detail" }, detail));
    return node;
  }
  function renderLiveTrades(env) {
    const host = document.getElementById("lt-tracks");
    const counter = document.getElementById("lt-counter");
    if (!host || !env || !env.data) return;
    const trades = env.data.trades || [];
    const summary = env.data.summary || {};
    if (counter) {
      counter.textContent = `${summary.crypto_active ?? 0} crypto · ${summary.wheel_active ?? 0} wheel · ${summary.alpaca_paper === false ? "LIVE" : "PAPER"}`;
    }
    clear(host);
    if (!trades.length) {
      host.appendChild(el("div", { className: "lt-empty" },
        "— no active trades right now — bot is running, gates are blocking new entries —"));
      return;
    }
    for (const t of trades) {
      const pnlPct = t.pnl_pct;
      const pnlDir = pnlPct == null ? "" : pnlPct > 0 ? "up" : pnlPct < 0 ? "down" : "";
      let detail;
      if (t.kind === "wheel") {
        const credit = t.pnl_usd != null ? `$${Number(t.pnl_usd).toFixed(0)} credit` : "";
        const subkind = t.subkind === "short_put" ? "CSP"
          : t.subkind === "short_call" ? "CC"
          : t.subkind === "long_shares" ? "shares"
          : (t.subkind || "");
        detail = `${subkind} $${Number(t.entry || 0).toFixed(2)} · ${credit} · ${t.extra || ""}`;
      } else {
        const dur = t.duration_s ? `${Math.floor(t.duration_s / 60)}m` : "—";
        const pnlStr = pnlPct == null ? "—" : `${pnlPct >= 0 ? "+" : "−"}${Math.abs(pnlPct).toFixed(2)}%`;
        const pnlUsdStr = t.pnl_usd != null ? `$${Number(t.pnl_usd).toFixed(2)}` : "";
        detail = `${t.subkind || ""} @${t.entry == null ? "?" : t.entry} · ${pnlStr} ${pnlUsdStr} · held ${dur}`;
      }
      host.appendChild(pill(t.kind, t.label || "?", detail, pnlDir));
    }
  }
  const refreshLiveTrades = () => getJson("/api/ops/live_trades").then(renderLiveTrades).catch(() => {});

  // ── Stocks regime card (SPY 50/200) ───────────────────────────────
  const STOCK_REGIME_LABELS_APP = {
    trending_up:     "↗ trending up",
    trending_down:   "↘ trending down",
    mean_reverting:  "↔ mean-reverting",
    high_volatility: "⚡ high vol",
  };
  const STOCK_REGIME_COLORS_APP = {
    trending_up:     "#3fb950",
    trending_down:   "#f85149",
    mean_reverting:  "#9ab8ff",
    high_volatility: "#f4b942",
  };
  function applyStockRegime(env) {
    if (!env || !env.data) return;
    const d = env.data;
    const labelEl = document.getElementById("stock-regime-label");
    if (labelEl && d.current) {
      labelEl.textContent = STOCK_REGIME_LABELS_APP[d.current] || d.current;
      labelEl.style.color = STOCK_REGIME_COLORS_APP[d.current] || "var(--text-primary)";
    }
    const setKv = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
    setKv("stock-regime-spot",  d.spot != null ? "$" + Number(d.spot).toFixed(2) : "—");
    setKv("stock-regime-ma50",  d.ma_50 != null ? "$" + Number(d.ma_50).toFixed(2) : "—");
    setKv("stock-regime-ma200", d.ma_200 != null ? "$" + Number(d.ma_200).toFixed(2) : "—");
    const r5 = d.return_5d_pct;
    setKv("stock-regime-r5",   r5 == null ? "—" : `${r5 >= 0 ? "+" : ""}${r5.toFixed(2)}%`);
    setKv("stock-regime-vol",  d.realized_vol_21d_pct != null ? `${d.realized_vol_21d_pct.toFixed(1)}%` : "—");
    setKv("stock-regime-conf", d.probability != null ? `${(d.probability * 100).toFixed(0)}%` : "—");
  }
  const refreshStockRegime = () => getJson("/api/ops/stock_regime").then(applyStockRegime).catch(() => {});

  // First load — hit /api/state right away so the sidebar has data even if
  // the WS handshake takes a moment.
  getJson("/api/state").then(applyState).catch(() => {});
  getJson("/api/mode").then(applyMode).catch(() => {});
  refreshStocks();
  refreshLiveTrades();
  refreshStockRegime();
  setInterval(() => getJson("/api/mode").then(applyMode).catch(() => {}), 60_000);
  setInterval(refreshStocks, 30_000);
  setInterval(refreshLiveTrades, 5_000);
  setInterval(refreshStockRegime, 60_000);
  loadPair(currentPair(), currentTimeframe());
  connectWs();
})();
