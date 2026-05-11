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
  // NumberRoll — port of prototype components.jsx NumberRoll.
  //
  // Renders a number with each digit in its own cell. When the value
  // changes, digits roll vertically (translateY in 0.1em units) and a
  // 600ms flash overlay (green up / red down) fires. No JSX — produces
  // raw DOM nodes the caller appends.
  //
  // Usage:
  //   const n = QC.NumberRoll({ initial: 119000, decimals: 2, prefix: "$" });
  //   container.appendChild(n.el);
  //   n.set(119_842.42);  // animates the diff
  //
  // No-digit-leak guarantee: each cell has overflow: hidden, a 1em
  // height, line-height: 1, and the inner track has flex column with
  // 10 children sized exactly 1em each. translateY can never exceed
  // the bounds of any cell.
  // ──────────────────────────────────────────────────────────────────
  function NumberRoll(opts) {
    opts = opts || {};
    const decimals = opts.decimals != null ? opts.decimals : 2;
    const prefix   = opts.prefix || "";
    const suffix   = opts.suffix || "";
    const className = opts.className || "";

    const wrap = el("span", { class: "numroll num " + className });
    let prevValue = null;
    let cells = [];   // {ch: string, node: <span>, track?: <span>}

    function formatString(v) {
      if (v == null || isNaN(v)) return "—";
      return Math.abs(v).toLocaleString("en-US", {
        minimumFractionDigits: decimals, maximumFractionDigits: decimals,
      });
    }

    function clearCells() {
      while (wrap.firstChild) wrap.removeChild(wrap.firstChild);
      cells = [];
    }

    function makeDigit(ch, flashClass) {
      const isDigit = /[0-9]/.test(ch);
      if (!isDigit) {
        const c = document.createElement("span");
        c.className = "numroll-cell wide";
        const inner = document.createElement("span");
        inner.textContent = ch;
        c.appendChild(inner);
        return { ch, node: c };
      }
      const c = document.createElement("span");
      c.className = "numroll-cell" + (flashClass ? " " + flashClass : "");
      const track = document.createElement("span");
      track.className = "numroll-track";
      for (let i = 0; i < 10; i++) {
        const d = document.createElement("span");
        d.textContent = String(i);
        track.appendChild(d);
      }
      const n = parseInt(ch, 10);
      track.style.transform = `translateY(-${n}em)`;
      c.appendChild(track);
      return { ch, node: c, track };
    }

    function set(value) {
      const flashFlag = (prevValue != null && value != null && value !== prevValue)
        ? (value > prevValue ? "flash-up" : "flash-down")
        : null;
      const valStr = formatString(value);
      const sign = (value != null && value < 0) ? "−" : "";
      const fullStr = prefix + sign + valStr + suffix;
      const chars = fullStr.split("");

      // First-time render or length mismatch — rebuild
      if (cells.length !== chars.length) {
        clearCells();
        for (const ch of chars) {
          const cell = makeDigit(ch, flashFlag);
          cells.push(cell);
          wrap.appendChild(cell.node);
        }
      } else {
        // Same length — update each cell in place
        for (let i = 0; i < chars.length; i++) {
          const ch = chars[i];
          const cell = cells[i];
          if (cell.ch !== ch || !/[0-9]/.test(ch)) {
            const fresh = makeDigit(ch, flashFlag);
            wrap.replaceChild(fresh.node, cell.node);
            cells[i] = fresh;
          } else if (cell.track) {
            // Same digit position, just animate to new value if changed
            const n = parseInt(ch, 10);
            cell.track.style.transform = `translateY(-${n}em)`;
            if (flashFlag) {
              cell.node.classList.remove("flash-up", "flash-down");
              // force reflow
              void cell.node.offsetWidth;
              cell.node.classList.add(flashFlag);
              setTimeout(() => cell.node.classList.remove(flashFlag), 600);
            }
          }
        }
      }
      prevValue = value;
    }

    // Initial render
    set(opts.initial != null ? opts.initial : null);
    return { el: wrap, set, get: () => prevValue };
  }

  // ──────────────────────────────────────────────────────────────────
  // KillSwitchProto — exact prototype timing.
  // Wires up an existing <button>/<div> as a hold-to-confirm trigger.
  //   * 1500ms fill animation
  //   * Pointermove-cancel on early release (matches prototype)
  //   * Confirm fires once; subsequent presses do nothing until reset
  //   * Returns { dispose, reset }
  //
  // The caller is responsible for the visual chrome (label, ARM button,
  // breath glow). This helper just runs the timing.
  // ──────────────────────────────────────────────────────────────────
  function killHoldProto(btnEl, onConfirm, opts) {
    opts = opts || {};
    const HOLD_MS = opts.holdMs || 1500;
    let raf = null, t0 = 0, holding = false, confirmed = false;

    // Inject fill element if absent
    let fill = btnEl.querySelector(".kill-hold-fill");
    if (!fill) {
      fill = document.createElement("span");
      fill.className = "kill-hold-fill";
      btnEl.classList.add("kill-hold");
      btnEl.insertBefore(fill, btnEl.firstChild);
    }

    function step(now) {
      if (!holding) return;
      const pct = Math.min(100, ((now - t0) / HOLD_MS) * 100);
      fill.style.width = pct + "%";
      if (pct >= 100) {
        if (!confirmed) {
          confirmed = true;
          holding = false;
          try { onConfirm(); } catch (e) { console.error("kill confirm:", e); }
        }
        return;
      }
      raf = requestAnimationFrame(step);
    }

    function start(e) {
      if (confirmed) return;
      if (e && e.preventDefault) e.preventDefault();
      holding = true;
      t0 = performance.now();
      raf = requestAnimationFrame(step);
    }
    function end() {
      if (!holding) return;
      holding = false;
      cancelAnimationFrame(raf);
      fill.style.width = "0%";
    }
    function reset() {
      confirmed = false;
      holding = false;
      cancelAnimationFrame(raf);
      fill.style.width = "0%";
    }

    btnEl.addEventListener("mousedown",  start);
    btnEl.addEventListener("touchstart", start, { passive: false });
    ["mouseup", "mouseleave", "blur"].forEach(ev => btnEl.addEventListener(ev, end));
    ["touchend", "touchcancel"].forEach(ev => btnEl.addEventListener(ev, end));
    // Pointermove cancel (matches prototype's "release on drag-off")
    btnEl.addEventListener("pointerleave", end);

    return {
      reset,
      dispose() {
        btnEl.removeEventListener("mousedown", start);
        btnEl.removeEventListener("touchstart", start);
        ["mouseup", "mouseleave", "blur", "touchend", "touchcancel", "pointerleave"]
          .forEach(ev => btnEl.removeEventListener(ev, end));
        if (fill) fill.remove();
      },
    };
  }

  // ──────────────────────────────────────────────────────────────────
  // TimeSince — auto-updating "Ns ago" / "Nm ago" label.
  // Pass an ISO string or epoch ms; the element updates every 5s.
  // ──────────────────────────────────────────────────────────────────
  function TimeSince(initialTs) {
    const span = el("span", { class: "mono dim" });
    let ts = initialTs;
    function render() {
      if (ts == null) { span.textContent = "—"; return; }
      const t = typeof ts === "string" ? new Date(ts).getTime() : ts;
      const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
      span.textContent = s < 5 ? "just now"
        : s < 60 ? s + "s ago"
        : s < 3600 ? Math.floor(s / 60) + "m ago"
        : Math.floor(s / 3600) + "h ago";
    }
    render();
    const iv = setInterval(render, 5000);
    return {
      el: span,
      set(t) { ts = t; render(); },
      dispose() { clearInterval(iv); },
    };
  }

  // ──────────────────────────────────────────────────────────────────
  // TweaksFab — React FAB + drawer that toggles theme + density and
  // persists the operator's choice to localStorage. Ported from the
  // legacy /ops in-page script at templates/ops.html:1402-1440 so the
  // SPA reaches operator-parity for the ⌥ panel. dYdX/Geist-compliant:
  //   * 1px solid border, no box-shadow on the drawer (legacy app.css:866
  //     had a 32-blur shadow that violated spec — dropped here).
  //   * Slides in from right via translateX, no scale, no fade-stagger.
  //   * ESC closes.
  //
  // The component lives here (not in qc_react.js) so it stays optional —
  // a self-bootstrapping IIFE below appends a root <div> to <body> and
  // calls ReactDOM.createRoot() on it, which means neither dashboard_spa
  // nor ops_spa has to thread it through their existing component tree
  // (keeps the merge surface with agent C minimal).
  // ──────────────────────────────────────────────────────────────────
  function TweaksFab(props) {
    var React = global.React;
    if (!React) return null;
    var h = React.createElement;
    var F = React.Fragment;

    function readLS(key, fallback) {
      try { return global.localStorage.getItem(key) || fallback; }
      catch (e) { return fallback; }
    }
    function writeLS(key, value) {
      try { global.localStorage.setItem(key, value); }
      catch (e) { /* localStorage may be unavailable */ }
    }

    var openState = React.useState(false);
    var open = openState[0], setOpen = openState[1];
    var themeState = React.useState(function () { return readLS("quanta.theme", "control"); });
    var theme = themeState[0], setTheme = themeState[1];
    var densityState = React.useState(function () { return readLS("quanta.density", "default"); });
    var density = densityState[0], setDensity = densityState[1];

    // Sync <html> attribute when the operator picks a value. The inline
    // boot script in the SPA template already seeded this on first load
    // — this effect keeps it in sync after the React tree mounts.
    React.useEffect(function () {
      document.documentElement.setAttribute("data-theme", theme);
      writeLS("quanta.theme", theme);
    }, [theme]);
    React.useEffect(function () {
      document.documentElement.setAttribute("data-density", density);
      writeLS("quanta.density", density);
    }, [density]);

    // ESC closes the drawer.
    React.useEffect(function () {
      if (!open) return;
      function onKey(e) { if (e.key === "Escape") setOpen(false); }
      document.addEventListener("keydown", onKey);
      return function () { document.removeEventListener("keydown", onKey); };
    }, [open]);

    var fabStyle = {
      position: "fixed", right: 16, bottom: 16,
      width: 36, height: 36,
      background: "var(--bg-card)",
      border: "1px solid var(--line-3)",
      borderRadius: "50%",
      color: "var(--fg-2)",
      cursor: "pointer",
      display: "grid", placeItems: "center",
      zIndex: 60,
      fontFamily: "var(--mono)",
      fontSize: "var(--t-sm)",
    };
    var drawerStyle = {
      position: "fixed", right: 16, bottom: 64,
      width: 280,
      background: "var(--bg-card)",
      border: "1px solid var(--line-3)",
      borderRadius: "var(--r-base)",
      padding: "var(--s-3)",
      zIndex: 61,
      transform: open ? "translateX(0)" : "translateX(calc(100% + 24px))",
      transition: "transform 180ms var(--ease-out, ease-out)",
      pointerEvents: open ? "auto" : "none",
    };
    var headStyle = {
      margin: "0 0 var(--s-2)",
      fontSize: "var(--t-xs)",
      fontFamily: "var(--mono)",
      letterSpacing: ".14em",
      textTransform: "uppercase",
      color: "var(--fg-3)",
    };
    var rowStyle = { display: "flex", gap: 4, marginBottom: "var(--s-3)" };
    function optStyle(active) {
      return {
        flex: 1, padding: 6,
        background: active ? "var(--accent-bg, var(--bg-inset))" : "var(--bg-inset)",
        border: "1px solid " + (active ? "var(--accent-line, var(--line-3))" : "var(--line-2)"),
        borderRadius: "var(--r-sm)",
        color: active ? "var(--fg-1)" : "var(--fg-2)",
        fontFamily: "var(--mono)",
        fontSize: "var(--t-xs)",
        cursor: "pointer",
        textAlign: "center",
      };
    }
    function optBtn(label, value, current, setter) {
      return h("button", {
        key: value,
        type: "button",
        style: optStyle(current === value),
        "aria-pressed": current === value,
        onClick: function () { setter(value); },
      }, label);
    }

    return h(F, null,
      h("button", {
        type: "button",
        style: fabStyle,
        title: "Theme & density",
        "aria-label": "Open theme and density tweaks",
        "aria-expanded": open,
        onClick: function () { setOpen(function (v) { return !v; }); },
      }, "⌥"),
      h("div", { role: "dialog", "aria-label": "Theme and density tweaks", style: drawerStyle },
        h("h4", { style: headStyle }, "Theme"),
        h("div", { style: rowStyle },
          optBtn("Control", "control", theme, setTheme),
          optBtn("Geist", "geist", theme, setTheme),
          optBtn("Bloomberg", "bloomberg", theme, setTheme)
        ),
        h("h4", { style: headStyle }, "Density"),
        h("div", { style: rowStyle },
          optBtn("Compact", "compact", density, setDensity),
          optBtn("Default", "default", density, setDensity),
          optBtn("Roomy", "roomy", density, setDensity)
        )
      )
    );
  }

  // Self-bootstrap: wait for React + ReactDOM (loaded via the UMD <script>
  // tags in the SPA templates) and mount TweaksFab into its own root so it
  // doesn't depend on dashboard_spa / ops_spa wiring it into their tree.
  // Idempotent: bails if the root <div> already exists.
  function bootTweaksFab() {
    if (!global.React || !global.ReactDOM || !global.ReactDOM.createRoot) return;
    if (document.getElementById("quanta-tweaks-fab-root")) return;
    var host = document.createElement("div");
    host.id = "quanta-tweaks-fab-root";
    document.body.appendChild(host);
    global.ReactDOM.createRoot(host).render(global.React.createElement(TweaksFab));
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootTweaksFab);
  } else {
    bootTweaksFab();
  }

  // ──────────────────────────────────────────────────────────────────
  // Public surface
  // ──────────────────────────────────────────────────────────────────
  global.QC = {
    el, fmtUsd, fmtPct, fmtNum, fmtAge,
    gateBadge, pill, dot, statusRow, metric,
    sparkline, regimeRibbon, liveTicker,
    holdToConfirm, flashChange,
    NumberRoll, killHoldProto, TimeSince,
    TweaksFab,
  };
})(window);
