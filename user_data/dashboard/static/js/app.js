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

  async function loadPair(pair, timeframe) {
    els.title.textContent = `${pair} — ${timeframe}`;
    clear(els.meta);
    els.meta.appendChild(document.createTextNode("loading…"));
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

  function applyState(state) {
    if (!state || state.error) return;
    const s = els.state;
    s.regime.textContent = state.regime
      ? `${REGIME_LABELS[state.regime] || state.regime} (${fmtPct(state.regime_confidence)})`
      : "—";
    s.sentiment.textContent = state.sentiment_score == null
      ? "—"
      : `${fmt(state.sentiment_score, 2)} @ ${fmtPct(state.sentiment_confidence)}`;
    if (state.meta_signal == null) {
      s.meta.textContent = "—";
    } else {
      const ms = Number(state.meta_signal);
      const arrow = ms > 0 ? "↑" : (ms < 0 ? "↓" : "·");
      s.meta.textContent = `${arrow} ${ms} @ ${fmtPct(state.meta_confidence)}`;
    }
    s.tft.textContent = state.tft && state.tft.up != null
      ? `${fmt(state.tft.up, 2)} / ${fmt(state.tft.flat, 2)} / ${fmt(state.tft.down, 2)}`
      : "—";
    s.tftConf.textContent = state.tft ? fmtPct(state.tft.confidence) : "—";

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

  els.pair.addEventListener("change", () => loadPair(currentPair(), currentTimeframe()));
  els.tf.addEventListener("change",   () => loadPair(currentPair(), currentTimeframe()));

  // First load — hit /api/state right away so the sidebar has data even if
  // the WS handshake takes a moment.
  getJson("/api/state").then(applyState).catch(() => {});
  loadPair(currentPair(), currentTimeframe());
  connectWs();
})();
