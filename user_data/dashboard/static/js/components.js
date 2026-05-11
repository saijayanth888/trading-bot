/* ════════════════════════════════════════════════════════════════════
   Quanta dashboard — reusable DOM primitives.

   Plain ES module — no React, no Babel, no build step. Every helper
   returns a real DOM node (or in some cases a small element factory)
   so existing render code can call .appendChild(comp(...)) directly.

   Exposed on window.QC so legacy ops.js / app.js can pick them up
   without import statements.
   ════════════════════════════════════════════════════════════════════ */
(function (global) {
  "use strict";

  // ──────────────────────────────────────────────────────────────────
  // tiny safe-DOM helpers
  // ──────────────────────────────────────────────────────────────────
  function el(tag, attrs, children) {
    const n = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null) continue;
        if (k === "class")       n.className = v;
        else if (k === "text")   n.textContent = v;
        else if (k === "style" && typeof v === "object") Object.assign(n.style, v);
        else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2).toLowerCase(), v);
        else if (k === "data") {
          for (const [dk, dv] of Object.entries(v)) n.dataset[dk] = dv;
        }
        else                     n.setAttribute(k, v);
      }
    }
    if (children) {
      const arr = Array.isArray(children) ? children : [children];
      for (const c of arr) {
        if (c == null || c === false) continue;
        n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
      }
    }
    return n;
  }

  function fmtUsd(n, decimals = 2) {
    if (n == null || isNaN(n)) return "—";
    return "$" + Math.abs(n).toLocaleString("en-US", {
      minimumFractionDigits: decimals, maximumFractionDigits: decimals,
    });
  }
  function fmtPct(n, signed = false) {
    if (n == null || isNaN(n)) return "—";
    const v = (n * 100).toFixed(2) + "%";
    if (!signed) return v;
    return (n >= 0 ? "+" : "") + v;
  }
  function fmtNum(n, decimals = 2) {
    if (n == null || isNaN(n)) return "—";
    return Number(n).toLocaleString("en-US", {
      minimumFractionDigits: decimals, maximumFractionDigits: decimals,
    });
  }
  function fmtAge(seconds) {
    if (seconds == null) return "no data";
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
  }

  // ──────────────────────────────────────────────────────────────────
  // GateBadge — PASS / BLOCK / WARN / N/A
  // ──────────────────────────────────────────────────────────────────
  function gateBadge(state, label) {
    const cls = state === true || state === "pass"  ? "gate pass" :
                state === false || state === "block" ? "gate block" :
                state === "warn"                     ? "gate warn" :
                                                       "gate na";
    const text = label != null ? label :
                 (state === true || state === "pass")  ? "PASS"  :
                 (state === false || state === "block") ? "BLOCK" :
                 (state === "warn")                     ? "WARN"  : "N/A";
    return el("span", { class: cls }, text);
  }

  // ──────────────────────────────────────────────────────────────────
  // Pill — coloured tag (up / down / warn / accent / info / default)
  // ──────────────────────────────────────────────────────────────────
  function pill(variant, text) {
    return el("span", { class: "pill " + (variant || "") }, String(text));
  }

  // dot indicator
  function dot(variant) {
    return el("span", { class: "dot " + (variant || "") });
  }

  // ──────────────────────────────────────────────────────────────────
  // StatusRow — colored dot · name · sub · value
  // ──────────────────────────────────────────────────────────────────
  function statusRow({ state, name, sub, value }) {
    const row = el("div", { class: "srow" });
    row.appendChild(dot(state));
    row.appendChild(el("span", { class: "srow-name" }, name || ""));
    row.appendChild(el("span", { class: "srow-sub" }, sub || ""));
    row.appendChild(el("span", { class: "srow-val" }, value != null ? String(value) : "—"));
    return row;
  }

  // ──────────────────────────────────────────────────────────────────
  // MetricTile — label · value · sub · optional delta
  // ──────────────────────────────────────────────────────────────────
  function metric({ label, value, sub, delta, deltaState, size }) {
    const wrap = el("div", { class: "metric" });
    if (label) wrap.appendChild(el("div", { class: "metric-label" }, label));
    const valClass = "metric-value" + (size === "huge" ? " huge" : size === "giant" ? " giant" : "");
    wrap.appendChild(el("div", { class: valClass }, value != null ? String(value) : "—"));
    if (delta != null) {
      const dCls = "metric-delta " + (deltaState || "");
      wrap.appendChild(el("div", { class: dCls }, String(delta)));
    }
    if (sub) wrap.appendChild(el("div", { class: "metric-sub" }, sub));
    return wrap;
  }

  // ──────────────────────────────────────────────────────────────────
  // Sparkline — canvas, simple line, accepts CSS var for color
  // ──────────────────────────────────────────────────────────────────
  function sparkline(canvas, data, { color = "var(--accent)", fill = true, animate = true } = {}) {
    if (!canvas || !data || !data.length) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || canvas.width || 100;
    const h = canvas.clientHeight || canvas.height || 28;
    canvas.width  = w * dpr;
    canvas.height = h * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    const mn = Math.min(...data);
    const mx = Math.max(...data);
    const rng = (mx - mn) || 1;
    const px = i => (i / (data.length - 1)) * (w - 2) + 1;
    const py = v => h - 2 - ((v - mn) / rng) * (h - 4);

    let _color = color;
    if (color.startsWith("var(")) {
      const cssVar = color.replace("var(", "").replace(")", "").trim();
      _color = (getComputedStyle(document.documentElement).getPropertyValue(cssVar) || "#7c5cff").trim();
    }

    let progress = animate ? 0 : 1;
    function draw() {
      ctx.clearRect(0, 0, w, h);
      const end = Math.max(1, Math.floor((data.length - 1) * progress));
      ctx.lineWidth = 1.4;
      ctx.strokeStyle = _color;
      ctx.beginPath();
      for (let i = 0; i <= end; i++) {
        const x = px(i), y = py(data[i]);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
      if (fill) {
        const grad = ctx.createLinearGradient(0, 0, 0, h);
        grad.addColorStop(0, _color + "33");
        grad.addColorStop(1, _color + "00");
        ctx.lineTo(px(end), h);
        ctx.lineTo(px(0), h);
        ctx.closePath();
        ctx.fillStyle = grad;
        ctx.fill();
      }
      if (animate && progress < 1) {
        progress = Math.min(1, progress + 0.08);
        requestAnimationFrame(draw);
      }
    }
    draw();
  }

  // ──────────────────────────────────────────────────────────────────
  // RegimeRibbon — flex-segments with semantic weights
  //   segments: [{ kind: "bull"|"bear"|"range"|"volatile", weight: 0..1 }]
  // ──────────────────────────────────────────────────────────────────
  function regimeRibbon(segments) {
    const bar = el("div", { class: "regbar" });
    const sum = segments.reduce((a, s) => a + (s.weight || 0), 0) || 1;
    for (const s of segments) {
      const seg = el("div", { class: "regbar-seg " + (s.kind || "range") });
      seg.style.flex = `${(s.weight || 0) / sum} 1 0`;
      bar.appendChild(seg);
    }
    return bar;
  }

  // ──────────────────────────────────────────────────────────────────
  // LiveTicker — render an array of {symbol, side, qty, entry, current,
  //              pnl_pct, pnl_usd} into a marquee-scrolling track.
  //              Doubles the items so the loop is seamless.
  // ──────────────────────────────────────────────────────────────────
  function liveTicker(trades) {
    const wrap = el("div", { class: "ticker" });
    if (!trades || !trades.length) {
      wrap.appendChild(el("div", {
        class: "lt-empty",
        style: { padding: "10px 14px" }
      }, "— no active trades right now —"));
      return wrap;
    }
    const track = el("div", { class: "ticker-track" });
    // Render once, then duplicate so the -50% wrap is seamless
    const renderItem = (t) => {
      const item = el("span", { class: "tick" });
      const pnlPct = t.pnl_pct || 0;
      item.appendChild(dot(pnlPct >= 0 ? "up" : "down"));
      item.appendChild(el("span", { class: "tick-sym" }, t.label || t.pair || "?"));
      const side = (t.subkind || t.side || "").toUpperCase();
      item.appendChild(el("span", { class: "dim2" }, side));
      item.appendChild(el("span", { class: "tick-px" },
        fmtNum(t.current || t.current_price || 0, 4)));
      const d = el("span", {
        class: "tick-d " + (pnlPct >= 0 ? "up" : "down")
      }, (pnlPct >= 0 ? "+" : "") + pnlPct.toFixed(2) + "%");
      item.appendChild(d);
      if (t.pnl_usd != null) {
        item.appendChild(el("span", {
          class: "dim", style: { fontSize: "10px" }
        }, "(" + (t.pnl_usd >= 0 ? "+" : "") + fmtNum(t.pnl_usd, 2) + ")"));
      }
      const venue = (t.kind || "").toString().toLowerCase();
      if (venue) item.appendChild(el("span", { class: "dim", style: { fontSize: "10px" } },
        "· " + venue));
      return item;
    };
    [...trades, ...trades].forEach(t => track.appendChild(renderItem(t)));
    wrap.appendChild(track);
    return wrap;
  }

  // ──────────────────────────────────────────────────────────────────
  // Hold-to-confirm helper — wires a button so the operator must hold
  // for `holdMs` to trigger `onConfirm`. Returns a tear-down fn.
  // Used by the topbar kill switch in the templates.
  // ──────────────────────────────────────────────────────────────────
  function holdToConfirm(btn, onConfirm, holdMs = 1500) {
    let holding = false, t0 = 0, raf = null;
    let fillEl = el("span", { class: "kill-hold-fill" });
    btn.classList.add("kill-hold");
    btn.insertBefore(fillEl, btn.firstChild);
    function start(e) {
      e.preventDefault(); holding = true; t0 = performance.now();
      const step = () => {
        if (!holding) return;
        const pct = Math.min(100, ((performance.now() - t0) / holdMs) * 100);
        fillEl.style.width = pct + "%";
        if (pct >= 100) { holding = false; onConfirm(); }
        else raf = requestAnimationFrame(step);
      };
      raf = requestAnimationFrame(step);
    }
    function end() {
      holding = false; cancelAnimationFrame(raf); fillEl.style.width = "0%";
    }
    btn.addEventListener("mousedown", start);
    btn.addEventListener("touchstart", start, { passive: false });
    ["mouseup", "mouseleave", "touchend", "touchcancel"].forEach(ev => btn.addEventListener(ev, end));
    return () => {
      btn.removeEventListener("mousedown", start);
      btn.removeEventListener("touchstart", start);
      ["mouseup", "mouseleave", "touchend", "touchcancel"].forEach(ev => btn.removeEventListener(ev, end));
      fillEl.remove();
    };
  }

  // ──────────────────────────────────────────────────────────────────
  // Number flash — fire a 600ms green/red overlay when a number changes.
  // Used for top-bar equity, live trade PnLs, etc.
  // ──────────────────────────────────────────────────────────────────
  function flashChange(el, oldV, newV) {
    if (!el || oldV == null || newV == null || oldV === newV) return;
    el.classList.remove("flash-up", "flash-down");
    el.offsetWidth;  // force reflow
    el.classList.add(newV > oldV ? "flash-up" : "flash-down");
    setTimeout(() => el.classList.remove("flash-up", "flash-down"), 600);
  }

  // ──────────────────────────────────────────────────────────────────
  // Public surface
  // ──────────────────────────────────────────────────────────────────
  global.QC = {
    el, fmtUsd, fmtPct, fmtNum, fmtAge,
    gateBadge, pill, dot, statusRow, metric,
    sparkline, regimeRibbon, liveTicker,
    holdToConfirm, flashChange,
  };
})(window);
