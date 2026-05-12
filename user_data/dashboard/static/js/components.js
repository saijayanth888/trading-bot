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
  // CommandPalette (V3 §5.7) — Cmd/Ctrl+K, fuzzy filter, sections.
  // Mount inside each SPA React tree (ops_spa / dashboard_spa). No portal.
  // ──────────────────────────────────────────────────────────────────
  var PALETTE_RECENT_KEY = "quanta.cmdp.recent.v1";

  function paletteReadHermesKey() {
    try { return global.sessionStorage.getItem("hermesMcpKey") || ""; }
    catch (e) { return ""; }
  }
  function paletteEnsureHermesKey() {
    var k = paletteReadHermesKey();
    if (k) return k;
    var p = global.prompt("Enter Hermes MCP key (stored in session for mutating actions):");
    if (p && String(p).trim()) {
      try { global.sessionStorage.setItem("hermesMcpKey", String(p).trim()); } catch (e2) { /* ignore */ }
      return String(p).trim();
    }
    return "";
  }
  function paletteAuthHeadersJson() {
    var headers = { "Content-Type": "application/json" };
    var k = paletteReadHermesKey();
    if (k) {
      headers.Authorization = "Bearer " + k;
      headers["X-Hermes-MCP-Key"] = k;
    }
    return headers;
  }
  function palettePushRecent(entry) {
    try {
      var raw = global.sessionStorage.getItem(PALETTE_RECENT_KEY) || "[]";
      var arr = [];
      try { arr = JSON.parse(raw); } catch (e) { arr = []; }
      if (!Array.isArray(arr)) arr = [];
      arr.unshift({ title: entry.title, ts: Date.now(), kind: entry.kind || "" });
      var seen = {};
      var dedup = [];
      for (var i = 0; i < arr.length; i++) {
        var t = String(arr[i].title || "");
        if (seen[t]) continue;
        seen[t] = 1;
        dedup.push(arr[i]);
        if (dedup.length >= 5) break;
      }
      global.sessionStorage.setItem(PALETTE_RECENT_KEY, JSON.stringify(dedup));
    } catch (e3) { /* ignore */ }
  }

  /** Simple substring + token-boundary score (no RegExp.exec). */
  function paletteFuzzyScore(query, text) {
    var t = String(text || "").toLowerCase();
    var q = String(query || "").toLowerCase().trim();
    if (!q.length) return 1;
    var score = 0;
    var idx = t.indexOf(q);
    if (idx >= 0) score += 80 + Math.max(0, 20 - idx);
    var parts = q.split(/\s+/);
    for (var pi = 0; pi < parts.length; pi++) {
      var tok = parts[pi];
      if (!tok) continue;
      var pos = 0;
      while (true) {
        var j = t.indexOf(tok, pos);
        if (j < 0) break;
        var prev = j > 0 ? t.charCodeAt(j - 1) : 32;
        var boundary = j === 0 || prev < 48 || (prev > 57 && prev < 65) || (prev > 90 && prev < 97) || prev > 122;
        score += boundary ? 12 : 4;
        pos = j + tok.length;
      }
    }
    return score;
  }

  function paletteEnvData(env) {
    if (env && typeof env === "object" && "data" in env) return env.data;
    return env;
  }

  function CommandPalette(props) {
    var React = global.React;
    if (!React) return null;
    var h = React.createElement;
    var F = React.Fragment;
    var useState = React.useState;
    var useEffect = React.useEffect;
    var useMemo = React.useMemo;
    var useRef = React.useRef;
    var variant = props.variant || "ops";
    var opsData = props.opsData || {};
    var dash = props.dash || {};
    var setKillState = props.setKillState;
    var HTC = global.HoldToConfirmButton;

    var openState = useState(false);
    var open = openState[0], setOpen = openState[1];
    var qState = useState("");
    var query = qState[0], setQuery = qState[1];
    var idxState = useState(0);
    var selIdx = idxState[0], setSelIdx = idxState[1];
    var toolsState = useState([]);
    var tools = toolsState[0], setTools = toolsState[1];
    var pendState = useState(null);
    var pending = pendState[0], setPending = pendState[1];

    var riskDraftState = useState({ daily: 0.03, nameCap: 0.1, corr: 0.85 });
    var riskDraft = riskDraftState[0], setRiskDraft = riskDraftState[1];
    var riskInitRef = useRef(false);

    useEffect(function () {
      riskInitRef.current = false;
    }, [opsData.risk_gates]);

    useEffect(function () {
      function onKey(e) {
        var metaK = (e.metaKey || e.ctrlKey) && String(e.key).toLowerCase() === "k";
        if (metaK) {
          var t = e.target;
          if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) {
            if (!(t.getAttribute && t.getAttribute("data-cmdp-input") === "1")) return;
          }
          e.preventDefault();
          setOpen(function (v) { return !v; });
        }
      }
      global.document.addEventListener("keydown", onKey, true);
      return function () { global.document.removeEventListener("keydown", onKey, true); };
    }, []);

    useEffect(function () {
      if (!open) return;
      setSelIdx(0);
      setPending(null);
      var ctrl = new AbortController();
      global.fetch("/api/ops/tools", { signal: ctrl.signal })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          var d = paletteEnvData(j) || {};
          var list = Array.isArray(d.tools) ? d.tools : [];
          setTools(list);
        })
        .catch(function () { setTools([]); });
      return function () { ctrl.abort(); };
    }, [open]);

    useEffect(function () {
      if (variant !== "ops" || !open) return;
      var rg = paletteEnvData(opsData.risk_gates) || {};
      var resolved = rg.resolved || rg.risk_gates || {};
      if (!riskInitRef.current && resolved && typeof resolved === "object") {
        riskInitRef.current = true;
        setRiskDraft({
          daily: Number(resolved.daily_loss_halt_pct != null ? resolved.daily_loss_halt_pct : 0.03),
          nameCap: Number(resolved.single_name_cap_pct != null ? resolved.single_name_cap_pct : 0.1),
          corr: Number(resolved.correlation_cap != null ? resolved.correlation_cap : 0.85),
        });
      }
    }, [variant, open, opsData.risk_gates]);

    function scrollToId(id) {
      var el = global.document.getElementById(id);
      if (el && el.scrollIntoView) el.scrollIntoView({ behavior: "smooth", block: "center" });
    }

    var items = useMemo(function () {
      var out = [];
      function add(section, title, sub, mutating, run, extra) {
        var hay = section + " " + title + " " + (sub || "");
        var sc = paletteFuzzyScore(query, hay);
        if (query && sc <= 0) return;
        out.push({ section: section, title: title, sub: sub || "", mutating: !!mutating, run: run, score: sc + (extra || 0) });
      }

      add("Navigation", "Go to crypto pair telemetry (card 06)", "scroll #pair-telemetry-crypto", false, function () { scrollToId("pair-telemetry-crypto"); setOpen(false); }, 2);
      add("Navigation", "Go to stocks pair telemetry (card 23)", "scroll #pair-telemetry-stocks", false, function () { scrollToId("pair-telemetry-stocks"); setOpen(false); }, 2);
      add("Navigation", "Go to card 21a Agent flow strip", "scroll #agent-flow-strip", false, function () { scrollToId("agent-flow-strip"); setOpen(false); }, 2);
      add("Navigation", "Open Service health card", "scroll #service-health", false, function () { scrollToId("service-health"); setOpen(false); }, 2);
      add("Navigation", "Open Quick actions control panel", "scroll #quick-actions", false, function () { scrollToId("quick-actions"); setOpen(false); }, 2);
      add("Navigation", "Open MCP tool console", "scroll #mcp-console", false, function () { scrollToId("mcp-console"); setOpen(false); }, 2);
      add("Navigation", "Go to LLM activity section", "scroll #llm-calls", false, function () { scrollToId("llm-calls"); setOpen(false); }, 1);
      add("Navigation", "Go to training row", "scroll #training", false, function () { scrollToId("training"); setOpen(false); }, 1);
      add("Navigation", "Go to risk gates matrix", "scroll #risk", false, function () { scrollToId("risk"); setOpen(false); }, 1);

      var cryptoPairs = [];
      if (variant === "ops") {
        var sp = paletteEnvData(opsData.sparklines) || {};
        var pm = sp.pairs || {};
        cryptoPairs = Object.keys(pm);
      } else if (dash.cryptoPairs && dash.cryptoPairs.length) {
        cryptoPairs = dash.cryptoPairs;
      }
      for (var ci = 0; ci < cryptoPairs.length; ci++) {
        var cp = cryptoPairs[ci];
        (function (pairSym) {
          add("Pairs", pairSym, "crypto · open dashboard", false, function () {
            global.location.href = "/?pair=" + encodeURIComponent(pairSym) + "&venue=crypto";
          }, 0);
        })(cp);
      }

      var stockSyms = [];
      if (variant === "ops") {
        var ss = paletteEnvData(opsData.stocks_sparklines) || {};
        stockSyms = Array.isArray(ss.basket) ? ss.basket : Object.keys(ss.symbols || {});
      } else if (dash.stockSymbols && dash.stockSymbols.length) {
        stockSyms = dash.stockSymbols;
      }
      for (var si = 0; si < stockSyms.length; si++) {
        var st = stockSyms[si];
        (function (sym) {
          add("Pairs", sym, "stocks · open dashboard", false, function () {
            global.location.href = "/?pair=" + encodeURIComponent(sym) + "&venue=stocks";
          }, 0);
        })(st);
      }

      if (variant === "ops") {
        add("Actions", "Pause all entries", "POST /api/ops/pause", true, function () {
          var k = paletteEnsureHermesKey();
          if (!k) return;
          return global.fetch("/api/ops/pause", { method: "POST", headers: paletteAuthHeadersJson(), body: JSON.stringify({ reason: "operator command palette pause" }) })
            .then(function () { palettePushRecent({ title: "Pause all entries", kind: "action" }); setOpen(false); });
        }, 0);
        add("Actions", "Flatten all positions", "POST /api/ops/pause flatten payload", true, function () {
          var k = paletteEnsureHermesKey();
          if (!k) return;
          return global.fetch("/api/ops/pause", { method: "POST", headers: paletteAuthHeadersJson(), body: JSON.stringify({ reason: "operator kill bar flatten+halt via palette" }) })
            .then(function () { palettePushRecent({ title: "Flatten all positions", kind: "action" }); setOpen(false); });
        }, 0);
        add("Actions", "Kill bot (UI halt state)", "sets kill strip to KILLED", true, function () {
          if (setKillState) setKillState("killed");
          palettePushRecent({ title: "Kill bot (UI halt state)", kind: "action" });
          setOpen(false);
        }, 0);
        add("Actions", "Resume after manual review", "POST /api/ops/resume", true, function () {
          var k = paletteEnsureHermesKey();
          if (!k) return;
          return global.fetch("/api/ops/resume", { method: "POST", headers: paletteAuthHeadersJson(), body: JSON.stringify({ reason: "operator command palette resume", confirm: true }) })
            .then(function () { palettePushRecent({ title: "Resume after manual review", kind: "action" }); setOpen(false); });
        }, 0);
      }

      add("Settings", "Switch theme to control", "", false, function () {
        try { global.localStorage.setItem("quanta.theme", "control"); global.document.documentElement.setAttribute("data-theme", "control"); } catch (e) { /* */ }
        setOpen(false);
      }, 0);
      add("Settings", "Switch theme to geist", "", false, function () {
        try { global.localStorage.setItem("quanta.theme", "geist"); global.document.documentElement.setAttribute("data-theme", "geist"); } catch (e) { /* */ }
        setOpen(false);
      }, 0);
      add("Settings", "Switch theme to bloomberg", "", false, function () {
        try { global.localStorage.setItem("quanta.theme", "bloomberg"); global.document.documentElement.setAttribute("data-theme", "bloomberg"); } catch (e) { /* */ }
        setOpen(false);
      }, 0);
      add("Settings", "Switch density to compact", "", false, function () {
        try { global.localStorage.setItem("quanta.density", "compact"); global.document.documentElement.setAttribute("data-density", "compact"); } catch (e) { /* */ }
        setOpen(false);
      }, 0);
      add("Settings", "Switch density to default", "", false, function () {
        try { global.localStorage.setItem("quanta.density", "default"); global.document.documentElement.setAttribute("data-density", "default"); } catch (e) { /* */ }
        setOpen(false);
      }, 0);
      add("Settings", "Switch density to roomy", "", false, function () {
        try { global.localStorage.setItem("quanta.density", "roomy"); global.document.documentElement.setAttribute("data-density", "roomy"); } catch (e) { /* */ }
        setOpen(false);
      }, 0);

      if (variant === "ops") {
        var rg2 = paletteEnvData(opsData.risk_gates) || {};
        var rgMap = rg2.risk_gates || rg2.resolved || {};
        var keys = Object.keys(rgMap || {});
        for (var ki = 0; ki < keys.length; ki++) {
          var ky = keys[ki];
          (function (keyName) {
            add("Risk config", "risk gate key · " + keyName, String(rgMap[keyName]), false, function () { scrollToId("risk"); setOpen(false); }, 0);
          })(ky);
        }
        add("Risk config", "Apply risk draft (daily halt / name cap / correlation)", "POST /api/ops/risk_gates", true, function () {
          var k = paletteEnsureHermesKey();
          if (!k) return;
          var base = paletteEnvData(opsData.risk_gates) || {};
          var cur = Object.assign({}, base.resolved || base.risk_gates || {});
          cur.daily_loss_halt_pct = riskDraft.daily;
          cur.single_name_cap_pct = riskDraft.nameCap;
          cur.correlation_cap = riskDraft.corr;
          return global.fetch("/api/ops/risk_gates", { method: "POST", headers: paletteAuthHeadersJson(), body: JSON.stringify({ risk_gates: cur }) })
            .then(function () { palettePushRecent({ title: "Apply risk draft", kind: "risk" }); setOpen(false); });
        }, 0);
      }

      for (var ti = 0; ti < tools.length; ti++) {
        var tool = tools[ti];
        (function (t) {
          var title = (t.mutating ? "❗ " : "") + "MCP tool · " + t.name;
          add("MCP tools", title, t.doc || "POST /api/ops/mcp/" + t.name, !!t.mutating, function () {
            global.location.hash = "mcp-console";
            scrollToId("mcp-console");
            palettePushRecent({ title: title, kind: "mcp" });
            setOpen(false);
          }, 0);
        })(tool);
      }

      var recent = [];
      try { recent = JSON.parse(global.sessionStorage.getItem(PALETTE_RECENT_KEY) || "[]"); } catch (e4) { recent = []; }
      if (!Array.isArray(recent)) recent = [];
      for (var ri = 0; ri < recent.length; ri++) {
        (function (rec) {
          add("Recent", rec.title || "—", "last used", false, function () { setOpen(false); }, 0);
        })(recent[ri]);
      }

      out.sort(function (a, b) { return b.score - a.score; });
      return out;
    }, [variant, query, tools, opsData, dash, riskDraft]);

    useEffect(function () {
      if (selIdx >= items.length) setSelIdx(Math.max(0, items.length - 1));
    }, [items.length, selIdx]);

    function runItem(it) {
      if (!it || !it.run) return;
      if (it.mutating) {
        setPending(it);
        return;
      }
      try { it.run(); } catch (e) { /* */ }
      palettePushRecent({ title: it.title, kind: "run" });
    }

    useEffect(function () {
      if (!open) return;
      function onNav(e) {
        if (e.key === "Escape") { e.preventDefault(); setOpen(false); return; }
        if (e.key === "ArrowDown") { e.preventDefault(); setSelIdx(function (i) { return Math.min(items.length - 1, i + 1); }); }
        if (e.key === "ArrowUp") { e.preventDefault(); setSelIdx(function (i) { return Math.max(0, i - 1); }); }
        if (e.key === "Enter") {
          e.preventDefault();
          var it = items[selIdx];
          runItem(it);
        }
      }
      global.document.addEventListener("keydown", onNav, true);
      return function () { global.document.removeEventListener("keydown", onNav, true); };
    }, [open, items, selIdx]);

    if (!open) return null;

    var sections = {};
    for (var ii = 0; ii < items.length; ii++) {
      var it0 = items[ii];
      if (!sections[it0.section]) sections[it0.section] = [];
      sections[it0.section].push({ idx: ii, item: it0 });
    }
    var sectionOrder = ["Navigation", "Pairs", "Actions", "Settings", "Risk config", "MCP tools", "Recent"];

    return h("div", {
      className: "v3-cmdp-backdrop",
      role: "presentation",
      onMouseDown: function (e) { if (e.target && e.target.classList && e.target.classList.contains("v3-cmdp-backdrop")) setOpen(false); },
    },
      h("div", { className: "v3-cmdp-panel", role: "dialog", "aria-label": "Command palette" },
        h("div", { className: "v3-cmdp-head" }, "Command palette · ⌘K · fuzzy search"),
        h("input", {
          className: "v3-cmdp-input",
          "data-cmdp-input": "1",
          autoFocus: true,
          placeholder: "Search cards, pairs, tools, settings…",
          value: query,
          onChange: function (e) { setQuery(e.target.value); setSelIdx(0); },
        }),
        h("div", { className: "v3-cmdp-list" },
          sectionOrder.map(function (sec) {
            var rows = sections[sec];
            if (!rows || !rows.length) return null;
            return h(F, { key: sec },
              h("div", { className: "v3-cmdp-section" }, sec),
              rows.map(function (row) {
                var it1 = row.item;
                var active = row.idx === selIdx;
                return h("div", {
                  key: sec + "-" + row.idx,
                  className: "v3-cmdp-item" + (active ? " v3-cmdp-item--active" : ""),
                  onMouseEnter: function () { setSelIdx(row.idx); },
                  onClick: function () { runItem(it1); },
                },
                  h("div", { className: "v3-cmdp-item-title" },
                    it1.title,
                    it1.mutating ? h("span", { className: "v3-cmdp-badge" }, "mutating") : null),
                  h("div", { className: "v3-cmdp-item-sub" }, it1.sub)
                );
              })
            );
          }),
          variant === "ops" ? h("div", { key: "risk-sliders", className: "v3-cmdp-section" }, "Risk sliders (draft)") : null,
          variant === "ops" ? h("div", { key: "risk-ui", style: { padding: "8px 14px", fontSize: "var(--t-2xs)" } },
            h("label", { style: { display: "block", marginBottom: 6 } }, "daily_loss_halt_pct · ", h("span", { className: "v3-num" }, (riskDraft.daily * 100).toFixed(1)), "%",
              h("input", {
                type: "range", min: 0, max: 0.2, step: 0.005, value: riskDraft.daily,
                style: { width: "100%" },
                onChange: function (e) { setRiskDraft(function (d) { return Object.assign({}, d, { daily: Number(e.target.value) }); }); },
              })),
            h("label", { style: { display: "block", marginBottom: 6 } }, "single_name_cap_pct · ", h("span", { className: "v3-num" }, (riskDraft.nameCap * 100).toFixed(1)), "%",
              h("input", {
                type: "range", min: 0, max: 0.5, step: 0.01, value: riskDraft.nameCap,
                style: { width: "100%" },
                onChange: function (e) { setRiskDraft(function (d) { return Object.assign({}, d, { nameCap: Number(e.target.value) }); }); },
              })),
            h("label", { style: { display: "block" } }, "correlation_cap · ", h("span", { className: "v3-num" }, riskDraft.corr.toFixed(2)),
              h("input", {
                type: "range", min: 0, max: 1, step: 0.01, value: riskDraft.corr,
                style: { width: "100%" },
                onChange: function (e) { setRiskDraft(function (d) { return Object.assign({}, d, { corr: Number(e.target.value) }); }); },
              }))
          ) : null
        ),
        pending && HTC ? h("div", { className: "v3-cmdp-hold-wrap" },
          h("div", { className: "dim mono", style: { fontSize: "var(--t-2xs)", marginBottom: 6 } }, "Hold 1500ms to run · ", pending.title),
          h(HTC, {
            label: "CONFIRM · RUN ACTION",
            variant: "compact",
            danger: !!pending.mutating,
            ariaLabel: "Confirm command palette action",
            onHoldComplete: function () {
              var p = pending;
              setPending(null);
              if (p && p.run) {
                try {
                  var ret = p.run();
                  if (ret && typeof ret.then === "function") ret.then(function () {}).catch(function () {});
                } catch (e5) { /* */ }
              }
            },
          })
        ) : null
      )
    );
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
    CommandPalette,
  };
})(window);
