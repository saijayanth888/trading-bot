/* qc_react.js — React components ported from Claude Code Design prototype.
   No JSX. No Babel. Uses React.createElement directly via the global React.
   Loaded after react/react-dom UMD on /ops and /dashboard.

   Source of truth: /tmp/qtb-handoff/quanta-trading-bot/project/components.jsx
   Pixel-exact behavior preserved for the three non-negotiables:
     - CandleChart wheel-zoom around cursor, drag-pan with bounds,
       double-click reset, hover crosshair + OHLC tag, top-right legend.
     - KillSwitch 1500ms hold-to-confirm with pointermove cancel.
     - NumberRoll per-digit overflow:hidden 1em cells (no leak),
       600ms green/red flash on change.
*/
(function () {
  "use strict";
  const React = window.React;
  const ReactDOM = window.ReactDOM;
  const { useState, useEffect, useRef, useMemo, useCallback } = React;
  const h = React.createElement;
  const F = React.Fragment;

  // ─────────────── helpers ───────────────
  function cls(...xs) { return xs.filter(Boolean).join(" "); }

  // Compatibility shim: the prototype reads window.QuantaData for the clock
  // formatter and "Ns ago" formatter (Topbar/LiveTicker only). The integration
  // agent may not have wired QuantaData; fall back to local impls so the
  // components don't crash when QuantaData is absent.
  function fmtClock() {
    // Operator's host clock is ET — show ET, not UTC, so dashboard time
    // matches the corner-of-screen system clock. 12-hour AM/PM mirrors
    // legacy /ops topbar (commit ad266b8).
    const D = window.QuantaData;
    if (D && typeof D.fmtClockET === "function") return D.fmtClockET();
    if (D && typeof D.fmtClock === "function") return D.fmtClock();
    try {
      const parts = new Intl.DateTimeFormat("en-US", {
        timeZone: "America/New_York", hour12: true,
        hour: "2-digit", minute: "2-digit", second: "2-digit",
      }).formatToParts(new Date());
      const get = (t) => (parts.find((p) => p.type === t) || {}).value || "";
      return get("hour") + ":" + get("minute") + ":" + get("second") + " " + get("dayPeriod") + " ET";
    } catch {
      return new Date().toLocaleTimeString();
    }
  }
  function fmtAgoSecs(secs) {
    const D = window.QuantaData;
    if (D && typeof D.fmtAgo === "function") return D.fmtAgo(secs);
    if (secs == null) return "—";
    const s = Math.max(0, Math.floor(secs));
    if (s < 5) return "just now";
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    return Math.floor(s / 3600) + "h ago";
  }

  // ─────────────── NumberRoll ───────────────
  // Per-digit cells with overflow:hidden, 1em wide, line-height:1.
  // Track translates from 0em (digit 0) to -9em (digit 9), max -9em.
  // 600ms green/red flash on value change.
  function Digit({ ch, flash }) {
    const isDigit = /[0-9]/.test(ch);
    if (!isDigit) {
      return h(
        "span",
        { className: "numroll-cell wide" },
        h("span", null, ch)
      );
    }
    const n = parseInt(ch, 10);
    const flashCls = flash === "up" ? "flash-up" : flash === "down" ? "flash-down" : "";
    return h(
      "span",
      { className: cls("numroll-cell", flashCls) },
      h(
        "span",
        { className: "numroll-track", style: { transform: "translateY(-" + n + "em)" } },
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9].map((d) => h("span", { key: d }, String(d)))
      )
    );
  }

  function NumberRoll({ value, decimals = 2, prefix = "", suffix = "", className = "" }) {
    const str = useMemo(() => {
      if (value == null || isNaN(value)) return "—";
      return Math.abs(value).toLocaleString("en-US", {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      });
    }, [value, decimals]);
    const prev = useRef(value);
    const [flash, setFlash] = useState(null);
    useEffect(() => {
      if (prev.current != null && value != null && value !== prev.current) {
        setFlash(value > prev.current ? "up" : "down");
        const t = setTimeout(() => setFlash(null), 600);
        prev.current = value;
        return () => clearTimeout(t);
      }
      prev.current = value;
    }, [value]);

    const sign = value < 0 ? "−" : "";
    return h(
      "span",
      { className: cls("numroll", "num", className) },
      h("span", null, prefix, sign),
      str.split("").map((ch, i) => h(Digit, { key: i, ch: ch, flash: flash })),
      h("span", null, suffix)
    );
  }

  // ─────────────── Sparkline ───────────────
  function Sparkline({ data, color = "var(--accent)", fill = true, height = 32, animate = true }) {
    const ref = useRef(null);
    useEffect(() => {
      const cv = ref.current;
      if (!cv || !data || !data.length) return;
      const dpr = window.devicePixelRatio || 1;
      const w = cv.clientWidth, hH = cv.clientHeight;
      cv.width = w * dpr; cv.height = hH * dpr;
      const ctx = cv.getContext("2d"); ctx.scale(dpr, dpr);
      const mn = Math.min.apply(null, data), mx = Math.max.apply(null, data);
      const rng = (mx - mn) || 1;
      const px = (i) => (i / (data.length - 1)) * (w - 2) + 1;
      const py = (v) => hH - 2 - ((v - mn) / rng) * (hH - 4);
      let progress = animate ? 0 : 1;
      const cssVar = color.replace("var(", "").replace(")", "").trim();
      const _color = getComputedStyle(document.documentElement).getPropertyValue(cssVar) || color;

      const draw = () => {
        ctx.clearRect(0, 0, w, hH);
        const end = Math.max(1, Math.floor((data.length - 1) * progress));
        ctx.lineWidth = 1.4;
        ctx.strokeStyle = _color.trim() || color;
        ctx.beginPath();
        for (let i = 0; i <= end; i++) {
          const x = px(i), y = py(data[i]);
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.stroke();
        if (fill) {
          ctx.lineTo(px(end), hH); ctx.lineTo(px(0), hH); ctx.closePath();
          const g = ctx.createLinearGradient(0, 0, 0, hH);
          g.addColorStop(0, (_color.trim() || color) + "44");
          g.addColorStop(1, (_color.trim() || color) + "00");
          ctx.fillStyle = g;
          ctx.fill();
        }
        ctx.fillStyle = _color.trim() || color;
        const lx = px(end), ly = py(data[end]);
        ctx.beginPath(); ctx.arc(lx, ly, 1.6, 0, Math.PI * 2); ctx.fill();
      };

      if (!animate) { draw(); return; }
      let raf;
      const start = performance.now();
      const tick = (now) => {
        progress = Math.min(1, (now - start) / 500);
        draw();
        if (progress < 1) raf = requestAnimationFrame(tick);
      };
      raf = requestAnimationFrame(tick);
      return () => cancelAnimationFrame(raf);
    }, [data, color, fill, animate]);
    return h("canvas", { ref: ref, className: "spark", style: { height: height } });
  }

  // ─────────────── IndicatorSubchart ───────────────
  // Compact line chart for RSI / MACD / similar indicators. Takes
  // `data` as [{time, value}, ...]. Optionally renders a second series
  // (for MACD signal) and a histogram (for MACD hist). Horizontal
  // reference lines via `refLines` ([{value, color}]).
  //
  // Designed to sit BELOW the main CandleChart and share its time axis
  // (caller provides the same data range). Height is fixed at 110px.
  function IndicatorSubchart({ data, signal, hist, refLines, label, color = "var(--accent)", height = 110 }) {
    const ref = useRef(null);
    const wrapRef = useRef(null);
    useEffect(() => {
      const cv = ref.current, wrap = wrapRef.current;
      if (!cv || !wrap) return;
      const dpr = window.devicePixelRatio || 1;
      const cs = getComputedStyle(document.documentElement);
      const _resolve = (c) => {
        if (typeof c !== "string") return c;
        if (!c.startsWith("var(")) return c;
        const v = c.slice(4, -1).trim();
        return (cs.getPropertyValue(v) || c).trim();
      };
      const cLine = _resolve(color) || "#7c5cff";
      const cFg4 = (cs.getPropertyValue("--fg-3").trim() || "#9a9aa6");
      const cUp = (cs.getPropertyValue("--up").trim() || "#22c55e");
      const cDn = (cs.getPropertyValue("--down").trim() || "#ef4444");

      const resize = () => {
        const w = wrap.clientWidth, hH = wrap.clientHeight;
        cv.width = w * dpr; cv.height = hH * dpr;
        cv.style.width = w + "px"; cv.style.height = hH + "px";
        draw();
      };
      const draw = () => {
        const ctx = cv.getContext("2d");
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.scale(dpr, dpr);
        const w = wrap.clientWidth, hH = wrap.clientHeight;
        ctx.clearRect(0, 0, w, hH);
        if (!data || !data.length) {
          ctx.fillStyle = cFg4 + "88";
          ctx.font = "10px Geist Mono, monospace";
          ctx.fillText("(no data)", 8, hH / 2);
          return;
        }
        const padR = 70, padL = 8, padT = 14, padB = 6;
        const chartW = w - padR - padL, chartH = hH - padT - padB;
        // Compute domain across data + signal + hist
        let mn = Infinity, mx = -Infinity;
        const consider = (arr) => arr && arr.forEach(p => {
          const v = (p && typeof p.value === "number") ? p.value : null;
          if (v == null) return;
          if (v < mn) mn = v;
          if (v > mx) mx = v;
        });
        consider(data); consider(signal); consider(hist);
        if (refLines) refLines.forEach(r => {
          if (r.value < mn) mn = r.value;
          if (r.value > mx) mx = r.value;
        });
        if (mn === Infinity) return;
        if (mn === mx) { mn -= 1; mx += 1; }
        const rng = mx - mn;
        const N = data.length;
        const px = (i) => padL + (i + 0.5) / N * chartW;
        const py = (v) => padT + (1 - (v - mn) / rng) * chartH;

        // Reference lines
        ctx.strokeStyle = "rgba(255,255,255,.07)"; ctx.lineWidth = 1; ctx.setLineDash([2, 3]);
        (refLines || []).forEach(r => {
          ctx.strokeStyle = r.color || "rgba(255,255,255,.12)";
          ctx.beginPath();
          ctx.moveTo(padL, py(r.value));
          ctx.lineTo(padL + chartW, py(r.value));
          ctx.stroke();
          ctx.fillStyle = (r.color || cFg4) + "cc";
          ctx.font = "9px Geist Mono";
          ctx.textAlign = "left";
          ctx.fillText(String(r.value), padL + chartW + 4, py(r.value) + 3);
        });
        ctx.setLineDash([]);

        // Histogram (MACD)
        if (hist && hist.length) {
          const barW = Math.max(1, (chartW / N) * 0.7);
          const zeroY = py(0);
          hist.forEach((p, i) => {
            if (p == null || typeof p.value !== "number") return;
            const x = px(i);
            const y = py(p.value);
            ctx.fillStyle = p.value >= 0 ? cUp + "88" : cDn + "88";
            ctx.fillRect(x - barW / 2, Math.min(y, zeroY), barW, Math.abs(y - zeroY));
          });
        }
        // Main line
        ctx.strokeStyle = cLine; ctx.lineWidth = 1.4;
        ctx.beginPath();
        let started = false;
        data.forEach((p, i) => {
          if (p == null || typeof p.value !== "number") return;
          const x = px(i), y = py(p.value);
          if (!started) { ctx.moveTo(x, y); started = true; }
          else ctx.lineTo(x, y);
        });
        ctx.stroke();
        // Signal line (MACD signal)
        if (signal && signal.length) {
          ctx.strokeStyle = "rgba(245,158,11,0.9)"; ctx.lineWidth = 1;
          ctx.beginPath();
          let s = false;
          signal.forEach((p, i) => {
            if (p == null || typeof p.value !== "number") return;
            const x = px(i), y = py(p.value);
            if (!s) { ctx.moveTo(x, y); s = true; }
            else ctx.lineTo(x, y);
          });
          ctx.stroke();
        }
        // Right-side last-value label
        const last = data[data.length - 1];
        if (last && typeof last.value === "number") {
          const lblY = py(last.value);
          ctx.fillStyle = cLine;
          ctx.fillRect(padL + chartW, lblY - 8, padR - 8, 16);
          ctx.fillStyle = "#0a0a0a";
          ctx.font = "10px Geist Mono";
          ctx.textAlign = "left";
          ctx.fillText(last.value.toFixed(2), padL + chartW + 6, lblY + 3);
        }
        // Top-left label
        if (label) {
          ctx.fillStyle = cFg4;
          ctx.font = "10px Geist Mono";
          ctx.textAlign = "left";
          ctx.fillText(label, padL + 4, padT - 2);
        }
      };
      resize();
      const ro = new ResizeObserver(resize); ro.observe(wrap);
      return () => ro.disconnect();
    }, [data, signal, hist, refLines, label, color]);
    return h("div", { ref: wrapRef, style: { position: "relative", width: "100%", height: height } },
      h("canvas", { ref: ref, style: { display: "block", width: "100%", height: "100%" } })
    );
  }

  // ─────────────── CandleChart ───────────────
  // Pixel-exact port:
  //   wheel-zoom around cursor (anchor = view.start + li, leftFrac = li/range)
  //   drag-pan with bar-0-to-latest bounds
  //   dblclick resets to full range
  //   crosshair + OHLC tag on hover
  //   "N bars · scroll = zoom · drag = pan · dbl-click = reset" legend top-right
  function CandleChart({ candles, markers = [], height = 460, showVolume = true, overlays = null }) {
    // overlays: {
    //   bb_upper: [{time, value}], bb_mid: [...], bb_lower: [...],
    //   ema20: [...], ema50: [...], vwap: [...]
    // }
    // Each is matched to candles by timestamp (lookup table built each draw).
    // Path-A close: legacy /charts had these overlays on the TradingView
    // chart; ported here so /dashboard_spa users see the same context.
    const ref = useRef(null);
    const wrapRef = useRef(null);
    const [hover, setHover] = useState(null);
    const viewRef = useRef({ start: 0, end: (candles || []).length });
    const dragRef = useRef(null);
    const drawRef = useRef(null);
    const candlesRef = useRef(candles);
    const [, force] = useState(0);
    const rerender = () => force((n) => n + 1);

    useEffect(() => {
      viewRef.current = { start: 0, end: (candles || []).length };
      candlesRef.current = candles;
      rerender();
    }, [candles]);

    // Redraw effect — recomputes layout/scales/renders whenever any of
    // {candles, markers, showVolume, overlays} change. Also installs the
    // latest draw closure into drawRef so the listener effect (attach-once)
    // can invoke it without re-attaching.
    useEffect(() => {
      const cv = ref.current, wrap = wrapRef.current;
      if (!cv || !wrap || !candles || !candles.length) return;
      candlesRef.current = candles;
      const dpr = window.devicePixelRatio || 1;
      const cs = getComputedStyle(document.documentElement);
      const cUp = cs.getPropertyValue("--up").trim();
      const cDn = cs.getPropertyValue("--down").trim();
      const cFg4 = cs.getPropertyValue("--fg-3").trim() || "#9a9aa6";
      const cAccent = cs.getPropertyValue("--accent").trim();

      const draw = (hi) => {
        if (hi === undefined) hi = null;
        const w = wrap.clientWidth, hH = wrap.clientHeight;
        const ctx = cv.getContext("2d");
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.scale(dpr, dpr); ctx.clearRect(0, 0, w, hH);

        const padR = 70, padB = showVolume ? 80 : 36, padT = 16, padL = 8;
        const chartW = w - padR - padL, chartH = hH - padB - padT;
        const volH = showVolume ? 60 : 0;

        const view = viewRef.current;
        const s = Math.max(0, Math.floor(view.start));
        const e = Math.min(candles.length, Math.ceil(view.end));
        const slice = candles.slice(s, e);
        const N = slice.length || 1;

        const lows = slice.map((c) => c.l);
        const highs = slice.map((c) => c.h);
        const mn = Math.min.apply(null, lows);
        const mx = Math.max.apply(null, highs);
        const rng = (mx - mn) || 1;
        const px = (i) => padL + (i + 0.5) / N * chartW;
        const py = (v) => padT + (1 - (v - mn) / rng) * chartH;

        // grid
        ctx.strokeStyle = "rgba(255,255,255,.08)"; ctx.lineWidth = 1;
        for (let g = 0; g <= 5; g++) {
          const y = padT + (g / 5) * chartH;
          ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + chartW, y); ctx.stroke();
        }
        // y-axis (right) price labels
        ctx.fillStyle = "rgba(232,232,240,.85)";
        ctx.font = "11px Geist Mono, monospace";
        ctx.textAlign = "left";
        for (let g = 0; g <= 5; g++) {
          const v = mx - (g / 5) * rng;
          ctx.fillText(v.toFixed(v < 10 ? 4 : 2), padL + chartW + 6, padT + (g / 5) * chartH + 4);
        }
        // x-axis time labels
        ctx.fillStyle = "rgba(232,232,240,.7)";
        ctx.font = "10px Geist Mono, monospace";
        ctx.textAlign = "center";
        const xTicks = Math.min(8, N);
        for (let g = 0; g <= xTicks; g++) {
          const i = Math.floor((g / xTicks) * (N - 1));
          const x = padL + ((i + 0.5) / N) * chartW;
          const c = slice[i];
          if (c && c.t) ctx.fillText(c.t, x, hH - 6);
        }

        // candles
        const bodyW = Math.max(1, (chartW / N) * 0.7);
        slice.forEach((c, i) => {
          const x = px(i);
          const up = c.c >= c.o;
          ctx.strokeStyle = up ? cUp : cDn;
          ctx.fillStyle = up ? cUp : cDn;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(x, py(c.h)); ctx.lineTo(x, py(c.l)); ctx.stroke();
          const yo = py(c.o), yc = py(c.c);
          const top = Math.min(yo, yc), bh = Math.max(1, Math.abs(yc - yo));
          ctx.fillRect(x - bodyW / 2, top, bodyW, bh);
        });

        // ── Overlays (BB / EMA20 / EMA50 / VWAP) ──
        // Series come from /api/candles indicators as [{time, value}].
        // Match each indicator point to a candle by timestamp.
        if (overlays && slice.length > 0) {
          // Build a candle-timestamp → slice-index lookup
          const tsToIdx = new Map();
          slice.forEach((c, i) => { if (c && c.ts) tsToIdx.set(c.ts, i); });
          const drawLine = (series, color, width) => {
            if (!Array.isArray(series) || series.length === 0) return;
            ctx.strokeStyle = color; ctx.lineWidth = width; ctx.lineCap = "round";
            ctx.beginPath();
            let started = false;
            series.forEach((p) => {
              if (!p || typeof p.value !== "number" || typeof p.time !== "number") return;
              const idx = tsToIdx.get(p.time);
              if (idx == null) return;
              const x = px(idx), y = py(p.value);
              if (!started) { ctx.moveTo(x, y); started = true; }
              else ctx.lineTo(x, y);
            });
            ctx.stroke();
          };
          // Bollinger bands — translucent upper/lower + dashed mid
          drawLine(overlays.bb_upper, "rgba(79,141,247,0.7)", 1);
          drawLine(overlays.bb_lower, "rgba(79,141,247,0.7)", 1);
          if (overlays.bb_mid && overlays.bb_mid.length) {
            ctx.setLineDash([3, 3]);
            drawLine(overlays.bb_mid, "rgba(79,141,247,0.55)", 1);
            ctx.setLineDash([]);
          }
          // EMA20 (amber) + EMA50 (purple) + VWAP (cyan)
          drawLine(overlays.ema20, "#f59e0b", 1.4);
          drawLine(overlays.ema50, "rgba(147,51,234,0.85)", 1.4);
          drawLine(overlays.vwap, "rgba(34,211,238,0.85)", 1.2);
        }

        // volume
        if (showVolume) {
          const diffs = slice.map((c) => Math.abs(c.c - c.o));
          const vmx = Math.max.apply(null, diffs) || 1;
          slice.forEach((c, i) => {
            const x = px(i);
            const v = Math.abs(c.c - c.o);
            const bh = (v / vmx) * (volH - 10);
            ctx.fillStyle = (c.c >= c.o ? cUp : cDn) + "55";
            ctx.fillRect(x - bodyW / 2, hH - padB + 8 + (volH - 10 - bh), bodyW, bh);
          });
          ctx.strokeStyle = "rgba(255,255,255,.12)";
          ctx.beginPath();
          ctx.moveTo(padL, hH - padB + 4);
          ctx.lineTo(padL + chartW, hH - padB + 4);
          ctx.stroke();
          ctx.fillStyle = "rgba(232,232,240,.7)";
          ctx.font = "10px Geist Mono";
          ctx.textAlign = "left";
          ctx.fillText("VOL", padL + 4, hH - padB + 16);
        }

        // markers (translate global idx -> slice idx)
        markers.forEach((m) => {
          if (m.i < s || m.i >= e) return;
          const li = m.i - s;
          const x = px(li);
          const y = py(m.price);
          ctx.fillStyle = m.side === "BUY" ? cUp : cDn;
          ctx.beginPath();
          if (m.side === "BUY") {
            ctx.moveTo(x, y + 6); ctx.lineTo(x - 5, y + 14); ctx.lineTo(x + 5, y + 14);
          } else {
            ctx.moveTo(x, y - 6); ctx.lineTo(x - 5, y - 14); ctx.lineTo(x + 5, y - 14);
          }
          ctx.closePath(); ctx.fill();
          ctx.font = "9px Geist Mono";
          ctx.textAlign = "center";
          ctx.fillText(m.label, x, m.side === "BUY" ? y + 24 : y - 18);
        });

        // crosshair
        if (hi != null && hi >= 0 && hi < N) {
          const c = slice[hi];
          const x = px(hi);
          ctx.strokeStyle = cFg4; ctx.setLineDash([2, 3]);
          ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, hH - padB); ctx.stroke();
          ctx.beginPath();
          ctx.moveTo(padL, py(c.c));
          ctx.lineTo(padL + chartW, py(c.c));
          ctx.stroke();
          ctx.setLineDash([]);
          const lbl = c.c.toFixed(c.c < 10 ? 4 : 2);
          ctx.fillStyle = cAccent;
          ctx.fillRect(padL + chartW, py(c.c) - 8, padR - 8, 16);
          ctx.fillStyle = "#000";
          ctx.font = "10px Geist Mono";
          ctx.textAlign = "left";
          ctx.fillText(lbl, padL + chartW + 6, py(c.c) + 3);
        }
      };

      const sizeAndDraw = () => {
        const w = wrap.clientWidth, hH = wrap.clientHeight;
        cv.width = w * dpr; cv.height = hH * dpr;
        cv.style.width = w + "px"; cv.style.height = hH + "px";
        draw();
      };
      drawRef.current = draw;
      sizeAndDraw();
      return () => { drawRef.current = null; };
    }, [candles, markers, showVolume, overlays]);

    // Listener-attach effect — attaches mousemove / mouseleave / wheel /
    // mousedown / mouseup / dblclick / ResizeObserver ONCE on mount, cleans
    // up on unmount. Handlers read the latest candles via candlesRef and
    // invoke the latest draw via drawRef so listeners never need to be
    // re-attached when parent re-renders. Before this split, the listeners
    // re-attached on every render because `markers` was in deps and
    // dashboard_spa.js rebuilds the markers array each fetch tick.
    useEffect(() => {
      const cv = ref.current, wrap = wrapRef.current;
      if (!cv || !wrap) return;
      const dpr = window.devicePixelRatio || 1;

      const callDraw = (hi) => { if (drawRef.current) drawRef.current(hi); };
      const resize = () => {
        const w = wrap.clientWidth, hH = wrap.clientHeight;
        cv.width = w * dpr; cv.height = hH * dpr;
        cv.style.width = w + "px"; cv.style.height = hH + "px";
        callDraw();
      };
      const ro = new ResizeObserver(resize); ro.observe(wrap);

      const xToSliceIdx = (clientX) => {
        const r = cv.getBoundingClientRect();
        const x = clientX - r.left;
        const padL = 8, padR = 70;
        const w = r.width;
        const cW = w - padR - padL;
        const view = viewRef.current;
        const N = Math.max(1, view.end - view.start);
        const li = Math.max(0, Math.min(N - 1, Math.floor(((x - padL) / cW) * N)));
        return { li: li, x: x };
      };

      const onMove = (e) => {
        const candlesNow = candlesRef.current || [];
        if (dragRef.current) {
          const dx = e.clientX - dragRef.current.startX;
          const r = cv.getBoundingClientRect();
          const w = r.width - 78;
          const view0 = dragRef.current.view0;
          const range = view0.end - view0.start;
          const shift = -(dx / w) * range;
          let s = view0.start + shift, en = view0.end + shift;
          if (s < 0) { en -= s; s = 0; }
          if (en > candlesNow.length) { s -= (en - candlesNow.length); en = candlesNow.length; }
          viewRef.current = { start: Math.max(0, s), end: Math.min(candlesNow.length, en) };
          callDraw();
          return;
        }
        const r = xToSliceIdx(e.clientX);
        const view = viewRef.current;
        const globalIdx = Math.floor(view.start) + r.li;
        setHover({ i: globalIdx, c: candlesNow[globalIdx], li: r.li });
        callDraw(r.li);
      };
      const onLeave = () => { setHover(null); callDraw(); };
      const onWheel = (e) => {
        e.preventDefault();
        const candlesNow = candlesRef.current || [];
        const r = xToSliceIdx(e.clientX);
        const view = viewRef.current;
        const range = view.end - view.start;
        const factor = e.deltaY > 0 ? 1.18 : 1 / 1.18;
        let newRange = Math.max(8, Math.min(candlesNow.length, range * factor));
        const anchor = view.start + r.li;
        const leftFrac = r.li / Math.max(1, range);
        let s = anchor - newRange * leftFrac;
        let en = s + newRange;
        if (s < 0) { en -= s; s = 0; }
        if (en > candlesNow.length) { s -= (en - candlesNow.length); en = candlesNow.length; }
        viewRef.current = { start: Math.max(0, s), end: Math.min(candlesNow.length, en) };
        callDraw();
      };
      const onDown = (e) => {
        dragRef.current = { startX: e.clientX, view0: Object.assign({}, viewRef.current) };
        cv.style.cursor = "grabbing";
      };
      const onUp = () => { dragRef.current = null; cv.style.cursor = "crosshair"; };
      const onDbl = () => {
        const candlesNow = candlesRef.current || [];
        viewRef.current = { start: 0, end: candlesNow.length };
        callDraw();
      };

      cv.style.cursor = "crosshair";
      cv.addEventListener("mousemove", onMove);
      cv.addEventListener("mouseleave", onLeave);
      cv.addEventListener("wheel", onWheel, { passive: false });
      cv.addEventListener("mousedown", onDown);
      window.addEventListener("mouseup", onUp);
      cv.addEventListener("dblclick", onDbl);
      return () => {
        ro.disconnect();
        cv.removeEventListener("mousemove", onMove);
        cv.removeEventListener("mouseleave", onLeave);
        cv.removeEventListener("wheel", onWheel);
        cv.removeEventListener("mousedown", onDown);
        window.removeEventListener("mouseup", onUp);
        cv.removeEventListener("dblclick", onDbl);
      };
    }, []);

    const view = viewRef.current;
    const visible = Math.round(view.end - view.start);

    return h(
      "div",
      { ref: wrapRef, style: { position: "relative", width: "100%", height: height } },
      h("canvas", { ref: ref, style: { display: "block" } }),
      h(
        "div",
        {
          style: {
            position: "absolute", top: 8, right: 78,
            fontFamily: "var(--mono)", fontSize: "var(--t-2xs)",
            color: "var(--fg-3)", letterSpacing: ".06em",
            pointerEvents: "none", userSelect: "none",
          },
        },
        visible + " bars · scroll = zoom · drag = pan · dbl-click = reset"
      ),
      hover && hover.c
        ? h(
            "div",
            {
              style: {
                position: "absolute", top: 10, left: 10,
                fontFamily: "var(--mono)", fontSize: "var(--t-xs)",
                background: "var(--bg-overlay)", border: "1px solid var(--line-2)",
                padding: "6px 10px", borderRadius: 4, lineHeight: 1.6,
                pointerEvents: "none",
              },
            },
            h(
              "div",
              { style: { color: "var(--fg-3)" } },
              "BAR #" + hover.i + (hover.c.t ? " · " + hover.c.t : "")
            ),
            h("div", null, "O ", h("span", { className: "num" }, hover.c.o.toFixed(2))),
            h("div", null, "H ", h("span", { className: "num up" }, hover.c.h.toFixed(2))),
            h("div", null, "L ", h("span", { className: "num down" }, hover.c.l.toFixed(2))),
            h("div", null, "C ", h("span", { className: "num" }, hover.c.c.toFixed(2)))
          )
        : null
    );
  }

  // ─────────────── RegimeRibbon ───────────────
  function RegimeRibbon({ segments }) {
    return h(
      "div",
      { className: "regbar" },
      (segments || []).map((s, i) =>
        h("div", {
          key: i,
          className: "regbar-seg " + s.kind,
          style: { flex: s.weight },
        })
      )
    );
  }

  // ─────────────── StatusRow ───────────────
  function StatusRow({ status, name, sub, value, valueClass = "" }) {
    return h(
      "div",
      { className: "srow" },
      h("span", { className: cls("dot", status, status === "up" ? "pulse" : "") }),
      h("span", { className: "srow-name" }, name),
      h("span", { className: "srow-sub dim" }, sub),
      h("span", { className: cls("srow-val", valueClass) }, value)
    );
  }

  // ─────────────── GateBadge ───────────────
  function GateBadge({ state, label }) {
    const cssClass = ({ PASS: "pass", BLOCK: "block", WARN: "warn", NA: "na" })[state] || "na";
    return h("span", { className: "gate " + cssClass }, label || state);
  }

  // ─────────────── KillSwitch ───────────────
  // 1500 ms hold-to-confirm. raf-driven fill width. release before 100% -> cancel.
  // pointermove cancel matches prototype's "release on drag-off".
  function KillSwitch({ state, onArm, onKill, onResume }) {
    const [holding, setHolding] = useState(0);
    const raf = useRef(null);
    const start = useRef(0);
    const holdMs = 1500;

    const tick = (now) => {
      const p = Math.min(1, (now - start.current) / holdMs);
      setHolding(p * 100);
      if (p >= 1) {
        cancelAnimationFrame(raf.current);
        raf.current = null;
        if (onKill) onKill();
        setHolding(0);
      } else {
        raf.current = requestAnimationFrame(tick);
      }
    };
    const down = () => {
      if (state !== "armed") return;
      start.current = performance.now();
      raf.current = requestAnimationFrame(tick);
    };
    const up = () => {
      if (raf.current) cancelAnimationFrame(raf.current);
      raf.current = null;
      setHolding(0);
    };

    useEffect(() => {
      return () => {
        if (raf.current) cancelAnimationFrame(raf.current);
        raf.current = null;
      };
    }, []);

    if (state === "killed") {
      return h(
        "div",
        { className: "kill-wrap" },
        h("span", { className: "kill-label", style: { color: "var(--down)" } }, "● KILLED"),
        h("button", { className: "kill-btn", onClick: onResume }, "RESUME")
      );
    }
    if (state === "armed") {
      return h(
        "div",
        { className: "kill-wrap armed" },
        h("span", { className: "kill-label" }, "⚠ ARMED"),
        h(
          "button",
          {
            className: "kill-btn confirm kill-hold",
            onMouseDown: down,
            onMouseUp: up,
            onMouseLeave: up,
            onTouchStart: down,
            onTouchEnd: up,
          },
          h("span", { className: "kill-hold-fill", style: { width: holding + "%" } }),
          h("span", { className: "kill-hold-text" }, "HOLD 1.5s TO CONFIRM")
        ),
        h("button", { className: "kill-btn", onClick: onResume }, "CANCEL")
      );
    }
    return h(
      "div",
      { className: "kill-wrap" },
      h("span", { className: "kill-label" }, "KILL"),
      h("button", { className: "kill-btn", onClick: onArm }, "ARM")
    );
  }

  // ─────────────── NavIcon (private helper for Sidebar) ───────────────
  function NavIcon({ kind }) {
    const m = {
      ops: "M2 3h12v3H2zM2 8h12v3H2zM2 13h7v0",
      dashboard: "M2 12L5 8L8 10L13 4",
      agent: "M2 8h12M5 5l-3 3 3 3M11 11l3-3-3-3",
      risk: "M8 2L2 13h12L8 2zM8 6v3M8 11v.5",
      research: "M3 3h7v10H3zM12 4v9M4 5h5M4 8h5M4 11h3",
      evolution: "M2 13c2-3 4-3 6 0s4 3 6 0M2 8c2-3 4-3 6 0s4 3 6 0",
      llm: "M3 5h10v6H3zM6 11v2M10 11v2M5 13h6",
      config: "M8 2v3M8 11v3M2 8h3M11 8h3M3.5 3.5l2 2M10.5 10.5l2 2M3.5 12.5l2-2M10.5 5.5l2-2",
    };
    return h(
      "svg",
      {
        className: "nav-icon",
        viewBox: "0 0 16 16",
        fill: "none",
        stroke: "currentColor",
        strokeWidth: "1.4",
        strokeLinecap: "round",
        strokeLinejoin: "round",
      },
      h("path", { d: m[kind] || m.ops })
    );
  }

  // ─────────────── Topbar ───────────────
  // Note: tweaks (theme/density/accent) are NOT ported; only the props
  // (killState, setKillState, active, density) used by the prototype itself.
  function Topbar({ killState, setKillState, active, density, onRefreshIntervalChange, onRefreshNow }) {
    const [clock, setClock] = useState(fmtClock());
    // Real uptime — poll /api/ops/uptime every 30s for freqtrade's actual
    // start time. Earlier this was page-load time, which made the pill
    // read "0h 0m" every refresh — not useful.
    const [uptime, setUptime] = useState("—");
    const [equity, setEquity] = useState({ value: null, deltaPct: null });
    // Live pill state — previously these were hardcoded "PAPER · DRY-RUN" and
    // "FREQTRADE OK" strings that lied through any incident. mode comes from
    // /api/mode, ftUp from /api/ops/services.
    const [mode, setMode] = useState({ label: "—", dry: true, healthy: false });
    const [ftUp, setFtUp] = useState(null);   // null = unknown, true/false otherwise
    useEffect(() => {
      const t = setInterval(() => setClock(fmtClock()), 1000);
      const refresh = async () => {
        try {
          const r = await fetch("/api/ops/uptime", { cache: "no-store" });
          const j = await r.json().catch(() => ({}));
          const ft = (j && j.data && j.data.freqtrade) || {};
          if (typeof ft.uptime_s === "number") {
            const s = ft.uptime_s;
            const d = Math.floor(s / 86400);
            const h = Math.floor((s % 86400) / 3600);
            const m = Math.floor((s % 3600) / 60);
            setUptime(d > 0 ? `${d}d ${h}h ${m}m` : (h > 0 ? `${h}h ${m}m` : `${m}m`));
          } else {
            setUptime("—");
          }
        } catch (_) { /* ignore */ }
        try {
          const r = await fetch("/api/ops/combined_portfolio", { cache: "no-store" });
          const j = await r.json().catch(() => ({}));
          const d = (j && j.data) || {};
          const total = Number(d.total_equity);
          const dd = Number(d.combined_drawdown_pct);
          if (Number.isFinite(total)) {
            setEquity({ value: total, deltaPct: Number.isFinite(dd) ? -Math.abs(dd) : null });
          }
        } catch (_) { /* ignore */ }
        // /api/mode — { mode: "paper"|"live", dry_run: bool, state: "running"|"paused"|... }
        try {
          const r = await fetch("/api/mode", { cache: "no-store" });
          const j = await r.json().catch(() => ({}));
          const m = String(j && j.mode || "").toLowerCase();
          const dry = !!(j && j.dry_run);
          const st = String(j && j.state || "").toLowerCase();
          const healthy = ["running", "reload_config", "starting", "init"].includes(st);
          const label = (m === "paper" || dry)
            ? (dry ? "PAPER · DRY-RUN" : "PAPER")
            : "LIVE";
          setMode({ label, dry, healthy });
        } catch (_) { /* ignore */ }
        // /api/ops/services — { data: { freqtrade: { up: bool, ... }, ... } }
        try {
          const r = await fetch("/api/ops/services", { cache: "no-store" });
          const j = await r.json().catch(() => ({}));
          const ftSvc = (j && j.data && j.data.freqtrade) || {};
          setFtUp(typeof ftSvc.up === "boolean" ? ftSvc.up : null);
        } catch (_) { /* ignore */ }
      };
      refresh();
      const u = setInterval(refresh, 30000);
      return () => { clearInterval(t); clearInterval(u); };
    }, []);
    return h(
      "header",
      { className: "topbar" },
      h(
        "div",
        { className: "brand" },
        h("div", { className: "brand-mark" }, "Q"),
        h(
          "span",
          { className: "brand-text" },
          "QUANTA ",
          h("span", { className: "brand-version" }, "v2.6")
        )
      ),
      h(
        "div",
        { className: "tb-group" },
        // Mode pill — live data from /api/mode. PAPER → warn, LIVE → down
        // (intentionally loud: live trading is the high-risk state).
        h(
          "span",
          { className: "pill " + (mode.dry ? "warn" : "down") },
          h("span", { className: "dot " + (mode.dry ? "warn" : "down") + " pulse" }),
          " " + mode.label
        ),
        // freqtrade up/down — from /api/ops/services. null = unknown (don't
        // claim health while we still haven't heard back).
        h(
          "span",
          { className: "pill " + (ftUp === true ? "up" : ftUp === false ? "down" : "") },
          h("span", { className: "dot " + (ftUp === true ? "up pulse" : ftUp === false ? "down" : "dim") }),
          " FREQTRADE " + (ftUp === true ? "OK" : ftUp === false ? "DOWN" : "—")
        )
      ),
      h("div", { className: "tb-divider" }),
      h(
        "div",
        { className: "tb-group" },
        h(
          "span",
          { className: "dim2 mono", style: { fontSize: "var(--t-xs)", letterSpacing: ".08em" } },
          "BOT UP"
        ),
        h("span", { className: "num" }, uptime)
      ),
      h("div", { className: "tb-divider" }),
      h(
        "div",
        { className: "tb-group" },
        h(
          "span",
          { className: "dim2 mono", style: { fontSize: "var(--t-xs)", letterSpacing: ".08em" } },
          "EQUITY"
        ),
        equity.value != null
          ? h(NumberRoll, { value: equity.value, prefix: "$" })
          : h("span", { className: "num dim" }, "—"),
        equity.deltaPct != null
          ? h("span", { className: "pill " + (equity.deltaPct >= 0 ? "up" : "down") },
              (equity.deltaPct >= 0 ? "+" : "") + equity.deltaPct.toFixed(2) + "%")
          : null
      ),
      h("span", { className: "tb-spacer" }),
      // A/B fallback link back to legacy console — Path-B cutover plan:
      // SPA is preview-tier this week; operator can switch back any time.
      h(
        "div",
        { className: "tb-group" },
        h(
          "a",
          {
            href: active === "ops" ? "/ops" : "/",
            className: "pill",
            style: { height: 24, fontSize: 11, textDecoration: "none", color: "var(--fg-2)" },
            title: "Switch back to the legacy console",
          },
          h("span", { className: "dot dim" }),
          " Classic console"
        )
      ),
      h("div", { className: "tb-divider" }),
      h(
        "div",
        { className: "tb-group" },
        h(
          "span",
          { className: "mono dim", style: { fontSize: "var(--t-xs)" } },
          clock
        ),
        h(
          "select",
          {
            className: "select",
            "aria-label": "Refresh interval",
            defaultValue: "5",
            onChange: (e) => {
              if (typeof onRefreshIntervalChange !== "function") return;
              const raw = e.target.value;
              const ms = raw === "off" ? 0 : parseInt(raw, 10) * 1000;
              onRefreshIntervalChange(ms);
            },
          },
          h("option", { value: "5" }, "5s"),
          h("option", { value: "10" }, "10s"),
          h("option", { value: "30" }, "30s"),
          h("option", { value: "60" }, "1m"),
          h("option", { value: "off" }, "Off")
        ),
        h(
          "button",
          {
            className: "icon-btn",
            title: "Force refresh",
            "aria-label": "Refresh now",
            onClick: () => { if (typeof onRefreshNow === "function") onRefreshNow(); },
          },
          "↻"
        )
      ),
      h("div", { className: "tb-divider" }),
      h(KillSwitch, {
        state: killState,
        onArm: () => setKillState && setKillState("armed"),
        onKill: () => setKillState && setKillState("killed"),
        onResume: () => setKillState && setKillState("normal"),
      })
    );
  }

  // ─────────────── Sidebar ───────────────
  function Sidebar({ active }) {
    // Routes (see user_data/dashboard/app.py): /ops_spa and /dashboard_spa.
    // The hand-off template carried over Vite-style `index.html`/`dashboard.html`
    // hrefs that produce 404s under FastAPI's URL space — replaced with the
    // server's actual routes here. Section sub-items still anchor under
    // /ops_spa#<id> until the inline-section routes land.
    const items = [
      { sect: "MONITOR" },
      { id: "ops", label: "Ops console", key: "1", href: "/ops_spa" },
      { id: "dashboard", label: "Pair dashboard", key: "2", href: "/dashboard_spa" },
      { sect: "ANALYSIS" },
      { id: "agent", label: "Agent timeline", key: "3", href: "/ops_spa#agent" },
      { id: "risk", label: "Risk & gates", key: "4", href: "/ops_spa#risk" },
      { id: "research", label: "Research feed", key: "5", href: "/ops_spa#research" },
      { sect: "SYSTEM" },
      { id: "evolution", label: "Evolution", key: "6", href: "/ops_spa#evolution" },
      { id: "llm", label: "LLM providers", key: "7", href: "/ops_spa#llm" },
      { id: "config", label: "Config", key: "8", href: "/ops_spa#config" },
    ];
    useEffect(() => {
      const navItems = items.filter((it) => !it.sect);
      const onKey = (e) => {
        const tag = e.target && e.target.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
        if (e.target && e.target.isContentEditable) return;
        if (e.metaKey || e.ctrlKey || e.altKey) return;
        const n = parseInt(e.key, 10);
        if (n >= 1 && n <= navItems.length) {
          const item = navItems[n - 1];
          if (item && item.href) {
            e.preventDefault();
            window.location.assign(item.href);
          }
        }
      };
      document.addEventListener("keydown", onKey);
      return () => document.removeEventListener("keydown", onKey);
    }, []);
    return h(
      "nav",
      { className: "sidebar" },
      items.map((it, i) =>
        it.sect
          ? h("div", { key: i, className: "nav-section" }, it.sect)
          : h(
              "a",
              {
                key: it.id,
                className: cls("nav-item", active === it.id ? "active" : ""),
                href: it.href,
              },
              h(NavIcon, { kind: it.id }),
              h("span", { className: "label" }, it.label),
              h("span", { className: "nav-key" }, it.key)
            )
      ),
      h("div", { style: { flex: 1 } }),
      h(
        "div",
        {
          style: {
            padding: "var(--s-3)",
            borderTop: "1px solid var(--line-2)",
            marginTop: "var(--s-2)",
          },
        },
        h("div", { className: "metric-label" }, "OPERATOR"),
        h(
          "div",
          {
            className: "mono",
            style: { fontSize: "var(--t-xs)", color: "var(--fg-2)", marginTop: 4 },
          },
          "quant@quanta · root"
        ),
        h(
          "div",
          {
            className: "mono",
            style: { fontSize: "var(--t-2xs)", color: "var(--fg-3)", marginTop: 2 },
          },
          "local · 192.168.1.49:8081"
        )
      )
    );
  }

  // ─────────────── Card ───────────────
  function Card({ num, title, sub, right, children, body = true, className = "" }) {
    const headChildren = [];
    if (num) headChildren.push(h("span", { key: "num", className: "num-tag" }, num));
    headChildren.push(h("h3", { key: "title" }, title));
    if (sub) headChildren.push(h("span", { key: "sub", className: "sub" }, sub));
    if (right) headChildren.push(h(F, { key: "right" }, right));

    const parts = [];
    if (num || title || right) {
      parts.push(h("header", { key: "head", className: "card-head" }, headChildren));
    }
    if (body !== false) {
      parts.push(h("div", { key: "body", className: "card-body" }, children));
    } else {
      parts.push(h(F, { key: "raw" }, children));
    }
    return h("section", { className: cls("card", "mountin", className) }, parts);
  }

  // ─────────────── LiveTicker ───────────────
  function LiveTicker({ items }) {
    const list = items || [];
    const dup = list.concat(list);
    return h(
      "div",
      { className: "ticker" },
      h(
        "div",
        { className: "ticker-track" },
        dup.map((t, i) => {
          const pxLabel =
            t.px < 10
              ? t.px.toFixed(4)
              : t.px.toLocaleString("en-US", {
                  minimumFractionDigits: 2,
                  maximumFractionDigits: 2,
                });
          const sideClass = t.side === "BUY" || t.side === "CSP" ? "up" : "down";
          return h(
            "span",
            { key: t.pair + ":" + t.side + ":" + i, className: "tick" },
            h("span", { className: "dot " + sideClass }),
            h("span", { className: "tick-sym" }, t.pair),
            h("span", { className: "dim" }, t.side),
            h("span", { className: "tick-px num" }, pxLabel),
            h(
              "span",
              { className: cls("tick-d", "num", t.pnl >= 0 ? "up" : "down") },
              (t.pnl >= 0 ? "+" : "") + t.pnl.toFixed(2)
            ),
            h(
              "span",
              { className: "dim", style: { fontSize: "var(--t-2xs)" } },
              fmtAgoSecs(t.t * 60) + " · " + t.venue
            )
          );
        })
      )
    );
  }

  // ─────────────── ProgressBar ───────────────
  function ProgressBar({ value, max = 100, ticks = [], cls: clsName = "" }) {
    const pct = Math.min(100, (value / max) * 100);
    return h(
      "div",
      { className: "bar" },
      h("div", { className: cls("bar-fill", clsName), style: { width: pct + "%" } }),
      (ticks || []).map((t, i) =>
        h("div", {
          key: i,
          className: "bar-tick",
          style: { left: ((t / max) * 100) + "%" },
          title: String(t),
        })
      )
    );
  }

  // ─────────────── TimeSince ───────────────
  // Auto-updating "Ns ago" / "Nm ago" / "Nh ago" label.
  // Accepts ISO string, epoch ms, or epoch seconds. Re-renders every 5s.
  // Returns a <span> with class "mono dim" (matches the vanilla controller).
  function TimeSince({ ts, className = "mono dim" }) {
    const [, force] = useState(0);
    useEffect(() => {
      const iv = setInterval(() => force((n) => n + 1), 5000);
      return () => clearInterval(iv);
    }, []);
    let label = "—";
    if (ts != null) {
      let t;
      if (typeof ts === "string") t = new Date(ts).getTime();
      else if (typeof ts === "number") t = ts < 1e12 ? ts * 1000 : ts; // sec -> ms autodetect
      else t = NaN;
      if (!isNaN(t)) {
        const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
        label =
          s < 5 ? "just now"
            : s < 60 ? s + "s ago"
            : s < 3600 ? Math.floor(s / 60) + "m ago"
            : Math.floor(s / 3600) + "h ago";
      }
    }
    return h("span", { className: className }, label);
  }

  // ─────────────── exports ───────────────
  Object.assign(window, {
    NumberRoll, Sparkline, CandleChart, IndicatorSubchart, RegimeRibbon, StatusRow,
    GateBadge, KillSwitch, Topbar, Sidebar, Card, LiveTicker,
    ProgressBar, TimeSince,
  });
})();
