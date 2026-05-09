/* ════════════════════════════════════════════════════════════════════════
   QUANT EDITORIAL · effects layer
   - flash-on-change for numeric values (MutationObserver-driven)
   - typewriter scramble on regime headline change
   - parallax ticker content (live pull from /api/ops/services)
   - section count-up on first paint
   - chart-area crosshair cursor
   No external deps. Defers gracefully if elements aren't present.
   ════════════════════════════════════════════════════════════════════════ */

(function () {
  "use strict";

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ── Helper: parse value out of a DOM string. Returns NaN if non-numeric.
  function parseNum(s) {
    if (s == null) return NaN;
    const m = String(s).trim().replace(/[\s,$%+]/g, "").match(/-?\d+(\.\d+)?/);
    return m ? parseFloat(m[0]) : NaN;
  }

  // ── Helper: pick a flash class given old → new. Up=teal, Down=coral, =amber.
  function flashClassFor(oldStr, newStr) {
    const a = parseNum(oldStr), b = parseNum(newStr);
    if (Number.isFinite(a) && Number.isFinite(b)) {
      if (b > a) return "flash-up";
      if (b < a) return "flash-down";
      return null;
    }
    return "flash-neutral";
  }

  // ── 1. Number-flash on text mutation ────────────────────────────────
  const FLASH_SELECTORS = [
    ".kv b",
    ".stat-value",
    ".profit-pos", ".profit-neg",
    ".recent .pnl",
    ".pnl-cell",
    "[data-flash]"
  ];

  function attachFlashWatcher(el) {
    if (el.__flashWatched) return;
    el.__flashWatched = true;
    let prev = el.textContent || "";
    const obs = new MutationObserver(() => {
      const next = el.textContent || "";
      if (next === prev) return;
      const cls = flashClassFor(prev, next);
      prev = next;
      if (!cls || reduced) return;
      el.classList.remove("flash-up", "flash-down", "flash-neutral");
      void el.offsetWidth;
      el.classList.add(cls);
      setTimeout(() => el.classList.remove(cls), 950);
    });
    obs.observe(el, { childList: true, characterData: true, subtree: true });
  }

  function watchAllFlashable(root) {
    root = root || document;
    FLASH_SELECTORS.forEach(sel => {
      root.querySelectorAll(sel).forEach(attachFlashWatcher);
    });
  }

  const rootObs = new MutationObserver(muts => {
    for (const m of muts) {
      m.addedNodes.forEach(n => {
        if (!(n instanceof HTMLElement)) return;
        watchAllFlashable(n);
        if (n.matches && FLASH_SELECTORS.some(sel => n.matches(sel))) {
          attachFlashWatcher(n);
        }
      });
    }
  });

  // ── 2. Hero regime fade-in on change ────────────────────────────────
  // Modern, subtle: when the regime headline text changes, fade out then in.
  function attachHeroFade() {
    const el = document.getElementById("hero-regime");
    if (!el) return;
    let prev = el.textContent || "";
    let animating = false;
    const obsOpts = { childList: true, characterData: true, subtree: true };
    const obs = new MutationObserver(() => {
      if (animating) return;
      const next = el.textContent || "";
      if (next === prev) return;
      const cleanPrev = prev.replace(/\s+/g, " ").trim();
      const cleanNext = next.replace(/\s+/g, " ").trim();
      prev = next;
      if (reduced || !cleanPrev || !cleanNext || cleanPrev === cleanNext) return;
      animating = true;
      el.style.transition = "opacity 200ms ease, transform 200ms ease";
      el.style.opacity = "0";
      el.style.transform = "translateY(-4px)";
      setTimeout(() => {
        el.style.opacity = "1";
        el.style.transform = "translateY(0)";
        setTimeout(() => { animating = false; }, 220);
      }, 200);
    });
    obs.observe(el, obsOpts);
  }

  // ── 3. Ticker — safe DOM construction (no innerHTML) ────────────────
  const STATIC_TICKER = [
    { label: "FREQTRADE",  v: "8080",            kind: "info"  },
    { label: "TFT",        v: "v1 · 8 PAIRS",    kind: "good"  },
    { label: "POSTGRES",   v: "5434",            kind: "info"  },
    { label: "INFLUX",     v: "8086",            kind: "info"  },
    { label: "GRAFANA",    v: "3000",            kind: "info"  },
    { label: "HERMES MCP", v: "8089",            kind: "info"  },
    { label: "OLLAMA",     v: "11434",           kind: "info"  },
    { label: "REGIME",     v: "4-STATE HMM",     kind: "good"  },
    { label: "RISK",       v: "8 GATES · 8% DD", kind: "good"  },
    { label: "EPT",        v: "12 GENOMES · 36H",kind: "good"  },
    { label: "SENTIMENT",  v: "5 SOURCES",       kind: "good"  }
  ];

  // Map kind → element tag (the tag picks up its color via CSS).
  // info -> <i> (teal), good -> <b> (amber), bad -> <u> (coral)
  function tagFor(kind) {
    if (kind === "info") return "i";
    if (kind === "bad")  return "u";
    return "b";
  }

  function buildTickerSpan(item) {
    const span = document.createElement("span");
    // Label as plain text node
    span.appendChild(document.createTextNode(item.label + " "));
    const accent = document.createElement(tagFor(item.kind));
    accent.textContent = item.v;
    span.appendChild(accent);
    return span;
  }

  function renderTicker(extra) {
    const tracks = document.querySelectorAll(".ticker-track");
    if (!tracks.length) return;
    const items = STATIC_TICKER.concat(extra || []);
    tracks.forEach(track => {
      while (track.firstChild) track.removeChild(track.firstChild);
      // Duplicate items so translateX -50% loops seamlessly
      [items, items].forEach(seq => {
        seq.forEach(it => track.appendChild(buildTickerSpan(it)));
      });
    });
  }

  async function refreshLiveTicker() {
    try {
      const r = await fetch("/api/ops/services", { cache: "no-store" });
      if (!r.ok) return;
      const j = await r.json();
      const data = (j && j.data) || {};
      const extra = [];
      const probe = (key, name) => {
        const ent = data[key];
        if (!ent) return;
        extra.push({
          label: name,
          v: ent.up ? "ONLINE" : "OFFLINE",
          kind: ent.up ? "info" : "bad"
        });
      };
      probe("freqtrade", "BOT");
      probe("hermes_mcp", "MCP");
      probe("hermes_gateway", "GATEWAY");
      probe("ollama", "LLM");
      const now = new Date();
      extra.push({
        label: "UTC",
        v: now.toISOString().slice(11, 19),
        kind: "good"
      });
      renderTicker(extra);
    } catch (_) { /* ignore */ }
  }

  // ── 3b. Live stat-strip updater (ops console) ───────────────────────
  function setStat(id, text, signedClass) {
    const el = document.getElementById(id);
    if (!el) return;
    const cur = el.textContent || "";
    if (cur !== text) el.textContent = text;
    el.classList.remove("is-pos", "is-neg", "is-warn");
    if (signedClass) el.classList.add(signedClass);
  }

  function fmtUSD(v) {
    if (v == null || !Number.isFinite(v)) return "—";
    const sign = v >= 0 ? "+" : "";
    return sign + "$" + Math.abs(v).toLocaleString("en-US", {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    }).replace(/^/, v < 0 ? "-" : "");
  }
  function fmtPct(v) {
    if (v == null || !Number.isFinite(v)) return "—";
    const sign = v > 0 ? "+" : "";
    return sign + v.toFixed(2) + "%";
  }

  async function refreshStatStrip() {
    if (!document.getElementById("stat-pnl")) return; // ops only
    try {
      const [risk, mode, regime] = await Promise.all([
        fetch("/api/ops/trades_risk", { cache: "no-store" }).then(r => r.json()).catch(() => null),
        fetch("/api/mode",            { cache: "no-store" }).then(r => r.json()).catch(() => null),
        fetch("/api/ops/regime",      { cache: "no-store" }).then(r => r.json()).catch(() => null)
      ]);
      const r = (risk && risk.data) || {};
      const m = mode || {};
      const g = (regime && regime.data) || {};

      // P&L
      const pnl = Number(r.daily_pnl_usd);
      setStat("stat-pnl",
        Number.isFinite(pnl) ? fmtUSD(pnl) : "—",
        Number.isFinite(pnl) ? (pnl > 0 ? "is-pos" : pnl < 0 ? "is-neg" : null) : null);

      // Open trades (X / Y)
      const open = r.open_count, max = r.max_open;
      setStat("stat-open",
        (open != null && max != null) ? `${open} / ${max}` : "—",
        (open != null && open > 0) ? "is-pos" : null);

      // 30-day drawdown
      const dd = Number(r.drawdown_pct_30d);
      setStat("stat-dd",
        Number.isFinite(dd) ? fmtPct(-Math.abs(dd)) : "—",
        Number.isFinite(dd) ? (Math.abs(dd) > 5 ? "is-warn" : null) : null);

      // Active regime
      const reg = g.regime || g.current_regime || "—";
      const regHuman = String(reg).toLowerCase().replace(/_/g, " ");
      setStat("stat-regime", regHuman.toUpperCase(), null);

      // Bot state
      const state = (m.mode || "—").toUpperCase();
      const cls = state === "LIVE" ? "is-neg"
                : state === "PAPER" ? "is-warn"
                : state === "PAUSED" ? null : null;
      setStat("stat-state", state, cls);

      // ─── Hero bot-status block (explicit, unambiguous) ──────
      const heroBot = document.querySelector(".hero-bot");
      if (heroBot) {
        const runState = String(m.state || "unknown").toLowerCase();
        // Map raw freqtrade states → display states
        const display = runState === "running" ? "running"
                      : runState === "paused"  ? "paused"
                      : runState === "stopped" ? "stopped"
                      : runState === "unknown" ? "offline"
                      : "error";
        heroBot.setAttribute("data-state", display);
        const nameEl = document.getElementById("hero-bot-state");
        if (nameEl) nameEl.textContent =
          display === "running" ? "Running"
          : display === "paused" ? "Paused"
          : display === "stopped" ? "Stopped"
          : display === "offline" ? "Offline"
          : "Error";
        // Mode pill (PAPER / LIVE / PAUSED)
        const modeEl = document.getElementById("hero-bot-mode");
        if (modeEl) {
          modeEl.textContent = state;
          modeEl.classList.remove("mode-paper", "mode-live", "mode-paused");
          if (state === "PAPER")  modeEl.classList.add("mode-paper");
          if (state === "LIVE")   modeEl.classList.add("mode-live");
          if (state === "PAUSED") modeEl.classList.add("mode-paused");
        }
        // Open positions
        const openEl = document.getElementById("hero-bot-open");
        if (openEl && open != null && max != null) {
          openEl.textContent = `${open} / ${max}`;
        }
      }
    } catch (_) { /* ignore */ }
  }

  // ── 4. Section count-up on first paint ──────────────────────────────
  function animateSectionNums() {
    if (reduced) return;
    const heads = document.querySelectorAll("[data-num]");
    heads.forEach((h, i) => {
      const target = h.getAttribute("data-num") || "";
      const m = target.match(/(\d+)/);
      if (!m) return;
      const finalN = parseInt(m[1], 10);
      const pad = m[1].length;
      const suffix = target.replace(m[1], "##");
      let n = 0;
      const startDelay = 80 + i * 40;
      setTimeout(() => {
        const tick = () => {
          n = Math.min(finalN, n + 1);
          h.setAttribute("data-num",
            suffix.replace("##", String(n).padStart(pad, "0")));
          if (n < finalN) setTimeout(tick, 18);
        };
        tick();
      }, startDelay);
    });
  }

  // ── 5. Crosshair cursor on chart canvases ───────────────────────────
  function applyCrosshair() {
    const SVG_CROSS = "data:image/svg+xml;utf8," + encodeURIComponent(
      `<svg xmlns='http://www.w3.org/2000/svg' width='22' height='22' viewBox='0 0 22 22'>
         <line x1='11' y1='1' x2='11' y2='21' stroke='%23f4b942' stroke-width='1'/>
         <line x1='1' y1='11' x2='21' y2='11' stroke='%23f4b942' stroke-width='1'/>
         <circle cx='11' cy='11' r='3' fill='none' stroke='%23f4b942' stroke-width='1'/>
       </svg>`
    );
    const css = `cursor: url('${SVG_CROSS}') 11 11, crosshair;`;
    document.querySelectorAll(".chart, .subchart").forEach(el => {
      el.style.cssText += css;
    });
  }

  // ── Boot ────────────────────────────────────────────────────────────
  function boot() {
    watchAllFlashable(document);
    rootObs.observe(document.body, { childList: true, subtree: true });
    attachHeroFade();
    renderTicker();
    refreshLiveTicker();
    setInterval(refreshLiveTicker, 60_000);
    refreshStatStrip();
    setInterval(refreshStatStrip, 5_000);
    animateSectionNums();
    applyCrosshair();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
