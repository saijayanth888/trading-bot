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
    DDRibbon, HeartbeatDot, KillBar, deriveHeartbeatStatus,
    HoldToConfirmButton,
  } = window;
  const CommandPalette = (window.QC && window.QC.CommandPalette) || function () { return null; };

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
  // Tier C P1-2: AbortError detector. Components that issue fetches inside
  // useEffect should swallow these silently — they fire on unmount when the
  // controller is aborted and are EXPECTED, not real errors.
  function isAbortError(e) {
    return !!(e && (e.name === "AbortError" || (e.message && e.message.indexOf("aborted") !== -1)));
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
    backtest_gates: "/api/ops/backtest_gates",
    llm_stats: "/api/ops/llm_stats",
    mcp: "/api/ops/mcp",
    risk_gates: "/api/ops/risk_gates",
    sentiment: "/api/ops/sentiment",
    // stocks_sentiment endpoint removed 2026-05-11 — see ops_routes.py
    // comment. Shark Briefing card (data-num 13c) is the source of truth
    // for per-symbol stocks sentiment via Shark's analyst pipeline.
    shark_briefing: "/api/ops/shark_briefing",
    // Shark BEAR_VOLATILE paper-mode override verifier — written by
    // ~/.hermes/scripts/shark_override_verify.sh after each market_open
    // run. Surfaces the SharkOverrideHealthLive card under TodayScoreboard.
    shark_override_health: "/api/ops/shark_override_health",
    // ModelForge weekly LoRA training pipeline status — surfaces the
    // WeeklyTrainingLive card under TodayScoreboard. Degrades soft when
    // model-forge is offline (card still renders with local-only fields).
    weekly_training: "/api/ops/weekly_training",
    // Per-pair TFT model.zip validation status — surfaces the
    // TrainingHealthLive card next to WeeklyTrainingLive. Powered by
    // pair_dictionary.json + the post-write validation gate. Red
    // rows when a pair is quarantined (stub/missing); amber when stale.
    training_health: "/api/ops/training_health",
    // LLM activity feed — last N calls from stocks/memory/llm-calls.jsonl
    // with summary aggregates (tokens, avg latency, ollama % share).
    // Default page is metadata-only (include_text=0) so the polling payload
    // stays small even with SHARK_LLM_LOG_FULL_TEXT=1 lines on disk.
    llm_calls: "/api/ops/llm_calls?limit=50",
  };
  const SLOW_ENDPOINTS = {
    ept_champion: { url: "/api/ops/mcp/get_champion_genome", method: "POST", body: {} },
    training: "/api/ops/training",
    readiness: "/api/ops/readiness",
    regime_config: "/api/ops/regime_config",
    slack_preview: "/api/ops/slack_preview",
    tools: "/api/ops/tools",
    // Single source of truth for the trading universe — backed by
    // user_data/universe.json. Frontend reads this so the Hero strip
    // RegimeCellLive grids reflect whatever's currently configured.
    universe: "/api/universe",
  };

  function useOpsData() {
    const [state, setState] = useState({});
    const stateRef = useRef(state);
    stateRef.current = state;
    // Tier C P1-2: AbortController per useEffect tick so unmounting the
    // page or rotating refresh cycles cancels in-flight fetches cleanly.
    // INVARIANT: every fetch issued from this hook must pass through the
    // current ctrlRef.current.signal so unmount aborts them. Without this
    // 22 in-flight fetches leak per unmount (page tab switch, hot reload).
    const ctrlRef = useRef(null);

    // Tier C P1-3: batch many parallel fetches into one setState per
    // refresh-cycle. Previously each of the 22 FAST_ENDPOINTS' fetches
    // resolved at different times and each called setState → 22 ops-page
    // renders per 10s tick. Now resolves are accumulated in a local object
    // and flushed in a single setState after Promise.allSettled finishes
    // for that batch.
    //
    // INVARIANT: setState must be called AT MOST ONCE per refetchFast /
    // refetchSlow call (and at most twice total per tick when both fast +
    // slow fire on mount). Future edits: do not move setState back inside
    // the per-fetch then/catch.
    //
    // Tier C P1-9: per-endpoint request token (stale-while-revalidate
    // guard). Two refreshes for the same key can be in-flight at once
    // (10 s tick fires while a previous tick's call is still pending).
    // Without a guard, the slower call's result silently overwrites the
    // faster (and fresher) call's result whenever it happens to land
    // second. Each call now captures an incrementing per-key token and
    // its result is dropped during flushBatch if a newer request has
    // since been issued for the same key.
    //
    // INVARIANT: only the latest-token response for a key may write that
    // key's slot. Future edits MUST keep the token comparison.
    const tokensRef = useRef({});

    const buildOne = useCallback((key, urlOrSpec, signal) => {
      const isSpec = typeof urlOrSpec === "object";
      const url = isSpec ? urlOrSpec.url : urlOrSpec;
      const opts = isSpec
        ? { method: urlOrSpec.method || "GET",
            headers: { "Content-Type": "application/json" },
            body: urlOrSpec.method === "POST" ? JSON.stringify(urlOrSpec.body || {}) : undefined,
            signal }
        : { signal };
      const myToken = (tokensRef.current[key] || 0) + 1;
      tokensRef.current[key] = myToken;
      return safeJsonFetch(url, opts)
        .then(env => ({ key, ok: true, env, token: myToken }))
        .catch(err => ({ key, ok: false, err, token: myToken }));
    }, []);

    const flushBatch = useCallback((results) => {
      const patch = {};
      const now = new Date().toISOString();
      let touched = 0;
      results.forEach(rv => {
        // Skip the resolved-but-aborted case
        if (!rv) return;
        const { key, ok, env, err, token } = rv;
        // P1-9: drop stale responses — a newer request for this key has
        // already been issued (and may even have landed first).
        if (token !== tokensRef.current[key]) return;
        if (ok) {
          patch[key] = env;
          patch[key + "_fetched_at"] = now;
          patch[key + "_error"] = null;
          touched++;
        } else {
          if (isAbortError(err)) return;
          patch[key + "_fetched_at"] = now;
          patch[key + "_error"] = String(err && err.message || err);
          touched++;
        }
      });
      if (touched === 0) return;
      setState(s => Object.assign({}, s, patch));
    }, []);

    const refetchFast = useCallback(() => {
      const sig = ctrlRef.current && ctrlRef.current.signal;
      const ps = Object.entries(FAST_ENDPOINTS).map(([k, u]) => buildOne(k, u, sig));
      return Promise.allSettled(ps).then(arr =>
        flushBatch(arr.map(s => s.status === "fulfilled" ? s.value : null))
      );
    }, [buildOne, flushBatch]);
    const refetchSlow = useCallback(() => {
      const sig = ctrlRef.current && ctrlRef.current.signal;
      const ps = Object.entries(SLOW_ENDPOINTS).map(([k, spec]) => buildOne(k, spec, sig));
      return Promise.allSettled(ps).then(arr =>
        flushBatch(arr.map(s => s.status === "fulfilled" ? s.value : null))
      );
    }, [buildOne, flushBatch]);

    useEffect(() => {
      const ctrl = new AbortController();
      ctrlRef.current = ctrl;
      refetchFast();
      refetchSlow();
      const ifast = setInterval(refetchFast, 10_000);
      const islow = setInterval(refetchSlow, 60_000);
      return () => { clearInterval(ifast); clearInterval(islow); ctrl.abort(); };
    }, [refetchFast, refetchSlow]);

    return { state, refetchFast, refetchSlow };
  }

  // ─────────────── scoreboard metrics (card 00 + DD ribbon + KillBar) ─────
  function computeScoreboardMetrics(data) {
    const cpSlot = slotState(data, "combined_portfolio");
    const cp = envelopeData(cpSlot.env) || {};
    const tr = envelopeData(data.trades_risk) || {};
    const stocks = envelopeData(data.stocks) || {};
    const wheelOpen = ((stocks.wheel || {}).open_positions || []).length;
    const equity = Number(cp.total_equity ?? 0);
    const peak = Number(cp.combined_peak_equity ?? equity);
    const closedPnl = Number(cp.day_pnl_usd ?? 0);
    const srcs = cp.sources || {};
    const cryptoUnrl = Number(srcs.crypto_unrealised_pnl ?? 0);
    const stocksEq = Number(cp.stocks_equity ?? 0);
    const stocksPeak = Number(cp.stocks_peak_equity ?? stocksEq);
    const stocksMove = stocksEq - stocksPeak;
    const liveDayPnl = closedPnl + cryptoUnrl + stocksMove;
    const baseCap = peak > 0 ? peak : equity;
    const liveDayPct = baseCap > 0 ? (liveDayPnl / baseCap) * 100 : 0;
    const rgSlot = slotState(data, "risk_gates");
    const rgEnv = envelopeData(rgSlot.env) || {};
    const rgNested = rgEnv.risk_gates || rgEnv.resolved || {};
    const rgSrc = (srcs.risk_gates) || rgNested || {};
    const haltFrac = Number(rgSrc.daily_loss_halt_pct ?? 0.03);
    const closedToday = Number(tr.closed_today ?? 0);
    const openCrypto = Number(tr.open_count ?? 0);
    const totalOpen = openCrypto + wheelOpen;
    const dd = Math.abs(Number(cp.combined_drawdown_pct ?? 0));
    return {
      cpSlot, equity, peak, closedPnl, cryptoUnrl, stocksMove, liveDayPnl, liveDayPct,
      haltFrac, closedToday, totalOpen, openCrypto, wheelOpen, dd, baseCap,
    };
  }

  function sentimentRadarPoints(env) {
    const cx = 60;
    const cy = 60;
    const R = 44;
    const clamp01 = (x) => {
      if (x == null || isNaN(x)) return 0.5;
      return Math.max(0, Math.min(1, x));
    };
    const deep = Number(env.deep_score);
    const fast = Number(env.fast_score);
    const fg = env.fear_greed != null ? Number(env.fear_greed) / 100 : null;
    let ag = env.community_score != null ? Number(env.community_score) : null;
    if (ag == null || isNaN(ag)) {
      ag = env.agreement === true ? 0.82 : 0.18;
    }
    const uDeep = clamp01((deep + 1) / 2);
    const uFast = clamp01((fast + 1) / 2);
    const uFg = clamp01(fg != null ? fg : 0.5);
    const uAg = clamp01(ag);
    const vals = [uDeep, uFast, uFg, uAg];
    const pts = [];
    for (let i = 0; i < 4; i++) {
      const ang = (-Math.PI / 2) + (i * Math.PI / 2);
      const rad = R * vals[i];
      pts.push((cx + rad * Math.cos(ang)).toFixed(1) + "," + (cy + rad * Math.sin(ang)).toFixed(1));
    }
    return { points: pts.join(" "), labels: ["Deep", "Fast", "F&G", "Agree"] };
  }

  // ─────────────── TODAY SCOREBOARD — single-card at-a-glance summary ─────
  // Operator's stated need (2026-05-11): "top right corner, daily P&L,
  // capital, trades done, all the things". This card distills 6 numbers
  // that answer "where are we right now" without scrolling.
  function TodayScoreboard({ data }) {
    const m = computeScoreboardMetrics(data);
    const {
      cpSlot, equity, peak, closedPnl, cryptoUnrl, stocksMove, liveDayPnl, liveDayPct,
      haltFrac, closedToday, totalOpen, openCrypto, wheelOpen, dd,
    } = m;

    const dayCls = liveDayPnl >= 0 ? "up" : "down";
    const ddCls = dd >= 8 ? "down" : dd >= 5 ? "warn" : "up";
    const regimeEnv = envelopeData(data.regime) || {};
    const regimeLabel = regimeEnv.current ? String(regimeEnv.current) : "—";
    const regimeTint = (() => {
      const r = regimeEnv.current ? String(regimeEnv.current) : "";
      switch (r) {
        case "trending_up": return "var(--up)";
        case "trending_down": return "var(--down)";
        case "mean_reverting": return "var(--warn)";
        case "high_volatility": return "var(--accent)";
        default: return "var(--line-2)";
      }
    })();

    const stat = (lbl, valNode) => h("div", { style: { display: "flex", flexDirection: "column", gap: 2, minWidth: 100 } },
      h("div", { className: "dim2 mono", style: { fontSize: "var(--t-2xs)", letterSpacing: ".08em", textTransform: "uppercase" } }, lbl),
      valNode
    );

    return h(Card, {
      num: "00", title: "Today · scoreboard",
      sub: "live · realized + unrealized · refreshes every 10s",
      className: "v3-card-scoreboard",
      right: cardRight(cpSlot.fetchedAt,
        h("span", { className: "pill " + dayCls, style: { height: 18 } },
          h("span", { className: "dot " + dayCls + (liveDayPnl === 0 ? "" : " pulse") }),
          " ", (liveDayPct >= 0 ? "+" : "") + liveDayPct.toFixed(2) + "% live"))
    },
      h(DDRibbon, { dayPct: liveDayPct, haltPct: haltFrac }),
      h("div", { className: "v3-score-hero" },
        h("div", { className: "dim2 mono", style: { fontSize: "var(--t-2xs)", letterSpacing: ".12em" } }, "LIVE DAY P&L"),
        h("div", { className: dayCls, style: { lineHeight: 1 } },
          h(NumberRoll, {
            value: liveDayPnl,
            decimals: 2,
            prefix: "$",
            className: "v3-hero-num",
          })
        ),
        h("div", { className: "v3-score-hero-meta" },
          stat("Capital", h("span", { className: "v3-num " + "mono", style: { fontSize: "var(--t-lg)", fontWeight: 500 } }, "$" + fmtUSD(equity))),
          stat("Realized today", h("span", { className: cls("v3-num", "mono", closedPnl >= 0 ? "up" : "down"), style: { fontSize: "var(--t-md)", fontWeight: 500 } },
            (closedPnl >= 0 ? "+$" : "−$") + fmtUSD(Math.abs(closedPnl)))),
          stat("Unrealized", h("span", { className: cls("v3-num", "mono", (cryptoUnrl + stocksMove) >= 0 ? "up" : "down"), style: { fontSize: "var(--t-md)", fontWeight: 500 } },
            (cryptoUnrl + stocksMove >= 0 ? "+$" : "−$") + fmtUSD(Math.abs(cryptoUnrl + stocksMove)))),
          stat("Drawdown", h("span", { className: cls("v3-num", "mono", ddCls), style: { fontSize: "var(--t-md)", fontWeight: 500 } }, dd.toFixed(2) + "%")),
          stat("Peak", h("span", { className: "v3-num mono", style: { fontSize: "var(--t-md)", fontWeight: 500 } }, "$" + fmtUSD(peak))),
          stat("Open", h("span", { className: "v3-num mono", style: { fontSize: "var(--t-md)", fontWeight: 500 } }, totalOpen + " (" + openCrypto + "C + " + wheelOpen + "S)")),
          stat("Closed today", h("span", { className: "v3-num mono", style: { fontSize: "var(--t-md)", fontWeight: 500 } }, String(closedToday)))
        ),
        h("div", { className: "v3-regime-edge", style: { background: regimeTint }, title: "BTC regime" }),
        h("div", { className: "mono dim", style: { fontSize: "var(--t-2xs)", marginTop: 4 } }, "REGIME · ", h("span", { className: "v3-num" }, regimeLabel))
      )
    );
  }

  // ─────────────── SHARK OVERRIDE HEALTH — BEAR_VOLATILE verifier ────────
  // Surfaces ~/.hermes/scripts/shark_override_verify.sh output via
  // /api/ops/shark_override_health. Operator-glance: "did the 0.85
  // confidence override fire today, or has it been stalled for N runs?"
  //
  // Color rules (per spec):
  //   green  — verifier status="healthy"
  //   yellow — stalled_runs >= 1
  //   red    — stalled_runs >= 3
  //
  // PRE-CONDITION: the override must have been observed firing at least
  // once on this paper account before the verifier flips to genuine
  // healthy. Fresh accounts will show "healthy — no candidates" or
  // "healthy — override never fired yet" until the first BEAR-regime
  // candidate clears the 0.85 floor. See HANDOFF.md.
  function SharkOverrideHealthLive({ data }) {
    const slot = slotState(data, "shark_override_health");
    const env = envelopeData(slot.env) || {};

    if (slot.phase === "down") {
      return h(Card, {
        num: "00b", title: "Shark · BEAR_VOLATILE override health",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt),
      }, h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 }));
    }
    if (slot.phase === "loading") {
      return h(Card, {
        num: "00b", title: "Shark · BEAR_VOLATILE override health",
        sub: "loading…",
        right: cardRight(slot.fetchedAt),
      }, h(LoadingState));
    }

    const status = String(env.status || "unknown").toLowerCase();
    const stalled = Number(env.stalled_runs || 0);
    const regime = env.regime || "—";
    const evald = Number(env.candidates_evaluated || 0);
    const passed = Number(env.candidates_passing_override || 0);
    const trades = Number(env.trades_placed || 0);
    const lastTrade = env.last_trade_at;
    const overrideExpected = !!env.override_expected;
    const overrideApplied = !!env.override_applied;

    // Color mapping per spec
    let pillCls, dot, label;
    if (stalled >= 3 || status === "stalled") {
      pillCls = "down"; dot = "down"; label = "STALLED";
    } else if (stalled >= 1 || status === "degraded") {
      pillCls = "warn"; dot = "warn"; label = "DEGRADED";
    } else if (status === "unknown") {
      pillCls = "info"; dot = "info"; label = "UNKNOWN";
    } else {
      pillCls = "up"; dot = "up"; label = "HEALTHY";
    }

    const reason = env.reason || "—";
    const checkedAt = env.checked_at;
    const heroClsName = cls("v3-shark-hero", "v3-num", pillCls);

    return h(Card, {
      num: "00b", title: "Shark · BEAR_VOLATILE override health",
      sub: "verifier · cron 09:45 ET · " + (regime || "—"),
      className: "v3-shark-card",
      right: cardRight(slot.fetchedAt,
        h("span", { className: "pill " + pillCls, style: { height: 18 } },
          h("span", { className: "dot " + dot + (label === "HEALTHY" ? "" : " pulse") }),
          " ", label))
    },
      h("div", { className: "v3-shark-row" },
        h("span", { className: heroClsName, title: label }, stalled >= 3 ? String(stalled) : label),
        h("div", { style: { display: "flex", flexDirection: "column", gap: 4, flex: 1, minWidth: 200 } },
          h("div", { style: { display: "flex", flexWrap: "wrap", gap: "var(--s-4)", alignItems: "center" } },
            h("span", { className: "v3-num mono", style: { fontSize: "var(--t-sm)" } }, "cand " + evald + " · pass " + passed + " · trades " + trades),
            h("span", { className: "v3-num mono dim", style: { fontSize: "var(--t-2xs)" } },
              "exp " + (overrideExpected ? "y" : "n") + " · app " + (overrideApplied ? "y" : "n"))),
          h("div", { className: "dim mono v3-num", style: { fontSize: "var(--t-2xs)", lineHeight: 1.35 } },
            reason,
            lastTrade ? h("span", null, " · last " + String(lastTrade).replace("T", " ").slice(0, 16)) : null,
            checkedAt ? h("span", null, " · chk " + String(checkedAt).replace("T", " ").slice(0, 16)) : null
          )
        )
      )
    );
  }

  // ─────────────── WEEKLY TRAINING — ModelForge LoRA pipeline status ────
  // Surfaces /api/ops/weekly_training. Six rows (one per trading-bot LLM
  // role) with current adapter version, last train ts, headline eval
  // score, and an eligibility badge. Two summary stats sit above the
  // table: reflections-this-week (the input to next Sunday's refresh)
  // and a Sunday 02:00 ET countdown.
  //
  // Color rules per spec:
  //   green   — adapter promoted this week (eligibility="promoted" + freshly trained)
  //   yellow  — adapter promoted earlier, score flat OR shadow run
  //   red     — adapter regressed and was rolled back
  //   gray    — no data yet (track registered, no champion)
  //
  // Degrade-soft: when model-forge is offline (data.model_forge_reachable
  // === false), the connectivity pip turns orange + we still render the
  // 6-track skeleton + the reflection count (which works purely off
  // local files).
  //
  // This card is the **viral screenshot** for the week 4 launch — keep it
  // pixel-perfect against the dYdX/Geist aesthetic the rest of the SPA
  // uses (mono numerics, no shadows, no gradients).
  function WeeklyTrainingLive({ data }) {
    const slot = slotState(data, "weekly_training");
    const env = envelopeData(slot.env) || {};
    const tracks = env.tracks || [];
    const summary = env.summary || {};
    const mfReachable = env.model_forge_reachable !== false;
    const reflections = env.reflections_this_week;
    const lessons = env.lessons_injected;

    if (slot.phase === "down") {
      return h(Card, {
        num: "00c", title: "Weekly training · LoRA adapters",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt),
      }, h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 }));
    }
    if (slot.phase === "loading") {
      return h(Card, {
        num: "00c", title: "Weekly training · LoRA adapters",
        sub: "loading…",
        right: cardRight(slot.fetchedAt),
      }, h(LoadingState));
    }

    // Connectivity pip — green when model-forge is reachable AND at least
    // one track has a champion; orange when MF is offline; soft info when
    // MF is reachable but no adapters have been trained yet (expected
    // new-system state, NOT a failure — see ops_routes.py:_envelope at
    // weekly_training endpoint).
    let pillCls, pillText;
    if (!mfReachable) {
      pillCls = "warn"; pillText = "MODEL-FORGE OFFLINE";
    } else if ((summary.n_tracks_trained || 0) === 0) {
      pillCls = "info";
      pillText = (summary.n_tracks_registered || 6) + " TRACKS READY";
    } else if ((summary.n_promoted_this_week || 0) > 0) {
      pillCls = "up"; pillText = (summary.n_promoted_this_week) + " PROMOTED THIS WEEK";
    } else {
      pillCls = "info"; pillText = (summary.n_tracks_trained || 0) + "/6 TRAINED";
    }

    return h(Card, {
      num: "00c", title: "Weekly training · LoRA adapters",
      sub: mfReachable
        ? ("model-forge @ " + (env.model_forge_url || "—")
           + " · Sun 14:00 ET refresh")
        : "model-forge offline — local-only metrics shown",
      right: cardRight(slot.fetchedAt,
        h("span", { className: "pill " + pillCls, style: { height: 18 } },
          h("span", { className: "dot " + pillCls + (pillCls === "up" ? " pulse" : "") }),
          " ", pillText))
    },
      // Summary strip — reflections this week + next training countdown.
      h(WeeklyTrainingSummary, {
        reflections: reflections,
        lessons: lessons,
        nextTrainingTs: env.next_training_ts,
        nTrained: summary.n_tracks_trained || 0,
        nRegistered: summary.n_tracks_registered || 6,
        mfReachable: mfReachable,
        mfError: env.model_forge_error,
      }),
      // Per-track table — 6 rows + a 5-column header.
      h("div", {
        style: {
          display: "grid",
          gridTemplateColumns:
            "minmax(140px, 1.6fr) minmax(150px, 1.5fr) minmax(90px, 1fr) minmax(110px, 1.2fr) minmax(70px, .8fr)",
          gap: 0,
          marginTop: "var(--s-3)",
          borderTop: "1px solid var(--line-1)",
        }
      },
        // header row
        h(F, null,
          h(WeeklyTrainingHeaderCell, { label: "Track" }),
          h(WeeklyTrainingHeaderCell, { label: "Adapter / status" }),
          h(WeeklyTrainingHeaderCell, { label: "Last train" }),
          h(WeeklyTrainingHeaderCell, { label: "Headline score" }),
          h(WeeklyTrainingHeaderCell, { label: "Examples", align: "right" }),
        ),
        // data rows — always 6, even when the array is empty (skeleton).
        tracks.map(t => h(WeeklyTrainingTrackRow, { key: t.track_id, t }))
      ),
      h("div", {
        className: "dim mono",
        style: { fontSize: "var(--t-2xs)", padding: "var(--s-2) 0 0",
                 letterSpacing: ".06em" }
      },
        mfReachable
          ? "promoted = Pareto-dominant on faithfulness + hit-rate · rolled back = regressed vs prior champion"
          : "showing local-only metrics · model-forge will populate adapter rows once :8000 is up")
    );
  }

  function WeeklyTrainingHeaderCell({ label, align }) {
    return h("div", {
      className: "dim2 mono",
      style: {
        fontSize: "var(--t-2xs)", letterSpacing: ".08em",
        textTransform: "uppercase",
        padding: "var(--s-2) var(--s-2)",
        textAlign: align || "left",
        borderBottom: "1px solid var(--line-1)",
      }
    }, label);
  }

  function WeeklyTrainingSummary({ reflections, lessons, nextTrainingTs, nTrained, nRegistered, mfReachable, mfError }) {
    // Countdown to next Sunday 14:00 ET (recomputed on each render via the
    // 1s-tick from RetryCountdown sibling — we use a similar interval here
    // so the "Next training" cell stays live).
    const [, force] = useState(0);
    useEffect(() => {
      const iv = setInterval(() => force(n => n + 1), 30_000);
      return () => clearInterval(iv);
    }, []);

    let countdown = "—";
    if (nextTrainingTs) {
      const ms = new Date(nextTrainingTs).getTime() - Date.now();
      if (ms > 0) {
        const days = Math.floor(ms / 86_400_000);
        const hours = Math.floor((ms % 86_400_000) / 3_600_000);
        const mins = Math.floor((ms % 3_600_000) / 60_000);
        if (days > 0) countdown = days + "d " + String(hours).padStart(2, "0") + "h";
        else if (hours > 0) countdown = hours + "h " + String(mins).padStart(2, "0") + "m";
        else countdown = mins + "m";
      } else {
        countdown = "now";
      }
    }

    const stat = (lbl, val, cls, hint) => h("div", {
      style: { display: "flex", flexDirection: "column", gap: 2, minWidth: 120 }
    },
      h("div", {
        className: "dim2 mono",
        style: { fontSize: "var(--t-2xs)", letterSpacing: ".08em", textTransform: "uppercase" }
      }, lbl),
      h("div", {
        className: "num " + (cls || ""),
        style: {
          fontSize: "var(--t-lg)", fontFamily: "var(--mono)",
          fontWeight: 500, fontVariantNumeric: "tabular-nums",
        }
      }, val),
      hint ? h("div", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, hint) : null
    );

    return h("div", {
      style: {
        display: "flex", flexWrap: "wrap", gap: "var(--s-5)",
        alignItems: "baseline", paddingBottom: "var(--s-3)",
      }
    },
      stat("Reflections this week",
           reflections == null ? "—" : String(reflections),
           reflections > 0 ? "up" : "",
           "from decisions.md"),
      stat("Lessons injected",
           lessons == null ? "n/a" : String(lessons),
           lessons > 0 ? "up" : "",
           lessons == null ? "logger not wired" : "get_past_context()"),
      stat("Tracks trained",
           nTrained + " / " + nRegistered,
           nTrained > 0 ? "up" : "",
           mfReachable ? "model-forge live" : "model-forge offline"),
      stat("Next training",
           countdown,
           "info",
           "Sunday 14:00 ET")
    );
  }

  function WeeklyTrainingTrackRow({ t }) {
    const elig = (t && t.eligibility) || "no-data";
    // Color mapping per spec.
    let pillCls, pillDot, pillText;
    if (elig === "promoted") { pillCls = "up";   pillDot = "up";   pillText = "PROMOTED"; }
    else if (elig === "shadow")    { pillCls = "warn"; pillDot = "warn"; pillText = "SHADOW"; }
    else if (elig === "regressed") { pillCls = "down"; pillDot = "down"; pillText = "ROLLED BACK"; }
    else                           { pillCls = "info"; pillDot = "info"; pillText = "NO DATA"; }

    const cell = (kids, extra) => h("div", {
      style: Object.assign({
        padding: "var(--s-2) var(--s-2)",
        borderBottom: "1px solid var(--line-1)",
        fontSize: "var(--t-xs)",
        display: "flex", alignItems: "center", gap: 6,
        minHeight: 32,
      }, extra || {})
    }, kids);

    // Track name + role.
    const role = t.role || t.track_id;
    const trackCell = cell(
      h(F, null,
        h("strong", { style: { color: "var(--fg-1)" } }, role),
        h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)", marginLeft: 6 } },
          t.track_id)
      )
    );

    // Adapter version + eligibility badge.
    const ver = t.current_adapter_version || (t.current_adapter ? "—" : "");
    const adapterCell = cell(
      h(F, null,
        h("span", { className: "mono",
                    style: { color: ver ? "var(--fg-1)" : "var(--fg-4)",
                             fontFamily: "var(--mono)" } },
          ver || "—"),
        h("span", { className: "pill " + pillCls,
                    style: { height: 16, marginLeft: "auto" } },
          h("span", { className: "dot " + pillDot }), " ", pillText)
      ),
      { gap: 6, justifyContent: "flex-start" }
    );

    // Last train timestamp — relative if recent, absolute otherwise.
    const trainCell = cell(
      h(WeeklyTrainingRelTime, { ts: t.last_train_ts })
    );

    // Headline score (whatever the role's headline metric is).
    const score = t.headline_score;
    const scoreStr = (score == null || isNaN(score)) ? "—"
      : (typeof score === "number" ? score.toFixed(3) : String(score));
    const scoreCls = score == null ? "" : (score >= 0.7 ? "up" : score >= 0.5 ? "" : "warn");
    const scoreCell = cell(
      h(F, null,
        h("span", { className: "num mono " + scoreCls,
                    style: { fontFamily: "var(--mono)", fontVariantNumeric: "tabular-nums" } },
          scoreStr),
        h("span", { className: "dim mono",
                    style: { fontSize: "var(--t-2xs)", marginLeft: 6, opacity: 0.7 } },
          t.headline_metric || "")
      )
    );

    // Examples trained this week.
    const ex = Number(t.examples_trained_this_week || 0);
    const examplesCell = cell(
      h("span", { className: "num mono " + (ex > 0 ? "up" : ""),
                  style: { fontFamily: "var(--mono)",
                           fontVariantNumeric: "tabular-nums",
                           width: "100%", textAlign: "right" } },
        ex > 0 ? String(ex) : "—"),
      { justifyContent: "flex-end" }
    );

    return h(F, null,
      trackCell, adapterCell, trainCell, scoreCell, examplesCell);
  }

  function WeeklyTrainingRelTime({ ts }) {
    // Tick once a minute so "2d ago" updates without a full refetch.
    const [, force] = useState(0);
    useEffect(() => {
      const iv = setInterval(() => force(n => n + 1), 60_000);
      return () => clearInterval(iv);
    }, []);
    if (!ts) {
      return h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, "—");
    }
    const now = Date.now();
    const t = new Date(ts).getTime();
    if (!isFinite(t)) {
      return h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, "—");
    }
    const ms = now - t;
    let rel;
    if (ms < 60_000) rel = "just now";
    else if (ms < 3_600_000) rel = Math.floor(ms / 60_000) + "m ago";
    else if (ms < 86_400_000) rel = Math.floor(ms / 3_600_000) + "h ago";
    else rel = Math.floor(ms / 86_400_000) + "d ago";
    // Sunday short-form for promoted-this-week adapters.
    let abs = "";
    try {
      const dt = new Date(ts);
      const day = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"][dt.getUTCDay()];
      abs = day + " " + String(dt.getUTCHours()).padStart(2, "0")
            + ":" + String(dt.getUTCMinutes()).padStart(2, "0");
    } catch (e) { /* leave abs empty */ }
    return h("span", { style: { display: "inline-flex", flexDirection: "column", lineHeight: 1.15 } },
      h("span", { className: "mono", style: { color: "var(--fg-1)" } }, rel),
      abs ? h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, abs) : null
    );
  }

  // ─────────────── TRAINING HEALTH — per-pair TFT model.zip validation ───────────────
  //
  // Sits next to WeeklyTrainingLive. One row per pair in pair_dictionary.json.
  // Backed by /api/ops/training_health on the 10s fast-poll tick. Red rows
  // when the model is a stub or missing; amber when stale (>72h). The
  // existing CSS tokens (up / warn / down + dim, line-1) carry the colour
  // semantics so this card matches the rest of the dashboard automatically.
  function TrainingHealthLive({ data }) {
    const slot = slotState(data, "training_health");
    const env = envelopeData(slot.env) || {};
    const pairs = env.pairs || [];
    const counts = env.counts || {};
    const staleThreshold = env.stale_hours_threshold || 72;

    if (slot.phase === "down") {
      return h(Card, {
        num: "00d", title: "TFT model health · per pair",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt),
      }, h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 }));
    }
    if (slot.phase === "loading") {
      return h(Card, {
        num: "00d", title: "TFT model health · per pair",
        sub: "loading…",
        right: cardRight(slot.fetchedAt),
      }, h(LoadingState));
    }

    const okN = counts.ok || 0;
    const stubN = counts.stub || 0;
    const missingN = counts.missing || 0;
    const staleN = counts.stale || 0;
    const errN = counts.error || 0;
    const badN = stubN + missingN + errN;

    // Pip — same vocabulary as WeeklyTrainingLive.
    let pillCls, pillText;
    if (badN > 0) {
      pillCls = "down"; pillText = badN + " QUARANTINED";
    } else if (staleN > 0) {
      pillCls = "warn"; pillText = staleN + " STALE";
    } else if (pairs.length === 0) {
      pillCls = "info"; pillText = "NO PAIRS YET";
    } else {
      pillCls = "up"; pillText = okN + "/" + pairs.length + " HEALTHY";
    }

    return h(Card, {
      num: "00d", title: "TFT model health · per pair",
      sub: "validates model.zip on every poll · stale = > " + Math.round(staleThreshold) + "h",
      right: cardRight(slot.fetchedAt,
        h("span", { className: "pill " + pillCls, style: { height: 18 } },
          h("span", { className: "dot " + pillCls + (pillCls === "up" ? " pulse" : "") }),
          " ", pillText))
    },
      h("div", {
        style: {
          display: "grid",
          gridTemplateColumns:
            "minmax(110px, 1.3fr) minmax(80px, .9fr) minmax(90px, 1fr) minmax(70px, .8fr) minmax(90px, 1fr)",
          gap: 0,
          marginTop: "var(--s-2)",
          borderTop: "1px solid var(--line-1)",
        }
      },
        h(F, null,
          h(WeeklyTrainingHeaderCell, { label: "Pair" }),
          h(WeeklyTrainingHeaderCell, { label: "Status" }),
          h(WeeklyTrainingHeaderCell, { label: "Last train" }),
          h(WeeklyTrainingHeaderCell, { label: "Age" }),
          h(WeeklyTrainingHeaderCell, { label: "Size", align: "right" }),
        ),
        pairs.length === 0
          ? h("div", {
              className: "dim mono",
              style: {
                gridColumn: "span 5", padding: "var(--s-3)",
                fontSize: "var(--t-xs)", textAlign: "center",
              }
            }, "pair_dictionary.json not yet written — bot is still in warm-up")
          : pairs.map(p => h(TrainingHealthRow, { key: p.pair, p })),
      ),
      h("div", {
        className: "dim mono",
        style: { fontSize: "var(--t-2xs)", padding: "var(--s-2) 0 0",
                 letterSpacing: ".06em" }
      },
        badN > 0
          ? "stub = size < 1 MB or no data.pkl · missing = trained_ts = 0 (last save failed) · investigate before next retrain"
          : staleN > 0
            ? "stale rows have not retrained in the last " + Math.round(staleThreshold) + "h — check freqai live_retrain_hours"
            : "all artifacts pass: size > 1 MB · data.pkl present · tensor blobs > 0"),
      // Fix 6: TFT-blind fallback footer line. Shown only when at least
      // one pair is eligible so the operator knows their config setting
      // is actively governing live trading behaviour.
      (function() {
        const tbf = env.tft_blind_fallback || {};
        const eligible = tbf.eligible_count || 0;
        if (eligible <= 0) return null;
        const mult = Math.round((tbf.position_size_multiplier || 0.5) * 100);
        const active = tbf.active_count || 0;
        // Paper-mode default 2026-05-12+: tft_blind_fallback.enabled is now
        // ON by default. When ON, banner names how many pairs are actively
        // trading via the BollingerRSI MR signal — no "DARK" wording. When
        // OFF (operator override), banner warns that quarantined pairs are
        // dark until retrain.
        const cls = tbf.enabled ? "warn" : "down";
        const txt = tbf.enabled
          ? "tft-blind fallback ON · " + active + " pair(s) trading on BollingerRSI MR at " + mult + "% size"
          : "tft-blind fallback OFF · " + eligible + " eligible pair(s) DARK until next TFT retrain · flip strategy_overrides.tft_blind_fallback.enabled=true to trade them at " + mult + "% size";
        return h("div", {
          className: "mono " + cls,
          style: { fontSize: "var(--t-2xs)", padding: "var(--s-2) 0 0",
                   letterSpacing: ".06em" }
        }, txt);
      })()
    );
  }

  function TrainingHealthRow({ p }) {
    // Red for stub/missing/error, amber for stale, default for ok.
    const status = p.status || "ok";
    let rowCls, statusText, statusCls;
    if (status === "stub") {
      rowCls = "down"; statusText = "STUB"; statusCls = "down";
    } else if (status === "missing") {
      rowCls = "down"; statusText = "MISS"; statusCls = "down";
    } else if (status === "error") {
      rowCls = "down"; statusText = "ERR"; statusCls = "down";
    } else if (status === "stale") {
      rowCls = "warn"; statusText = "STALE"; statusCls = "warn";
    } else {
      rowCls = "up"; statusText = "OK"; statusCls = "up";
    }

    const cell = (kids, extra) => h("div", {
      style: Object.assign({
        padding: "var(--s-2) var(--s-2)",
        borderBottom: "1px solid var(--line-1)",
        fontSize: "var(--t-xs)",
        display: "flex", alignItems: "center", gap: 6,
        minHeight: 30,
      }, extra || {})
    }, kids);

    // Pair name. On stub/missing the cell stays default colour but the
    // status pill carries the red. When TFT-blind fallback is eligible
    // for this pair, append a small chip:
    //   [blind] (warn) → fallback ACTIVE — pair is trading on BollingerRSI
    //   [dark]  (down) → fallback DISABLED — pair is no-op until retrain
    let blindChip = null;
    if (p.tft_blind_active) {
      blindChip = h("span", {
        className: "pill warn",
        style: { height: 14, fontSize: "var(--t-2xs)", marginLeft: 4 },
        title: "TFT-blind fallback ACTIVE — trading on BollingerRSI MR signal at degraded sizing. Will auto-disable on next successful TFT retrain."
      },
        h("span", { className: "dot warn" }),
        " blind"
      );
    } else if (p.tft_blind_eligible) {
      blindChip = h("span", {
        className: "pill down",
        style: { height: 14, fontSize: "var(--t-2xs)", marginLeft: 4 },
        title: "Eligible for TFT-blind fallback but operator has set strategy_overrides.tft_blind_fallback.enabled=false (paper-mode default is true). Pair is DARK until the next successful TFT retrain."
      },
        h("span", { className: "dot down" }),
        " dark"
      );
    }
    const pairCell = cell([
      h("span", {
        className: "mono",
        style: { color: "var(--fg-1)", fontFamily: "var(--mono)" },
        title: p.reason || ""
      }, p.pair),
      blindChip,
    ].filter(Boolean));

    const statusCell = cell(
      h("span", {
        className: "pill " + statusCls,
        style: { height: 16, fontSize: "var(--t-2xs)" },
        title: p.reason || ""
      },
        h("span", { className: "dot " + statusCls }),
        " ", statusText)
    );

    // Last train — ISO short time. Same component used by the weekly card.
    const lastTs = p.last_train_ts ? (p.last_train_ts * 1000) : null;
    let trainText;
    if (lastTs) {
      try {
        const dt = new Date(lastTs);
        trainText = String(dt.getUTCHours()).padStart(2, "0")
                  + ":" + String(dt.getUTCMinutes()).padStart(2, "0")
                  + " UTC";
      } catch (_) {
        trainText = "—";
      }
    } else {
      trainText = "never";
    }
    const trainCell = cell(
      h("span", {
        className: "mono " + (lastTs ? "" : "dim"),
        style: { fontFamily: "var(--mono)", fontSize: "var(--t-xs)" }
      }, trainText)
    );

    // Age in hours, formatted with the same helper as the rest of the page.
    const ageText = p.age_hours == null ? "—" : durToHM(p.age_hours);
    const ageCls = (status === "stale") ? "warn" : "";
    const ageCell = cell(
      h("span", {
        className: "mono " + ageCls,
        style: {
          fontFamily: "var(--mono)",
          fontVariantNumeric: "tabular-nums",
          fontSize: "var(--t-xs)",
        }
      }, ageText)
    );

    // Size — bytes -> MB / KB with one decimal. Stub artifacts show in red.
    const sz = Number(p.zip_size_bytes || 0);
    let szText;
    if (!sz) szText = "—";
    else if (sz < 1024) szText = sz + " B";
    else if (sz < 1_000_000) szText = (sz / 1024).toFixed(0) + " KB";
    else szText = (sz / 1_000_000).toFixed(1) + " MB";
    const szCls = (status === "stub" && sz > 0 && sz < 10000) ? "down" : "";
    const sizeCell = cell(
      h("span", {
        className: "mono " + szCls,
        style: {
          fontFamily: "var(--mono)",
          fontVariantNumeric: "tabular-nums",
          fontSize: "var(--t-xs)",
          width: "100%", textAlign: "right",
        }
      }, szText),
      { justifyContent: "flex-end" }
    );

    return h(F, null, pairCell, statusCell, trainCell, ageCell, sizeCell);
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
            h(RegimeCellLive, {
              venue: "CRYPTO", sym: "BTC",
              env: data.regime, fetchedAt: data.regime_fetched_at,
              sparksData: envelopeData(data.sparklines),
              symbols: (data.universe && data.universe.crypto && data.universe.crypto.pairs) || [],
            }),
            h(RegimeCellLive, {
              venue: "STOCKS", sym: "SPY",
              env: data.stock_regime, fetchedAt: data.stock_regime_fetched_at,
              sparksData: envelopeData(data.stocks_sparklines),
              symbols: (data.universe && data.universe.stocks && data.universe.stocks.dashboard_basket) || [],
            })
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

  function RegimeCellLive({ venue, sym, env, fetchedAt, sparksData, symbols }) {
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

    // Per-symbol mini-rows — operator (2026-05-11 PM): "we should see ALL
    // stocks not just SPY". The regime label (BTC/SPY) is still the lead
    // macro indicator since the HMM trains on the lead instrument only,
    // but render every symbol's current price + day-% so the operator
    // sees the actual book under the regime header.
    const rows = [];
    if (sparksData && symbols && Array.isArray(symbols)) {
      const sparkPairs = sparksData.pairs || sparksData.symbols || {};
      for (const s of symbols) {
        // crypto key is "BTC/USD"; stocks key is "BTC"
        const info = sparkPairs[s] || sparkPairs[s.toUpperCase()] || {};
        const closes = info.closes || [];
        const cur_px = Number(info.current ?? closes[closes.length - 1] ?? 0);
        const pct = Number(info.pct_24h ?? info.pct_session ?? 0);
        if (!cur_px && !closes.length) continue;
        rows.push({ sym: s.split("/")[0], px: cur_px, pct: pct });
      }
    }
    const pxFmt = (v) => v >= 1000 ? "$" + v.toLocaleString("en-US", { maximumFractionDigits: 0 })
                       : v >= 1     ? "$" + v.toFixed(2)
                       :              "$" + v.toFixed(4);

    return h("div", { className: "card mountin", style: { padding: "var(--s-3) var(--s-4)", justifyContent: "space-between", minHeight: 132, gap: 6 } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "var(--s-2)" } },
        h("span", { className: "metric-label" }, venue + " · " + sym),
        h("span", { className: "tb-spacer", style: { flex: 1 } }),
        h(TimeSince, { ts: fetchedAt, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        h("span", { className: "pill " + klass }, h("span", { className: "dot " + klass + " pulse" }), " ", regimeBucket)
      ),
      h("div", { style: { display: "flex", alignItems: "baseline", justifyContent: "space-between", margin: "var(--s-2) 0 var(--s-1)" } },
        h("span", { className: "num", style: { fontSize: "var(--t-xl)", letterSpacing: "-.02em" } },
          Math.round(conf * 100),
          h("span", { style: { fontSize: "var(--t-sm)", color: "var(--fg-3)" } }, "%")
        ),
        h("span", { className: "mono dim", style: { fontSize: "var(--t-2xs)" } },
          "conf · " + (dur != null ? durToHM(dur) : "—"))
      ),
      h(RegimeRibbon, { segments: segments }),
      // Per-symbol strip — 2-column grid below the regime header
      rows.length > 0 && h("div", {
        style: {
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: "2px 12px",
          marginTop: "var(--s-2)",
          fontSize: "var(--t-2xs)",
          fontFamily: "var(--mono)",
          lineHeight: 1.4,
        }
      },
        rows.map((r, i) => h("div", { key: i,
          style: { display: "flex", alignItems: "baseline", gap: 6, whiteSpace: "nowrap" } },
          h("span", { style: { color: "var(--fg-1)", fontWeight: 500, minWidth: 36 } }, r.sym),
          h("span", { className: "dim", style: { flex: 1 } }, pxFmt(r.px)),
          h("span", { className: r.pct >= 0 ? "up" : "down" },
            (r.pct >= 0 ? "+" : "") + r.pct.toFixed(2) + "%")
        ))
      )
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
    // Engine display: prefer /api/mode's engine field (post-cutover V4), then
    // freqtrade probe (legacy), else dash.
    const engineName = (mode.engine || "").toLowerCase();
    const engineLabel = engineName === "quanta_core"
      ? "quanta_core · " + ((mode.state === "running") ? "ok" : (mode.state || "—"))
      : (services.freqtrade && services.freqtrade.up) ? "freqtrade · ok"
      : (services.freqtrade ? "freqtrade · down" : "—");
    const strategyLabel = engineName === "quanta_core" ? "MeanRevBB + TrendFollow" : "EPT";
    return h("div", { className: "card mountin", style: { padding: "var(--s-3) var(--s-4)" } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "var(--s-2)" } },
        h("span", { className: "metric-label" }, "BOT STATE"),
        h("span", { className: "tb-spacer", style: { flex: 1 } }),
        h("span", { className: "pill " + klass }, h("span", { className: "dot " + klass + " pulse" }), " ", lbl)
      ),
      h("div", { style: { marginTop: 10, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "ENGINE"),    h("div", { className: "num" }, engineLabel),
        h("div", { className: "dim mono" }, "MODE"),      h("div", { className: "num" }, (mode.mode || "—") + (mode.dry_run ? " · dry" : "")),
        h("div", { className: "dim mono" }, "CHAMPION"),  h("div", { className: "num accent" }, champion + " · sh " + sharpe),
        h("div", { className: "dim mono" }, "STRATEGY"),  h("div", { className: "num" }, strategyLabel)
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

  const AT_LANE_KEYS = ["rsh", "ml", "evo", "risk", "rpt"];
  const AT_LANE_LABELS = { rsh: "RESEARCH", ml: "ML", evo: "EVO", risk: "RISK", rpt: "REPORT" };

  function AgentTimeline() {
    const hourNow = new Date().getUTCHours() + new Date().getUTCMinutes() / 60;
    const [, tick] = useState(0);
    useEffect(() => {
      const iv = setInterval(() => tick(n => n + 1), 60_000);
      return () => clearInterval(iv);
    }, []);
    void tick;

    const colorOf = (k) => ({
      rsh: "var(--info)", ml: "var(--accent)", evo: "var(--warn)",
      risk: "var(--down)", rpt: "var(--up)",
    }[k] || "var(--fg-3)");

    return h(Card, {
      num: "03", title: "Agent timeline · 24h",
      sub: "UTC · now " + String(Math.floor(hourNow)).padStart(2, "0") + ":" + String(Math.floor((hourNow % 1) * 60)).padStart(2, "0"),
      right: h("div", { className: "tb-group v3-at-legend", style: { display: "flex", gap: 6, flexWrap: "wrap" } },
        AT_LANE_KEYS.map(k => h("span", {
          key: k,
          className: "pill",
          style: { borderColor: colorOf(k), color: colorOf(k), fontSize: "var(--t-2xs)" },
        }, AT_LANE_LABELS[k]))
      ),
    },
      h("div", { className: "v3-agent-timeline" },
        h("div", { className: "v3-at-hour-ruler" },
          Array.from({ length: 25 }).map((_, hi) =>
            h("div", {
              key: hi,
              className: "v3-at-axis-tick" + (hi % 6 === 0 ? " is-major" : ""),
              style: { left: ((hi / 24) * 100) + "%" },
            })
          ),
          h("div", {
            className: "v3-at-now",
            style: { left: (hourNow / 24) * 100 + "%" },
          },
            h("span", { className: "v3-at-now-lbl mono" }, "NOW"))
        ),
        AT_LANE_KEYS.map(lk =>
          h("div", { key: lk, className: "v3-at-lane-pair" },
            h("div", { className: "v3-at-lane-title mono dim" }, AT_LANE_LABELS[lk]),
            h("div", { className: "v3-at-lane-row" },
              CRON_JOBS.filter(j => j.kind === lk).map((j, ji) => {
                const passed = j.h < hourNow;
                const gist = j.name + " — " + j.desc;
                return h("button", {
                  key: ji,
                  type: "button",
                  className: "v3-at-cron-tick",
                  style: {
                    left: (j.h / 24) * 100 + "%",
                    background: colorOf(lk),
                    opacity: passed ? 0.45 : 1,
                  },
                  title: gist,
                  "data-tt": gist,
                });
              })
            )
          )
        ),
        h("div", { className: "v3-at-foot mono dim" },
          ["00", "04", "08", "12", "16", "20", "24"].map(hh =>
            h("span", { key: hh, className: "v3-num" }, hh + ":00")
          )
        ),
        h("div", { className: "hr" }),
        h("div", { className: "v3-at-next-grid" },
          CRON_JOBS.filter(j => j.h > hourNow).slice(0, 3).map((j, i) =>
            h("div", { key: i, className: "v3-at-next-card" },
              h("div", {
                className: "tl-source mono",
                style: { color: colorOf(j.kind), fontSize: "var(--t-2xs)" },
              }, "NEXT · ", h("span", { className: "v3-num" }, String(j.h).padStart(2, "0")), ":00 UTC"),
              h("div", { className: "num", style: { marginTop: 4 } }, j.name),
              h("div", { className: "dim mono", style: { fontSize: "var(--t-xs)", marginTop: 2 } }, j.desc)
            )
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
        h("span", { className: "pill accent" },
          h("span", { className: "dot accent pulse" }), " ",
          h("span", { className: "v3-num" }, String(items.length)), " EVENTS · 24h")),
    },
      h("div", { className: "v3-research-feed" },
        items.length === 0
          ? h("div", { className: "dim v3-research-empty" }, "no recent activity")
          : items.map((r, i) => {
              const dot = r.level === "warn" ? "warn"
                        : r.level === "down" ? "down"
                        : r.level === "up" ? "up"
                        : "accent";
              const srcVar = "var(--" + dot + ")";
              const stableKey = `${r.ts}:${r.src}:${(r.title || "").slice(0, 32)}`;
              const open = expanded === stableKey;
              const body1 = (r.title || "").slice(0, 96);
              const body2 = (r.body || "").replace(/\s+/g, " ").trim().slice(0, 110);
              const ageLabel = r.age_s < 60
                ? r.age_s + "s"
                : r.age_s < 3600
                  ? Math.floor(r.age_s / 60) + "m"
                  : Math.floor(r.age_s / 3600) + "h";
              return h("article", {
                key: stableKey,
                className: "v3-research-item" + (open ? " is-open" : ""),
                onClick: () => setExpanded(open ? null : stableKey),
              },
                h("div", { className: "v3-research-ts mono dim" },
                  h(TimeSince, { ts: r.ts })),
                h("div", { className: "v3-research-dotcol" },
                  h("span", { className: "dot " + dot })),
                h("div", { className: "v3-research-copy" },
                  h("header", { className: "v3-research-item-head" },
                    h("span", { className: "v3-research-age mono dim" },
                      h("span", { className: "v3-num" }, ageLabel), " ago"),
                    h("span", { className: "tb-spacer", style: { flex: 1 } }),
                    h("span", {
                      className: "v3-research-src mono",
                      style: { color: srcVar },
                    }, r.src)
                  ),
                  h("h3", { className: "v3-research-hed" }, body1),
                  h("p", { className: "v3-research-dek dim" }, body2 || "—"),
                  open && h("div", { className: "v3-research-cites mono dim" },
                    h("div", { className: "v3-research-cites-lbl" }, "Citations"),
                    (r.cites || []).map((c, j) => h("div", { key: j, className: "v3-research-cite-line" }, "→ ", c))
                  )
                )
              );
            })
      )
    );
  }

  // ─────────────── ENTRY GATES — live from /api/ops/gates ───────────────
  function GateDot({ state, label, detail }) {
    // tiny inline dot + glyph used in EntryGatesLive's per-pair gate-strip.
    // hover title surfaces gate name + detail so operator gets per-gate
    // context without expanding the row.
    //
    // WCAG 1.4.1 — color is not the only channel: pass = green dot + ✓,
    // fail = red dot + ✕, unknown = dim dot + · . Glyph is 10px, inherits
    // color from the dot so it stays readable on colorblind-safe palettes.
    const color = state === true ? "var(--up)"
      : state === false ? "var(--down)"
      : "color-mix(in srgb, var(--fg-3) 60%, transparent)";
    const glyph = state === true ? "✓"   // ✓
      : state === false ? "✕"            // ✕
      : "·";                              // ·
    return h("span", {
      title: label + " — " + (state === true ? "PASS" : state === false ? "BLOCK" : "n/a") + (detail ? " · " + detail : ""),
      "aria-label": label + " " + (state === true ? "pass" : state === false ? "block" : "unknown"),
      style: { display: "inline-flex", alignItems: "center", gap: 3, flexShrink: 0 },
    },
      h("span", {
        "aria-hidden": "true",
        style: { width: 9, height: 9, borderRadius: "50%", background: color, display: "inline-block", flexShrink: 0 },
      }),
      h("span", {
        "aria-hidden": "true",
        style: { fontSize: 10, lineHeight: 1, color: color, fontWeight: 600 },
      }, glyph)
    );
  }

  // ─────────────── Move #6 · Traffic-light pill row ────────────────────────
  // Replacement layout for the per-pair gate-strip. Each pill shows:
  //   [ regime ✓ 14m ]   (gate name · pass/fail glyph · time-since-last-flip)
  // Click expands to show the underlying value vs threshold (existing detail).
  //
  // Feature-flagged via localStorage["quanta.entry_gates_v2"] (default ON).
  // The legacy GateDotGrid is rendered next to it when the flag is OFF so the
  // operator can A/B-test instantly:
  //
  //   localStorage.removeItem("quanta.entry_gates_v2"); location.reload();
  //
  // Flip-timestamp source: ops_spa.js's in-memory ring buffer (see
  // gateFlipTracker below). The /api/ops/gates payload does NOT include a
  // per-gate last_flip_ts field, so we derive it by diffing successive
  // snapshots in the SPA. First-render falls through to "now".
  //
  // Time formatting: fmtAgoShort gives 5s / 14m / 2h / 1d — Bloomberg-tight.
  function fmtAgoShort(ts) {
    if (ts == null) return "—";
    const ms = Date.now() - ts;
    if (!Number.isFinite(ms) || ms < 0) return "now";
    const s = Math.floor(ms / 1000);
    if (s < 5)    return "now";
    if (s < 60)   return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m";
    if (s < 86400)return Math.floor(s / 3600) + "h";
    return Math.floor(s / 86400) + "d";
  }

  // Module-scope ring buffer keyed by pair+gate → last observed pass state +
  // when that state was first seen. The map persists across renders so
  // 10-second polls accumulate flip history; bounded by the active gate set
  // so it cannot grow unbounded (a removed pair drops out within minutes).
  const __gateFlipTracker = (function () {
    // key = pair + "|" + gate. value = { state, since: <ms> }
    const m = new Map();
    return {
      observe(pair, gate, state) {
        const key = pair + "|" + gate;
        const prev = m.get(key);
        const now = Date.now();
        if (!prev || prev.state !== state) {
          m.set(key, { state, since: now });
          return now;
        }
        return prev.since;
      },
      // GC every observe-sweep: caller passes the active key set, anything
      // not in it gets dropped so dead pairs don't leak.
      retain(activeKeys) {
        if (m.size < 200) return;
        for (const k of m.keys()) {
          if (!activeKeys.has(k)) m.delete(k);
        }
      },
    };
  })();

  function TrafficLightPillRow({ pair, gates }) {
    const [open, setOpen] = useState(null);
    // Observe flips for every gate this render. Side-effect-free: map writes
    // only happen when state actually changed; otherwise we read the cached
    // since-ms. This is safe to call during render (no setState).
    const activeKeys = new Set();
    const rows = (gates || []).map((g, gi) => {
      activeKeys.add(pair + "|" + g.gate);
      const since = __gateFlipTracker.observe(pair, g.gate, g.pass);
      return Object.assign({}, g, { since });
    });
    __gateFlipTracker.retain(activeKeys);

    return h("div", { className: "tlpill-row" },
      rows.map((g, gi) => {
        const cls = g.pass === true ? "pass" : g.pass === false ? "block" : "na";
        const glyph = g.pass === true ? "✓" : g.pass === false ? "✕" : "·";
        const isOpen = open === gi;
        return h("span", {
          key: gi,
          className: "tlpill " + cls + (isOpen ? " open" : ""),
          onClick: (e) => { e.stopPropagation(); setOpen(isOpen ? null : gi); },
          title: g.gate + " — " + (g.pass === true ? "PASS" : g.pass === false ? "BLOCK" : "n/a")
                 + (g.detail ? " · " + g.detail : ""),
        },
          h("span", { className: "tlp-name" }, g.gate),
          h("span", { className: "tlp-glyph" }, glyph),
          h("span", { className: "tlp-ts" }, fmtAgoShort(g.since))
        );
      }),
      open !== null && rows[open] && h("div", { className: "tlpill-detail", key: "exp" },
        h("span", { className: "tlpd-name" }, rows[open].gate),
        h("span", { className: "tlpd-detail" }, rows[open].detail || "—")
      )
    );
  }

  // ─────────────── Move #7 · Global blocker banner ─────────────────────────
  // Single-line summary mounted under the topbar, above TodayScoreboard.
  // Aggregates /api/ops/gates data into one prominent line:
  //
  //   🚦 6/8 pairs blocked on regime=trending_down  ·  2/8 blocked on
  //      vol_floor  ·  newest blocker: tft<0.40 (12m ago)
  //
  // Click expands to a per-pair breakdown (uses the same p.gates data the
  // EntryGatesLive modal already renders). When zero blockers exist, the
  // component returns null and consumes zero footprint.
  //
  // ZERO new endpoint calls — reads from the existing data.gates slot the
  // SPA already polls every 10s.
  function BlockerBanner({ data }) {
    const [expand, setExpand] = useState(false);
    const slot = slotState(data, "gates");
    if (slot.phase !== "ok") return null;
    const env = envelopeData(slot.env) || {};
    const crypto = env.crypto || [];
    const stocks = env.stocks || [];
    const all = crypto.concat(stocks).map(r => ({
      sym: r.pair,
      blocking: r.n_blocking || 0,
      first_blocker: r.first_blocker,
      gates: r.gates || [],
    }));
    const total = all.length;
    const blocked = all.filter(p => (p.blocking || 0) > 0);
    if (blocked.length === 0 || total === 0) return null;

    // Tally most common blockers across the universe.
    const counts = {};
    blocked.forEach(p => {
      (p.gates || []).filter(g => g.pass === false).forEach(g => {
        counts[g.gate] = (counts[g.gate] || 0) + 1;
      });
    });
    const topCauses = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 3);

    // "Newest blocker" — read flip-time from the same ring buffer Move #6
    // uses. Lowest "since" (most recent flip to blocked) wins.
    let newest = null;
    blocked.forEach(p => {
      (p.gates || []).filter(g => g.pass === false).forEach(g => {
        const since = __gateFlipTracker.observe(p.sym, g.gate, g.pass);
        if (!newest || since > newest.since) newest = { gate: g.gate, since, sym: p.sym };
      });
    });

    return h(F, null,
      h("div", {
        className: "blocker-banner",
        role: "button",
        tabIndex: 0,
        onClick: () => setExpand(!expand),
        onKeyDown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setExpand(!expand); } },
        title: "Click for per-pair breakdown",
      },
        h("span", { className: "bb-glyph", "aria-hidden": "true" }, "🚦"),
        h("span", { className: "bb-summary" },
          h("span", { className: "bb-warn" }, blocked.length + "/" + total),
          " pairs blocked",
          topCauses.length > 0 && h(F, null,
            h("span", { className: "bb-sep" }, "·"),
            topCauses.map(([cause, n], i) => h(F, { key: cause },
              i > 0 && h("span", { className: "bb-sep" }, "·"),
              h("span", null, n + "/" + total + " on "),
              h("span", { className: "bb-warn" }, cause)
            ))
          ),
          newest && h(F, null,
            h("span", { className: "bb-sep" }, "·"),
            h("span", { className: "bb-dim" }, "newest blocker: "),
            h("span", null, newest.gate),
            h("span", { className: "bb-dim" }, " (" + fmtAgoShort(newest.since) + " ago)")
          )
        ),
        h("span", { className: "bb-caret" }, expand ? "▾" : "▸")
      ),
      expand && h("div", { className: "blocker-banner-detail" },
        blocked.map(p => h("div", { key: p.sym, className: "bb-row" },
          h("span", { className: "bb-sym" }, p.sym),
          " ",
          h("span", { className: "bb-cause" }, p.first_blocker || ("× " + p.blocking))
        ))
      )
    );
  }

  const V3_CRYPTO_GATES_ORDER = [
    "capital_allocation", "model_freshness", "freqai_predict", "volume", "regime",
    "up_prob_threshold", "tft_confidence", "high_vol_confidence",
    "meta_signal_up", "meta_confidence", "account_capacity",
  ];
  const V3_STOCKS_GATES_ORDER = [
    "kill_switch", "ticker_kill_flag", "spy_regime", "no_existing_csp",
    "no_assignment", "buying_power", "snapshot_fresh", "schedule",
  ];
  const V3_GATE_HDR_SHORT = {
    capital_allocation: "cap",
    model_freshness: "mdl",
    freqai_predict: "prd",
    volume: "vol",
    regime: "rgm",
    up_prob_threshold: "up≥",
    tft_confidence: "tft≥",
    high_vol_confidence: "hvol",
    meta_signal_up: "m_up",
    meta_confidence: "m_c",
    account_capacity: "open",
    kill_switch: "kill",
    ticker_kill_flag: "tkf",
    spy_regime: "spy",
    no_existing_csp: "ncsp",
    no_assignment: "nasg",
    buying_power: "bpow",
    snapshot_fresh: "snap",
    schedule: "sch",
  };

  function v3GateCellHeat(gate, pass, detail) {
    const d = String(detail || "").toLowerCase();
    if (pass === true && d.indexOf("gate disabled") >= 0) return 1;
    if (pass === null) return 0;
    if (pass === true) return 2;
    if (pass === false) {
      if (gate === "regime" || gate === "model_freshness") return 6;
      return 5;
    }
    return 0;
  }

  function v3GateCellGlyph(pass) {
    if (pass === true) return "●";
    if (pass === false) return "✗";
    return "○";
  }

  function v3WhyText(row) {
    const n = row.n_blocking || 0;
    if (n === 0) return "EXIT_OK";
    const fb = row.first_blocker;
    return fb || "—";
  }

  function v3WhyTitle(row) {
    const fb = row.first_blocker;
    if (!fb) return (row.n_blocking || 0) === 0 ? "All gates clear for this pair." : "";
    const g = (row.gates || []).find(x => x.gate === fb);
    const det = g && g.detail ? String(g.detail) : "";
    return det || fb;
  }

  function v3WhyHardSuffix(row) {
    const fb = row.first_blocker;
    if (!fb || (row.n_blocking || 0) === 0) return "";
    const g = (row.gates || []).find(x => x.gate === fb);
    const det = g && g.detail ? String(g.detail) : "";
    if (det.indexOf("HARD BLOCK") >= 0) return " ← HARD";
    if (fb === "regime" || fb === "model_freshness") return " ← HARD";
    return "";
  }

  function EntryGatesLive({ data }) {
    const [sortMode, setSortMode] = useState("blocking_desc");
    const slot = slotState(data, "gates");
    const env = envelopeData(slot.env) || {};
    const cryptoRaw = env.crypto || [];
    const stocksRaw = env.stocks || [];

    const mapRow = (r, kind) => ({
      kind,
      sym: r.pair,
      regime: r.regime,
      n_blocking: r.n_blocking || 0,
      first_blocker: r.first_blocker,
      gates: r.gates || [],
    });

    const sortRows = (rows) => {
      const copy = rows.slice();
      const eligible = copy.filter(r => (r.n_blocking || 0) === 0);
      const blocked = copy.filter(r => (r.n_blocking || 0) > 0);
      const esc = sortMode === "blocking_asc" ? 1 : -1;
      blocked.sort((a, b) => esc * ((a.n_blocking || 0) - (b.n_blocking || 0)) || String(a.sym).localeCompare(String(b.sym)));
      eligible.sort((a, b) => String(a.sym).localeCompare(String(b.sym)));
      return eligible.concat(blocked);
    };

    const cryptoRows = sortRows(cryptoRaw.map(r => mapRow(r, "crypto")));
    const stockRows = sortRows(stocksRaw.map(r => mapRow(r, "stocks")));
    const allFlat = cryptoRows.concat(stockRows);
    const passing = allFlat.filter(p => (p.n_blocking || 0) === 0).length;
    const blocked = allFlat.length - passing;

    const blockerCounts = {};
    allFlat.forEach(p => (p.gates || []).filter(g => g.pass === false).forEach(g => {
      blockerCounts[g.gate] = (blockerCounts[g.gate] || 0) + 1;
    }));
    const topBlockers = Object.entries(blockerCounts).sort((a, b) => b[1] - a[1]).slice(0, 2);

    const renderGateHeader = (order, hdrClass) => h("div", { className: "v3-gates-matrix-hdr " + hdrClass },
      h("div", { className: "v3-gates-hdr-corner" }, "pair"),
      order.map((gate) => h("div", {
        key: gate,
        className: "v3-gates-hdr-col",
        title: gate,
      }, V3_GATE_HDR_SHORT[gate] || gate)),
      h("div", { className: "v3-gates-hdr-why" }, "WHY")
    );

    const renderGateRow = (row, order, rowClass) => {
      const gateByName = {};
      (row.gates || []).forEach((g) => { gateByName[g.gate] = g; });
      const pin = (row.n_blocking || 0) === 0;
      return h("div", {
        key: row.sym,
        className: "v3-gates-row " + rowClass + (pin ? " eligible-pin" : ""),
      },
        h("div", { className: "v3-gates-pair mono" }, row.sym),
        order.map((gate) => {
          const g = gateByName[gate] || { gate, pass: null, detail: "—" };
          const heat = v3GateCellHeat(g.gate, g.pass, g.detail);
          const title = g.gate + " — " + (g.detail || "");
          return h("div", {
            key: gate,
            className: "v3-gates-cell v3-gh-" + heat,
            title,
          }, v3GateCellGlyph(g.pass));
        }),
        h("div", {
          className: "v3-gates-why mono",
          title: v3WhyTitle(row),
        },
          h("span", { className: "v3-num" }, v3WhyText(row)),
          v3WhyHardSuffix(row))
      );
    };

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
      sub: passing + "/" + allFlat.length + " pair" + (allFlat.length === 1 ? "" : "s") + " eligible · Gates Matrix",
      right: cardRight(slot.fetchedAt,
        h("span", { className: "pill " + (blocked > 0 ? "down" : "up"), style: { height: 18 } },
          h("span", { className: "dot " + (blocked > 0 ? "down pulse" : "up") }), " ",
          blocked > 0 ? (blocked + " BLOCKED") : "ALL CLEAR"))
    },
      blocked > 0 && topBlockers.length > 0 && h("div", {
        style: { fontSize: "var(--t-xs)", padding: "var(--s-2) var(--s-3)",
          marginBottom: "var(--s-2)", borderLeft: "2px solid var(--down)",
          background: "color-mix(in srgb, var(--down) 6%, transparent)" }
      },
        h("span", { style: { color: "var(--fg-1)" } }, blocked + " of " + allFlat.length + " pairs blocked"),
        h("span", { className: "dim", style: { marginLeft: 8 } }, "most common: "),
        topBlockers.map(([g, n], i) => h("span", { key: g, className: "mono", style: { marginLeft: 6 } },
          (i > 0 ? "· " : "") + g + " (" + n + "×)"))
      ),

      h("div", { className: "v3-gates-sort-hint" },
        "sort ",
        h("button", {
          type: "button",
          className: "v3-gates-sort-btn" + (sortMode === "blocking_desc" ? " on" : ""),
          onClick: () => setSortMode("blocking_desc"),
        }, "n_blocking ↓"),
        h("button", {
          type: "button",
          className: "v3-gates-sort-btn" + (sortMode === "blocking_asc" ? " on" : ""),
          onClick: () => setSortMode("blocking_asc"),
        }, "n_blocking ↑")
      ),

      allFlat.length === 0
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)", padding: "var(--s-3)" } },
            "no gate data — endpoint returned empty")
        : h("div", { className: "v3-gates-matrix-wrap" },
            h("div", { className: "v3-gates-matrix" },
              h("div", { className: "v3-gates-group-label" }, "crypto"),
              renderGateHeader(V3_CRYPTO_GATES_ORDER, "crypto"),
              cryptoRows.length === 0
                ? h("div", { className: "dim", style: { padding: "var(--s-2)" } }, "no crypto rows")
                : cryptoRows.map((row) => renderGateRow(row, V3_CRYPTO_GATES_ORDER, "crypto")),
              h("div", { className: "v3-gates-group-label" }, "stocks"),
              renderGateHeader(V3_STOCKS_GATES_ORDER, "stocks"),
              stockRows.length === 0
                ? h("div", { className: "dim", style: { padding: "var(--s-2)" } }, "no stocks rows")
                : stockRows.map((row) => renderGateRow(row, V3_STOCKS_GATES_ORDER, "stocks"))
            ))
    );
  }

  // Deterministic fallback closes when API sparse — symbol-seeded PRNG (no Math.random).
  function v3SymHash32(s) {
    let h = 2166136261 >>> 0;
    const str = String(s || "");
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 16777619) >>> 0;
    }
    return h >>> 0;
  }
  function v3DeterministicCloses(sym, n) {
    const out = [];
    let state = v3SymHash32(sym) || 1;
    let v = 1 + (state % 500) / 10000;
    for (let i = 0; i < n; i++) {
      state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
      const u = (state & 0xffff) / 65536 - 0.5;
      v = Math.max(1e-8, v * (1 + u * 0.024));
      out.push(v);
    }
    return out;
  }

  // ─────────────── PAIR TELEMETRY — Sparkline Strip (V3 §5.6) ───────────────
  function PairTelemetryLive({ data }) {
    const [sort, setSort] = useState("pct");
    const slot = slotState(data, "sparklines");
    const gateSlot = slotState(data, "gates");
    const liveSlot = slotState(data, "live_trades");
    const env = envelopeData(slot.env) || {};
    const pairs = env.pairs || {};
    const entries = Object.entries(pairs);

    const gateEnv = gateSlot.phase === "ok" ? (envelopeData(gateSlot.env) || {}) : {};
    const gateByPair = {};
    (gateEnv.crypto || []).forEach((row) => { if (row && row.pair) gateByPair[row.pair] = row; });

    const liveEnv = liveSlot.phase === "ok" ? (envelopeData(liveSlot.env) || {}) : {};
    const openLabels = new Set((liveEnv.trades || []).map((t) => String(t.label || "").toUpperCase()));

    if (slot.phase === "down") {
      return h(Card, {
        num: "06", title: "Pair telemetry · 5m closes · trailing 24h",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt),
      },
        h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }

    const rows = entries.map(([sym, p]) => {
      const raw = p.closes || [];
      const closes = raw.length >= 2 ? raw : v3DeterministicCloses(sym, 189);
      const pct = Number(p.pct_24h || 0);
      const px = Number(p.current || 0);
      const g = gateByPair[sym];
      const nBlocking = g ? (g.n_blocking || 0) : 0;
      // "blocked" was triggering strike-through styling on EVERY pair in
      // any non-bull regime — operator perceived the whole strip as broken.
      // Distinguish hard blocks (account/risk/breaker/kill) from soft regime
      // blocks (the strategy correctly waiting for the market to flip).
      const firstBlocker = g && g.first_blocker ? String(g.first_blocker) : "";
      const isRegimeBlock = nBlocking > 0 && (firstBlocker === "regime" || firstBlocker === "");
      const hardBlocked = nBlocking > 0 && !isRegimeBlock;
      const regime = g && g.regime ? String(g.regime) : "—";
      const hasPos = openLabels.has(String(sym).toUpperCase());
      const chip = hardBlocked
        ? ("blocked · " + firstBlocker)
        : (hasPos ? "position open" : ("regime · " + regime));
      return { sym, p, closes, pct, px, g, blocked: hardBlocked, regime, hasPos, chip };
    });

    const sorted = rows.slice().sort((a, b) => {
      if (sort === "sym") return String(a.sym).localeCompare(String(b.sym));
      if (sort === "regime") return String(a.regime).localeCompare(String(b.regime)) || String(a.sym).localeCompare(String(b.sym));
      if (sort === "position") return (Number(b.hasPos) - Number(a.hasPos)) || (b.pct - a.pct);
      return (b.pct - a.pct) || String(a.sym).localeCompare(String(b.sym));
    });

    const sortBar = h("div", { className: "v3-spark-strip-sort" },
      h("span", { className: "dim mono" }, "sort ·"),
      ["pct", "regime", "position", "sym"].map((k) => h("button", {
        key: k,
        type: "button",
        "aria-pressed": sort === k,
        onClick: () => setSort(k),
      }, k))
    );

    return h(Card, {
      num: "06", title: "Pair telemetry · 5m closes · trailing 24h",
      sub: entries.length + " pairs · Sparkline Strip · auto-refresh 10s",
      right: cardRight(slot.fetchedAt),
    },
      slot.phase === "loading"
        ? h(LoadingState)
        : entries.length === 0
          ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no sparkline data")
          : h(F, null,
            sortBar,
            h("div", { className: "v3-spark-strip" },
              sorted.map((r) => {
                const href = "/?pair=" + encodeURIComponent(r.sym) + "&venue=crypto";
                const rowCls = cls(
                  "v3-spark-row",
                  r.blocked && "v3-spark-row--blocked",
                  r.hasPos && "v3-spark-row--pos",
                );
                return h("a", {
                  key: r.sym,
                  href,
                  className: rowCls,
                  style: { textDecoration: "none", color: "inherit" },
                },
                h("span", { className: "v3-spark-row-edge" }),
                h("span", { className: "v3-spark-sym mono" }, r.sym),
                h("span", { className: "v3-spark-px mono v3-num" },
                  r.px < 10 ? r.px.toFixed(4) : fmtUSD(r.px)),
                h("span", { className: cls("v3-spark-delta", "mono", "v3-num", r.pct >= 0 ? "up" : "down") }, fmtPct(r.pct)),
                h("span", { className: cls("v3-spark-arrow", "mono", r.pct >= 0 ? "up" : "down") }, r.pct >= 0 ? "▲" : "▼"),
                h("div", { className: "v3-spark-spark" },
                  h(Sparkline, {
                    data: r.closes,
                    color: r.pct >= 0 ? "--up" : "--down",
                    height: r.hasPos ? 40 : 28,
                    animate: true,
                  })),
                h("span", {
                  className: cls("v3-spark-chip", "mono", "pill", r.blocked ? "warn" : r.hasPos ? "up" : "info"),
                  style: { fontWeight: 500 },
                }, r.chip)
                );
              })
            ))
    );
  }

  // ─────────────── STOCKS PAIR TELEMETRY — Sparkline Strip (V3 §5.6 template) ───────────────
  function StocksPairTelemetryLive({ data }) {
    const [sort, setSort] = useState("pct");
    const slot = slotState(data, "stocks_sparklines");
    const gateSlot = slotState(data, "gates");
    const liveSlot = slotState(data, "live_trades");
    const env = envelopeData(slot.env) || {};
    const symbols = env.symbols || {};
    const basket = Array.isArray(env.basket) ? env.basket : Object.keys(symbols);
    const marketOpen = !!env.market_open;
    const tfLabel = env.timeframe || "5Min";

    const gateEnv = gateSlot.phase === "ok" ? (envelopeData(gateSlot.env) || {}) : {};
    const gateBySym = {};
    (gateEnv.stocks || []).forEach((row) => { if (row && row.pair) gateBySym[String(row.pair).toUpperCase()] = row; });

    const liveEnv = liveSlot.phase === "ok" ? (envelopeData(liveSlot.env) || {}) : {};
    const openLabels = new Set((liveEnv.trades || []).map((t) => String(t.label || "").toUpperCase()));

    if (slot.phase === "down") {
      return h(Card, {
        num: "23", title: "Stocks pair telemetry · " + tfLabel + " · session window",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt),
      },
        h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }
    if (slot.phase === "loading") {
      return h(Card, {
        num: "23", title: "Stocks pair telemetry · " + tfLabel + " · session window",
        sub: "loading…",
        right: cardRight(slot.fetchedAt),
      }, h(LoadingState));
    }

    const subLine = marketOpen
      ? basket.length + " symbols · NYSE session · auto-refresh 10s"
      : basket.length + " symbols · NYSE closed · last session close";

    const wrapperStyle = marketOpen
      ? null
      : { opacity: 0.78 };

    const rows = basket.map((sym) => {
      const p = symbols[sym] || {};
      const raw = p.closes || [];
      const closes = raw.length >= 2 ? raw : v3DeterministicCloses(sym, 120);
      const pct = (p.pct_session == null) ? null : Number(p.pct_session);
      const px = (p.current == null) ? null : Number(p.current);
      const g = gateBySym[String(sym).toUpperCase()];
      const nBlocking = g ? (g.n_blocking || 0) : 0;
      const blocked = nBlocking > 0;
      const regime = g && g.regime ? String(g.regime) : "—";
      const hasPos = openLabels.has(String(sym).toUpperCase());
      const chip = blocked
        ? ("blocked · " + (g && g.first_blocker ? String(g.first_blocker) : "gates"))
        : (hasPos ? "position open" : ("regime · " + regime));
      return { sym, p, closes, pct, px, blocked, regime, hasPos, chip, err: p.error };
    });

    const sorted = rows.slice().sort((a, b) => {
      const ap = a.pct == null ? -999 : a.pct;
      const bp = b.pct == null ? -999 : b.pct;
      if (sort === "sym") return String(a.sym).localeCompare(String(b.sym));
      if (sort === "regime") return String(a.regime).localeCompare(String(b.regime)) || String(a.sym).localeCompare(String(b.sym));
      if (sort === "position") return (Number(b.hasPos) - Number(a.hasPos)) || (bp - ap);
      return (bp - ap) || String(a.sym).localeCompare(String(b.sym));
    });

    const sortBar = h("div", { className: "v3-spark-strip-sort" },
      h("span", { className: "dim mono" }, "sort ·"),
      ["pct", "regime", "position", "sym"].map((k) => h("button", {
        key: k,
        type: "button",
        "aria-pressed": sort === k,
        onClick: () => setSort(k),
      }, k))
    );

    const sessionStrip = h("div", { className: "v3-mh-strip" },
      marketOpen
        ? "NYSE session · OPEN · current-session rows highlighted"
        : "NYSE session · CLOSED · showing last session window");

    return h(Card, {
      num: "23", title: "Stocks pair telemetry · " + tfLabel + " · session window",
      sub: subLine,
      right: cardRight(slot.fetchedAt),
    },
      h("div", { style: wrapperStyle },
        sessionStrip,
        basket.length === 0
          ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no stock symbols configured")
          : h(F, null,
            sortBar,
            h("div", { className: "v3-spark-strip" },
              sorted.map((r) => {
                const href = "/?pair=" + encodeURIComponent(r.sym) + "&venue=stocks";
                const pctNum = r.pct == null ? 0 : r.pct;
                const sparkColor = r.pct == null ? "--fg-3" : (r.pct >= 0 ? "--up" : "--down");
                const rowCls = cls(
                  "v3-spark-row",
                  r.blocked && "v3-spark-row--blocked",
                  r.hasPos && "v3-spark-row--pos",
                  marketOpen && "v3-spark-row--session",
                );
                return h("a", {
                  key: r.sym,
                  href,
                  className: rowCls,
                  style: { textDecoration: "none", color: "inherit" },
                  "data-test": "stocks-spark-" + r.sym,
                },
                h("span", { className: "v3-spark-row-edge" }),
                h("span", { className: "v3-spark-sym mono" }, r.sym),
                h("span", { className: "v3-spark-px mono v3-num" },
                  r.px == null ? "—" : ("$" + fmtUSD(r.px))),
                h("span", { className: cls("v3-spark-delta", "mono", "v3-num", r.pct == null ? "" : (r.pct >= 0 ? "up" : "down")) },
                  r.pct == null ? "—" : fmtPct(r.pct)),
                h("span", { className: cls("v3-spark-arrow", "mono", r.pct == null ? "" : (r.pct >= 0 ? "up" : "down")) },
                  r.pct == null ? "·" : (r.pct >= 0 ? "▲" : "▼")),
                h("div", { className: "v3-spark-spark" },
                  h(Sparkline, { data: r.closes, color: sparkColor, height: r.hasPos ? 40 : 28, animate: true })),
                h("span", {
                  className: cls("v3-spark-chip", "mono", "pill", r.blocked ? "warn" : r.hasPos ? "up" : "info"),
                  style: { fontWeight: 500 },
                }, r.err ? String(r.err) : r.chip)
                );
              })
            ))
      )
    );
  }

  // ─────────────── SERVICES — 8-row health probe (V3 Wave 1D) ───────────────
  function ServicesLive({ data, killState }) {
    const [expanded, setExpanded] = useState(false);
    const slot = slotState(data, "services");
    const services = envelopeData(slot.env) || {};
    const rows = Object.entries(services);
    const totalUp = rows.filter(([, info]) => info && info.up).length;
    const mode = envelopeData(data.mode) || {};
    const cb = envelopeData(data.circuit_breakers) || {};
    const wt = envelopeData(data.weekly_training) || {};
    const hbStatus = deriveHeartbeatStatus({
      services: data.services,
      circuitBreakers: data.circuit_breakers,
      mode: data.mode,
      weeklyTraining: wt,
      ollamaHealth: data.ollama_health,
      killState: killState || "normal",
    });

    if (slot.phase !== "ok") {
      return h(Card, {
        num: "07a", title: "Service health · probes",
        sub: slot.phase === "loading" ? "loading…" : "endpoint unavailable",
        right: cardRight(slot.fetchedAt),
      },
        slot.phase === "loading"
          ? h(LoadingState)
          : h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }

    const headDot = h(HeartbeatDot, {
      status: hbStatus,
      title: "System heartbeat · " + totalUp + "/" + rows.length + " probes up · " + String(mode.state || "—"),
      onClick: () => setExpanded((v) => !v),
    });

    return h(Card, {
      num: "07a", title: "Service health · " + rows.length + " probes",
      sub: (expanded ? "expanded" : "collapsed") + " · " + totalUp + "/" + rows.length + " up · " + String(mode.state || "—"),
      right: h("div", { style: { display: "flex", alignItems: "center", gap: 8 } },
        headDot,
        cardRight(slot.fetchedAt)),
    },
      h("div", {
        className: expanded ? "v3-svc-probes-expanded" : "v3-svc-probes-collapsed",
        style: { display: "flex", flexDirection: "column" },
      },
        rows.length === 0
          ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no probes registered")
          : rows.map(([name, info]) => h("div", {
            key: name,
            className: "v3-svc-probe-row srow",
            style: { display: "flex", alignItems: "center", gap: 8, padding: "4px 0", borderBottom: "1px solid rgba(255,255,255,.04)" },
          },
            h(StatusRow, {
              status: info && info.up ? "up" : "down",
              name: name,
              sub: info ? ("via " + (info.via || "?") + (info.code != null ? " · " + info.code : "")) : "",
              value: h("span", null,
                info && info.age_s != null ? h("span", { className: "dim v3-num", style: { marginRight: 10 } }, Math.round(info.age_s) + "s") : null,
                info && info.endpoint ? h("span", { className: "dim mono v3-num", style: { fontSize: "var(--t-2xs)" } }, info.endpoint) : null
              ),
            })
          )),
        h("button", {
          type: "button",
          className: "btn",
          style: { marginTop: 8, alignSelf: "flex-start", fontSize: "var(--t-2xs)" },
          onClick: () => setExpanded((v) => !v),
        }, expanded ? "Collapse probes" : "Expand all probes")
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
        h(NumberRoll, { value: Number(saved), decimals: 2, prefix: "$", className: "v3-num" })
      )
    },
      h("div", { className: "v3-llm-two-tile" },
        h("div", { className: "v3-llm-tile" },
          h("div", { className: "metric-label" }, "OLLAMA · primary"),
          h("div", { style: { marginTop: 8 } },
            h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, "calls 24h · "),
            h(NumberRoll, { value: Number(cryptoCalls != null ? cryptoCalls : 0), decimals: 0, suffix: " calls", className: "v3-num" })),
          h("div", { className: "dim", style: { fontSize: "var(--t-2xs)", marginTop: 6 } }, "latency · ",
            ollamaLatencyMs != null ? h("span", { className: "mono v3-num" }, ollamaLatencyMs + " ms") : "—"),
          h("div", { className: "dim", style: { fontSize: "var(--t-2xs)", marginTop: 4 } }, "saved vs Anthropic baseline"),
          h("div", { style: { marginTop: 4 } },
            h(NumberRoll, { value: Number(saved), decimals: 2, prefix: "$", className: "v3-num" }))
        ),
        h("div", { className: "v3-llm-tile" },
          h("div", { className: "metric-label" }, "ANTHROPIC · fallback"),
          h("div", { style: { marginTop: 10 } }, h("span", { className: "pill down" }, "DISABLED")),
          h("div", { className: "dim mono", style: { fontSize: "var(--t-2xs)", marginTop: 8 } }, "circuit · ",
            breakers.length ? (breakers.length + " breaker(s)") : "armed · no trips"))
      ),
      breakers.length > 0 ? h("div", { style: { marginTop: "var(--s-3)" } },
        h("div", { className: "metric-label" }, "CIRCUIT DETAIL"),
        breakers.map(b => h(StatusRow, {
          key: b.name || b.id,
          status: b.state === "open" ? "down" : b.state === "half_open" ? "warn" : "up",
          name: b.name || b.id,
          sub: "state " + (b.state || "?") + " · failures " + (b.failure_count || 0),
          value: h("span", null,
            b.opened_at ? h("span", { className: "dim mono" }, "opened ", b.opened_at) : "—")
        }))
      ) : null
    );
  }

  // ─────────────── POSITIONS — live trades + wheel ───────────────
  // Bonus #1 · row-flash on new fill. We detect "new" by comparing each
  // row's synthetic trade key against the previous render's set; rows that
  // weren't there before get a one-shot 200ms CSS animation class
  // (flash-buy or flash-sell). Pure CSS, no animation lib. The first-ever
  // render seeds the set, so a hard-refresh does NOT flash every existing
  // row (operator was clear: flash on NEW fills only).
  function tradeRowKey(t) {
    return [t.label, t.kind, t.subkind, t.opened_at, t.entry].filter(Boolean).join("|");
  }
  function PositionsLive({ data }) {
    const slot = slotState(data, "live_trades");
    const trSlot = slotState(data, "trades_risk");
    const env = envelopeData(slot.env) || {};
    const trades = env.trades || [];
    const tape = trSlot.phase === "ok" ? ((envelopeData(trSlot.env) || {}).live_tape || []) : [];
    const lastExit = tape.length && tape[0].exit_time ? String(tape[0].exit_time).replace("T", " ").slice(0, 19) : null;

    const prevKeysRef = useRef(null);   // null = first render; seed on next paint
    const newKeys = useMemo(() => {
      const cur = new Set(trades.map(tradeRowKey));
      if (prevKeysRef.current === null) {
        // first render → no flash, just seed
        prevKeysRef.current = cur;
        return new Set();
      }
      const fresh = new Set();
      cur.forEach(k => { if (!prevKeysRef.current.has(k)) fresh.add(k); });
      prevKeysRef.current = cur;
      return fresh;
    }, [trades]);

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
          h("th", { style: { textAlign: "right" } }, "uPnL"),
          h("th", null, "Note")
        )),
        h("tbody", null,
          trades.length === 0
            ? h("tr", null, h("td", { colSpan: 8 },
                h("div", { className: "v3-bt-empty" },
                  h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no open positions"),
                  lastExit && h("div", { className: "mono v3-num", style: { marginTop: 10, fontSize: "var(--t-sm)" } },
                    "last trade exit · ", lastExit))))
            : trades.map((t, i) => {
                const key = tradeRowKey(t);
                const isShort = (t.subkind || "").includes("short");
                const isNew = newKeys.has(key);
                const flashCls = isNew ? (isShort ? "flash-sell" : "flash-buy") : "";
                return h("tr", { key: key || i, className: flashCls },
                  h("td", null, h("strong", { className: "mono" }, t.label)),
                  h("td", { className: "dim" }, t.kind === "crypto" ? "Coinbase" : t.kind === "wheel" ? "Alpaca" : t.kind),
                  h("td", { className: "mono " + (isShort ? "down" : "up") }, (t.subkind || "—").toUpperCase()),
                  h("td", { className: "num", style: { textAlign: "right" } }, t.qty != null ? t.qty : "—"),
                  h("td", { className: "num", style: { textAlign: "right" } }, t.entry != null ? fmtUSD(t.entry, t.entry < 10 ? 4 : 2) : "—"),
                  h("td", { className: "num", style: { textAlign: "right" } }, t.current != null ? fmtUSD(t.current, t.current < 10 ? 4 : 2) : "—"),
                  h("td", { className: "num", style: { textAlign: "right" } },
                    t.pnl_usd != null
                      ? h(NumberRoll, { value: Number(t.pnl_usd), decimals: 2, prefix: "$" })
                      : (t.pnl_pct != null ? h("span", { className: "v3-num" }, fmtPct(t.pnl_pct)) : "—")),
                  h("td", { className: "dim", style: { fontSize: "var(--t-xs)" } }, t.extra || "")
                );
              })
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
      (function () {
        const evs = Array.isArray(env.evolution) ? env.evolution : [];
        const last = evs.length ? evs[evs.length - 1] : null;
        let members = (last && last.members) || [];
        if (!members.length) {
          members = [{ member_id: "Shark TFT · pool", val_acc: env.best_val_acc }];
        }
        return h("div", { style: { display: "flex", flexDirection: "column", gap: 4, marginTop: 6 } },
          members.slice(0, 15).map((m, i) => h("div", {
            key: (m.member_id || "m") + String(i),
            style: {
              display: "flex", alignItems: "center", gap: 10, fontSize: "var(--t-xs)",
              padding: "4px 0", borderBottom: "1px solid rgba(255,255,255,.05)",
            },
          },
            h("span", { className: "mono", style: { minWidth: 140 } }, m.member_id || "—"),
            h("span", { className: "tb-spacer", style: { flex: 1 } }),
            h("span", { className: "dim mono v3-num", style: { fontSize: "var(--t-2xs)" } },
              "age ", (env.weights_age_seconds != null ? Math.floor(env.weights_age_seconds / 3600) + "h" : "—")),
            h("span", { className: "pill accent", style: { height: 18, fontSize: "var(--t-2xs)" } },
              "α ", m.val_acc != null ? Number(m.val_acc).toFixed(3) : (env.best_val_acc != null ? Number(env.best_val_acc).toFixed(3) : "—"))
          )));
      })(),
      h("div", { className: "hr" }),
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "BEST VAL_ACC"),
        h("div", { className: "num v3-num" }, env.best_val_acc != null ? env.best_val_acc.toFixed(3) : "—"),
        h("div", { className: "dim mono" }, "BEST EPOCH"),
        h("div", { className: "num v3-num" }, env.best_epoch != null ? env.best_epoch : "—"),
        h("div", { className: "dim mono" }, "N TRAIN"),
        h("div", { className: "num v3-num" }, env.n_train != null ? env.n_train : "—"),
        h("div", { className: "dim mono" }, "N TICKERS"),
        h("div", { className: "num v3-num" }, env.n_tickers != null ? env.n_tickers : "—"),
        h("div", { className: "dim mono" }, "DEVICE"),
        h("div", { className: "num v3-num" }, env.device || "—"),
        h("div", { className: "dim mono" }, "NEXT CRON"),
        h("div", { className: "num v3-num" }, env.next_train_cron || "—")
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
          h("div", { style: { display: "flex", gap: "var(--s-5)", alignItems: "baseline", marginBottom: "var(--s-2)" } },
            h("div", null,
              h("div", { className: "metric-label" }, "OPEN CONTRACTS"),
              h("div", { className: "mono v3-num", style: { fontSize: "var(--t-xl)", marginTop: 4 } }, String(positions.length))),
            h("div", { className: "vr", style: { alignSelf: "stretch" } }),
            h("div", null,
              h("div", { className: "metric-label" }, "PREMIUM COLLECTED"),
              h("div", { className: "mono v3-num up", style: { fontSize: "var(--t-xl)", marginTop: 4 } }, "$" + fmtUSD(totalPremium)))
          ),
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

    const st = envelopeStatus(slot.env);
    const degraded = st === "degraded";
    const pillLabel = !reachable ? "DOWN" : degraded ? "DEGRADED" : "OK";
    const pillCls = !reachable ? "v3-mcp-pill--down" : degraded ? "v3-mcp-pill--deg" : "v3-mcp-pill--ok";
    const lastTs = lastCall.ts ? String(lastCall.ts).replace("T", " ").slice(0, 19) : "—";

    return h(Card, {
      num: "11", title: "MCP · wire status",
      sub: reachable ? "Hermes MCP · streamable HTTP" : "MCP unreachable",
      right: cardRight(slot.fetchedAt),
    },
      h("div", { className: "v3-mcp-pill " + pillCls }, pillLabel),
      h("div", { className: "mono dim v3-num", style: { fontSize: "var(--t-xs)", marginTop: 10, textAlign: "center" } },
        "last successful call · ", lastTs,
        lastCall.tool ? (" · " + String(lastCall.tool)) : ""),
      h("div", { className: "dim mono", style: { fontSize: "var(--t-2xs)", marginTop: 8, wordBreak: "break-all" } },
        (env.transport || "—") + " · ", (env.endpoint || "—"))
    );
  }

  // ─────────────── QUICK ACTIONS — fully wired ───────────────
  // Each button shows a status indicator (success/error/info) below the button row.
  function QuickActions({ setKillState, killState }) {
    const [status, setStatus] = useState({ msg: "", level: "info", ts: 0 });
    const toast = (msg, level) => setStatus({ msg, level: level || "info", ts: Date.now() });

    function readMcpKey() {
      try { return sessionStorage.getItem("hermesMcpKey") || ""; } catch (_) { return ""; }
    }
    function authHeadersJson() {
      const headers = { "Content-Type": "application/json" };
      const k = readMcpKey();
      if (k) {
        headers.Authorization = "Bearer " + k;
        headers["X-Hermes-MCP-Key"] = k;
      }
      return headers;
    }
    const postJSON = (url, body) => fetch(url, {
      method: "POST",
      headers: authHeadersJson(),
      body: JSON.stringify(body || {}),
    });

    const doPause = () => postJSON("/api/ops/pause", { reason: "operator manual pause via spa" })
      .then(r => r.ok ? toast("PAUSED · dry_run=true", "ok") : toast("PAUSE failed · HTTP " + r.status, "warn"))
      .catch(e => toast("PAUSE error · " + e.message, "warn"));

    const doResumeDirect = () => postJSON("/api/ops/resume", { reason: "operator manual resume via spa", confirm: true })
      .then(r => r.ok ? toast("RESUMED · dry_run=false", "ok") : r.json().then(j => toast("RESUME refused · " + (j.detail || ("HTTP " + r.status)), "warn")))
      .catch(e => toast("RESUME error · " + e.message, "warn"));

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
      sub: "hold-to-confirm 1.5s · X-Hermes-MCP-Key when configured",
    },
      h("div", { style: { display: "flex", flexDirection: "column", gap: "var(--s-3)" } },
        h(HoldToConfirmButton, {
          label: "PAUSE TRADING",
          variant: "compact",
          ariaLabel: "Pause trading after hold",
          onHoldComplete: doPause,
        }),
        h(HoldToConfirmButton, {
          label: "RESUME",
          variant: "compact",
          ariaLabel: "Resume trading after hold",
          onHoldComplete: doResumeDirect,
        }),
        h(HoldToConfirmButton, {
          label: "TRIGGER EVOLUTION",
          variant: "compact",
          danger: true,
          ariaLabel: "Trigger evolution after hold",
          onHoldComplete: doEvolve,
        }),
        h(HoldToConfirmButton, {
          label: "REBALANCE WEIGHTS",
          variant: "compact",
          danger: true,
          ariaLabel: "Rebalance weights after hold",
          onHoldComplete: doRebalance,
        }),
        h("button", { className: "btn", type: "button", onClick: doSlackBrief, "aria-label": "Slack brief info" }, "DAILY SLACK BRIEF")
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
    const epochPct = cur && cur.max_epoch ? Math.min(100, (Number(cur.last_epoch) / Number(cur.max_epoch)) * 100) : 0;
    const inner = h(F, null,
      cur ? h("div", null,
        h(ProgressBar, { value: epochPct, max: 100, cls: "accent" }),
        h("div", { className: "hr" })) : null,
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "CURRENT PAIR"),
        h("div", { className: "num accent" }, (cur && cur.pair) || "—"),
        h("div", { className: "dim mono" }, "EPOCH"),
        h("div", { className: "num v3-num" }, cur ? (cur.last_epoch + " / " + cur.max_epoch) : "—"),
        h("div", { className: "dim mono" }, "VAL SHARPE"),
        h("div", { className: "num " + ((cur && cur.val_sharpe >= 0) ? "up" : "down") }, cur && cur.val_sharpe != null ? Number(cur.val_sharpe).toFixed(3) : "—"),
        h("div", { className: "dim mono" }, "LOSS"),
        h("div", { className: "num" }, cur && cur.loss != null ? Number(cur.loss).toFixed(4) : "—"),
        h("div", { className: "dim mono" }, "AVG EPOCH"),
        h("div", { className: "num v3-num" }, tft.avg_epoch_seconds != null ? tft.avg_epoch_seconds + "s" : "—"),
        h("div", { className: "dim mono" }, "ETA"),
        h("div", { className: "num v3-num" }, etaMin != null ? etaMin + "m" : "—"),
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
            h("span", { className: "num v3-num" }, "ep " + (p.last_epoch != null ? p.last_epoch : "—")),
            h("span", { className: "num " + ((p.val_sharpe || 0) >= 0 ? "up" : "down") }, p.val_sharpe != null ? Number(p.val_sharpe).toFixed(2) : "—"),
            h("span", { className: "dim mono" }, p.early_stopped ? "early-stop" : (p.end_ts || p.start_ts || ""))
          ))
        )
      )
    );
    return h("div", { className: cur ? "v3-train-live-glow" : undefined },
      h(Card, {
        num: "17", title: "Training · FreqAI / TFT retrain status",
        sub: cur ? ("training " + cur.pair + " · epoch " + cur.last_epoch + "/" + cur.max_epoch) : (done.length + " pairs trained"),
        right: h(F, null,
          h(TimeSince, { ts: data.training_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
          cur
            ? h("span", { className: "pill accent" }, h("span", { className: "dot accent pulse" }), " LIVE")
            : h("span", { className: "pill up" }, "IDLE")
        ),
      }, inner));
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
    let firstFailIdx = -1;
    for (let i = 0; i < checks.length; i++) {
      if (!checks[i].passed) { firstFailIdx = i; break; }
    }
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
        : h("table", { className: "v3-readiness-matrix" },
            h("thead", null, h("tr", null,
              h("th", null, "Gate"),
              h("th", null, "Current"),
              h("th", null, "Threshold"),
              h("th", null, "Dir"),
              h("th", null, "Pass")
            )),
            h("tbody", null, checks.map((c, i) => h("tr", {
              key: i,
              className: i === firstFailIdx ? "first-fail" : "",
            },
              h("td", { className: "mono" }, labelOf(c.name)),
              h("td", { className: "v3-num " + (c.passed ? "up" : "down") }, fmtVal(c.name, c.value)),
              h("td", { className: "dim mono v3-num" }, fmtTh(c.name, c.threshold, c.op)),
              h("td", { className: "dim mono" }, c.op || "—"),
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
  function RegimeParamsGuide() {
    const mh = { fontFamily: "var(--mono)", fontSize: "var(--t-2xs)", fontWeight: 600, letterSpacing: ".06em", textTransform: "uppercase", color: "var(--fg-3)", margin: "8px 0 6px" };
    const dl = { display: "grid", gridTemplateColumns: "160px 1fr", gap: "4px 14px", margin: "0 0 6px" };
    return h(F, null,
      h("h4", { style: mh }, "The 5 market regimes"),
      h("p", { style: { margin: "0 0 10px" } }, "HMM classifies each candle; strategy adapts entries, exits, sizing, and trailing stops."),
      h("dl", { style: dl },
        h("dt", null, h("code", null, "trending_up")), h("dd", null, "Uptrend — loosen entries, trail winners."),
        h("dt", null, h("code", null, "trending_down")), h("dd", null, "Downtrend — longs hard-blocked until regime flips."),
        h("dt", null, h("code", null, "mean_reverting")), h("dd", null, "Range — quick scalps via ", h("code", null, "mean_rev_take_profit"), "."),
        h("dt", null, h("code", null, "high_volatility")), h("dd", null, "Volatile — shrink size, raise ", h("code", null, "high_vol_min_confidence"), "."),
        h("dt", null, h("code", null, "unknown")), h("dd", null, "Uncertain HMM — conservative defaults.")
      ),
      h("h4", { style: Object.assign({}, mh, { marginTop: 14 }) }, "Entry & exit deltas"),
      h("p", { style: { margin: "0 0 10px" } }, "Offsets to base entry ", h("code", null, "0.62"), " and exit ", h("code", null, "0.55"), ". Blank entry_delta = hard-block longs."),
      h("h4", { style: Object.assign({}, mh, { marginTop: 14 }) }, "Scalar parameters"),
      h("p", { style: { margin: "0 0 10px" } }, "High-vol sizing, mean-reversion take-profit, trail trigger/distance, TFT and meta confidence floors."),
      h("div", {
        style: {
          marginTop: 10, padding: "8px 12px",
          background: "color-mix(in srgb, var(--warn) 12%, transparent)",
          borderLeft: "3px solid var(--warn)", borderRadius: 4, color: "var(--fg-1)",
        }
      },
        h("strong", null, "Blur-to-save"),
        " writes ", h("code", null, "config.json"), " atomically and triggers freqtrade reload. Open trades keep current parameters until close."
      )
    );
  }

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
      if (!f[group]) f[group] = {};
      f[group][regime] = v;
      setForm(f);
    };
    const setScalar = (k, v) => {
      const f = JSON.parse(JSON.stringify(form));
      f[k] = v;
      setForm(f);
    };
    const reset = () => setForm(JSON.parse(JSON.stringify(cfg)));

    const sameDelta = (a, b) => {
      const na = a == null;
      const nb = b == null;
      if (na && nb) return true;
      if (na || nb) return false;
      return Number(a) === Number(b);
    };

    const postPatch = (patch, label) => {
      setToastMsg({ msg: "saving " + label + "…", level: "info" });
      fetch("/api/ops/regime_config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ regime_gating: patch }),
      })
        .then(r => r.json())
        .then((resp) => {
          if (resp.status === "ok") {
            const ch = (resp.data && resp.data.changes) || [];
            setToastMsg({ msg: "saved · " + label + (ch.length ? (" · " + ch.join("; ")) : ""), level: "ok" });
          } else {
            setToastMsg({ msg: "rejected · " + (resp.error || "unknown"), level: "warn" });
            reset();
          }
        })
        .catch((e) => {
          setToastMsg({ msg: "POST · " + e.message, level: "warn" });
          reset();
        });
    };

    const flushEntryDelta = (regime) => {
      const v = (form.entry_delta || {})[regime];
      const old = (cfg.entry_delta || {})[regime];
      if (sameDelta(v, old)) return;
      postPatch({ entry_delta: { [regime]: v } }, "entry_delta." + regime);
    };
    const flushExitDelta = (regime) => {
      const v = (form.exit_delta || {})[regime];
      const old = (cfg.exit_delta || {})[regime];
      if (sameDelta(v, old)) return;
      postPatch({ exit_delta: { [regime]: v } }, "exit_delta." + regime);
    };
    const flushScalar = (k) => {
      const v = form[k];
      const old = cfg[k];
      if (v == null || old == null) {
        if (v === old) return;
      } else if (Number(v) === Number(old)) return;
      postPatch({ [k]: Number(v) }, k);
    };

    const entryDeltaInput = (r) => {
      const cur = (form.entry_delta || {})[r];
      return h("input", {
        type: "text",
        className: "select",
        placeholder: "blank = hard block",
        value: cur === null || cur === undefined ? "" : String(cur),
        "aria-label": "entry delta " + r,
        style: { width: 92, fontFamily: "var(--mono)", fontSize: "var(--t-xs)", textAlign: "right" },
        onChange: (e) => {
          const t = e.target.value.trim();
          if (t === "") setDelta("entry_delta", r, null);
          else {
            const n = Number(t);
            if (!isNaN(n)) setDelta("entry_delta", r, n);
          }
        },
        onBlur: () => flushEntryDelta(r),
      });
    };

    const exitDeltaInput = (r) => {
      const cur = (form.exit_delta || {})[r];
      return h("input", {
        type: "number",
        className: "select",
        step: 0.01,
        value: cur != null ? cur : "",
        "aria-label": "exit delta " + r,
        style: { width: 92, fontFamily: "var(--mono)", fontSize: "var(--t-xs)", textAlign: "right" },
        onChange: (e) => {
          const t = e.target.value;
          if (t === "") return;
          setDelta("exit_delta", r, Number(t));
        },
        onBlur: () => flushExitDelta(r),
      });
    };

    const scalarKeys = scalars.slice();
    if (cfg.regime_min_stable_hours !== undefined || form.regime_min_stable_hours !== undefined) {
      if (scalarKeys.indexOf("regime_min_stable_hours") < 0) scalarKeys.push("regime_min_stable_hours");
    }

    const scalarRow = (k) => {
      const range = (schema.scalar_ranges || {})[k] || [0, 1];
      const lo = range[0];
      const hi = range[1];
      const raw = form[k];
      const v = raw != null && !isNaN(Number(raw)) ? Number(raw) : lo;
      const clamped = Math.min(hi, Math.max(lo, v));
      const step = (hi - lo) <= 0.2 ? 0.001 : 0.01;
      return h("label", {
        key: k,
        style: { display: "flex", flexDirection: "column", gap: 6 },
      },
        h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, k),
        h("div", { className: "v3-regime-slider-row" },
          h("input", {
            type: "range",
            min: lo,
            max: hi,
            step: step,
            value: clamped,
            "aria-label": k + " slider",
            onChange: (e) => setScalar(k, Number(e.target.value)),
            onMouseUp: () => flushScalar(k),
            onBlur: () => flushScalar(k),
          }),
          h("span", { className: "v3-num mono", style: { width: 56, textAlign: "right", fontSize: "var(--t-2xs)" } },
            raw != null ? Number(raw).toFixed(3) : "—")
        ),
        h("input", {
          type: "number",
          className: "select",
          step: step,
          min: lo,
          max: hi,
          value: raw != null ? raw : "",
          style: { width: 100, fontFamily: "var(--mono)", fontSize: "var(--t-xs)" },
          onChange: (e) => {
            const t = e.target.value;
            if (t === "") return;
            setScalar(k, Number(t));
          },
          onBlur: () => flushScalar(k),
        })
      );
    };

    return h(Card, {
      num: "19", title: "Regime config editor",
      sub: "blur-to-save · " + (env.config_path || "config.json"),
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
        }, h(RegimeParamsGuide))
      ),
      h("div", { className: "metric-label" }, "ENTRY DELTA · per regime"),
      h("div", { className: "v3-regime-editor-grid", style: { marginTop: 4 } },
        regimes.map(r => h("label", { key: "e-" + r, style: { display: "flex", flexDirection: "column", gap: 4 } },
          h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, r),
          entryDeltaInput(r)
        ))
      ),
      h("div", { className: "hr" }),
      h("div", { className: "metric-label" }, "EXIT DELTA · per regime"),
      h("div", { className: "v3-regime-editor-grid", style: { marginTop: 4 } },
        regimes.map(r => h("label", { key: "x-" + r, style: { display: "flex", flexDirection: "column", gap: 4 } },
          h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, r),
          exitDeltaInput(r)
        ))
      ),
      h("div", { className: "hr" }),
      h("div", { className: "metric-label" }, "SCALAR PARAMS"),
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--s-3)", marginTop: 4 } },
        scalarKeys.map(k => scalarRow(k))
      ),
      h("div", { className: "hr" }),
      h("div", { style: { display: "flex", gap: "var(--s-3)", alignItems: "center", flexWrap: "wrap" } },
        h("button", { type: "button", className: "btn", onClick: reset }, "RESET"),
        toastMsg.msg && h("div", {
          className: "v3-regime-toast " + (toastMsg.level === "ok" ? "up" : toastMsg.level === "warn" ? "down" : "dim"),
          style: { flex: 1, minWidth: 200 },
        }, toastMsg.msg)
      )
    );
  }

  // ─────────────── SLACK PREVIEW — next daily report (data-num 20) ───────────────
  function SlackPreviewLive({ data }) {
    const [, tick] = useState(0);
    useEffect(() => {
      const iv = setInterval(() => tick((n) => n + 1), 1000);
      return () => clearInterval(iv);
    }, []);
    const env = envelopeData(data.slack_preview) || {};
    const sign = (env.pnl_usd || 0) >= 0 ? "+" : "−";
    const pnlAbs = Math.abs(Number(env.pnl_usd || 0));
    const emoji = (env.pnl_usd || 0) >= 0 ? "📈" : "📉";
    const regimeRows = env.regime_distribution || [];
    const now = new Date();
    const nextUtc = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1, 0, 0, 0));
    const secLeft = Math.max(0, Math.floor((nextUtc.getTime() - now.getTime()) / 1000));
    const hh = Math.floor(secLeft / 3600);
    const mm = Math.floor((secLeft % 3600) / 60);
    const ss = secLeft % 60;
    const countdown = hh + "h " + String(mm).padStart(2, "0") + "m " + String(ss).padStart(2, "0") + "s · 00:00 UTC";
    return h(Card, {
      num: "20", title: "Slack preview · next daily brief",
      sub: "fires at 00:00 UTC · " + (env.date_utc || ""),
      right: h(F, null,
        h(TimeSince, { ts: data.slack_preview_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        h("span", { className: "pill accent" }, h("span", { className: "dot accent pulse" }), " PREVIEW")
      )
    },
      h("div", { className: "dim mono v3-num", style: { fontSize: "var(--t-2xs)", marginBottom: 8 } }, "next send · ", countdown),
      h("div", { className: "v3-slack-bubble" },
        h("div", { style: { fontWeight: 600 } },
          emoji + " Quanta · daily P&L · " + (env.date_utc || "")),
        h("div", null,
          "• Day P&L: ",
          h("span", { className: (env.pnl_usd || 0) >= 0 ? "up" : "down" },
            sign + "$" + fmtUSD(pnlAbs, 2) + "  (" + fmtPct(env.pnl_pct || 0) + ")")),
        h("div", null,
          "• Trades: ", String(env.trades || 0), " · wins ", String(env.wins || 0), " · losses ", String(env.losses || 0), " · win rate ",
          h("span", { className: "v3-num" }, (env.win_rate_pct || 0).toFixed(1)), "%"),
        h("div", null,
          "• Sharpe (trailing): ",
          env.sharpe_trailing != null ? h("span", { className: "v3-num" }, Number(env.sharpe_trailing).toFixed(2)) : "—",
          " · MaxDD: ",
          env.max_dd_trailing != null ? h("span", { className: "v3-num" }, Number(env.max_dd_trailing).toFixed(2) + "%") : "—"),
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
    const [toolFilter, setToolFilter] = useState("");
    const [selected, setSelected] = useState("");
    const [argsText, setArgsText] = useState("{}");
    const [result, setResult] = useState(null);
    const [running, setRunning] = useState(false);
    const [err, setErr] = useState(null);

    const filtered = useMemo(() => {
      const f = toolFilter.trim().toLowerCase();
      if (!f) return tools;
      return tools.filter((t) => {
        const s = (t.name + " " + (t.doc || "")).toLowerCase();
        return s.indexOf(f) >= 0;
      });
    }, [tools, toolFilter]);

    useEffect(() => {
      if (!filtered.length) return;
      const still = filtered.find((t) => t.name === selected);
      if (!still) setSelected(filtered[0].name);
    }, [filtered, selected]);

    const cur = filtered.find((t) => t.name === selected) || tools.find((t) => t.name === selected);

    useEffect(() => {
      const tmeta = tools.find((t) => t.name === selected);
      if (!tmeta) return;
      const defaults = {};
      (tmeta.params || []).forEach((p) => {
        if (p.default !== null && p.default !== undefined) defaults[p.name] = p.default;
        else if (p.type === "int") defaults[p.name] = 0;
        else if (p.type === "bool") defaults[p.name] = false;
        else defaults[p.name] = "";
      });
      setArgsText(JSON.stringify(defaults, null, 2));
      setResult(null);
      setErr(null);
    }, [selected]);

    function readMcpKey() {
      try { return sessionStorage.getItem("hermesMcpKey") || ""; } catch (_) { return ""; }
    }
    function authHeadersJson() {
      const headers = { "Content-Type": "application/json" };
      const k = readMcpKey();
      if (k) {
        headers.Authorization = "Bearer " + k;
        headers["X-Hermes-MCP-Key"] = k;
      }
      return headers;
    }

    const run = () => {
      if (!selected) return;
      const meta = tools.find((t) => t.name === selected);
      if (meta && meta.mutating) {
        let k = readMcpKey();
        if (!k) {
          const p = window.prompt("Enter X-Hermes-MCP-Key for mutating MCP tools:");
          if (!p || !String(p).trim()) { setErr("mutating tool requires X-Hermes-MCP-Key"); return; }
          try { sessionStorage.setItem("hermesMcpKey", String(p).trim()); } catch (_) { /* */ }
        }
      }
      let body;
      try { body = JSON.parse(argsText || "{}"); }
      catch (e) { setErr("invalid JSON: " + e.message); return; }
      setRunning(true);
      setErr(null);
      setResult(null);
      fetch("/api/ops/mcp/" + selected, {
        method: "POST",
        headers: authHeadersJson(),
        body: JSON.stringify(body),
      })
        .then((r) => r.json().then((j) => ({ ok: r.ok, status: r.status, j })))
        .then(({ ok, status, j }) => {
          setRunning(false);
          if (!ok) setErr("HTTP " + status + " · " + (j && j.error ? j.error : ""));
          setResult(j);
        })
        .catch((e) => { setRunning(false); setErr("fetch error: " + e.message); });
    };

    return h(Card, {
      num: "21", title: "MCP tool console",
      sub: tools.length + " tools · POST /api/ops/mcp/{name}",
      right: h(F, null,
        h(TimeSince, { ts: data.tools_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        cur && cur.mutating
          ? h("span", { className: "pill warn" }, h("span", { className: "dot warn pulse" }), " ❗ MUTATING")
          : h("span", { className: "pill" }, "read-only")
      )
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "260px 1fr", gap: "var(--s-3)" } },
        h("div", null,
          h("div", { className: "metric-label" }, "TOOL · filter"),
          h("input", {
            type: "search",
            className: "v3-mcp-console-filter",
            placeholder: "Search tools…",
            value: toolFilter,
            onChange: (e) => setToolFilter(e.target.value),
            "aria-label": "Filter MCP tools",
          }),
          h("select", {
            className: "select",
            value: selected,
            onChange: (e) => setSelected(e.target.value),
            style: { width: "100%", marginTop: 4, fontFamily: "var(--mono)", fontSize: "var(--t-xs)" }
          },
            filtered.map((t) => h("option", { key: t.name, value: t.name }, (t.mutating ? "❗ " : "") + t.name))
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

    if (slot.phase === "loading") {
      return h(Card, {
        num: "13", title: "Sentiment aggregate",
        sub: "loading…",
        right: cardRight(slot.fetchedAt),
      }, h(LoadingState));
    }

    const radar = sentimentRadarPoints(env);
    const keys = Array.isArray(env.key_events) ? env.key_events : [];
    const chip = keys.length ? String(keys[0]) : "—";

    return h(Card, {
      num: "13", title: "Sentiment aggregate",
      sub: score != null ? "net " + (score >= 0 ? "+" : "") + score.toFixed(2) : "—",
      className: "v3-sentiment-card",
      right: cardRight(slot.fetchedAt,
        h("span", { className: "pill " + klass }, score == null ? "—" : score >= 0 ? "BULLISH" : "BEARISH"))
    },
      h("div", { className: "v3-sent-radar" },
        h(
          "svg",
          {
            className: "v3-sent-radar-svg",
            // viewBox heavily padded on left + right so the longest labels
            // ("Deep" extending left of x=0, "F&G" extending right of x=120)
            // never clip. Original v3 design used a 120 box that cut both —
            // operator screenshot 2026-05-12 confirmed.
            //
            //   viewBox: -36 -22 192 164 means visible coord space:
            //     x: -36 → 156   (36px pad left, 36px pad right of 0-120)
            //     y: -22 → 142   (22px pad top, 22px pad bottom)
            width: 196,
            height: 168,
            viewBox: "-36 -22 192 164",
            "aria-label": "Sentiment four-channel radar",
          },
          h("polygon", {
            fill: "rgba(124,92,255,.18)",
            stroke: "var(--accent)",
            strokeWidth: "1.25",
            points: radar.points,
          }),
          h("polygon", {
            fill: "none",
            stroke: "var(--line-2)",
            strokeWidth: "1",
            strokeDasharray: "3 3",
            points: "60,16 104,60 60,104 16,60",
          }),
          // Labels positioned with per-side anchoring so they always sit
          // CLEARLY outside the radar polygon and never collide with the
          // diamond grid. fontSize 11, fontWeight 600, --fg-1 white.
          [
            { key: "Fast",  x: 60,  y: -6,  anchor: "middle", baseline: "auto" },
            { key: "F&G",   x: 128, y: 60,  anchor: "start",  baseline: "middle" },
            { key: "Agree", x: 60,  y: 126, anchor: "middle", baseline: "hanging" },
            { key: "Deep",  x: -8,  y: 60,  anchor: "end",    baseline: "middle" },
          ].map((item) =>
            h("text", {
              key: item.key,
              x: item.x,
              y: item.y,
              fill: "var(--fg-1)",
              fontSize: "11",
              fontWeight: 600,
              fontFamily: "var(--mono)",
              textAnchor: item.anchor,
              dominantBaseline: item.baseline,
              style: { letterSpacing: "0.04em" },
            }, item.key)
          )
        ),
        h("div", { className: "v3-sent-center" },
          h("div", { className: "dim2 mono", style: { fontSize: "var(--t-2xs)", letterSpacing: ".1em" } }, "NET"),
          h("div", { className: cls("v3-num", klass), style: { fontSize: "var(--t-2xl)", fontWeight: 500, lineHeight: 1.1 } },
            score == null ? "—" : ((score >= 0 ? "+" : "") + score.toFixed(2)))
        )
      ),
      h("div", { className: "v3-sent-headlines mono", title: chip },
        h("span", { className: "dim2", style: { marginRight: 8 } }, "HEADLINE"),
        h("span", { className: "v3-num", style: { color: "var(--fg-1)" } }, chip),
        env.age_s != null
          ? h("span", { className: "dim2 v3-num", style: { marginLeft: 12, fontSize: "var(--t-2xs)" } },
            " · age " + Math.floor(env.age_s / 60) + "m")
          : null
      ),
      h("div", {
        className: "dim2 mono v3-num",
        style: { fontSize: "var(--t-xs)", marginTop: "var(--s-2)", display: "flex", flexWrap: "wrap", gap: "var(--s-3)" },
      },
        h("span", null, "deep ", h("span", { style: { color: "var(--fg-1)" } },
          env.deep_score != null ? env.deep_score.toFixed(2) : "—")),
        h("span", null, "fast ", h("span", { style: { color: "var(--fg-1)" } },
          env.fast_score != null ? env.fast_score.toFixed(2) : "—")),
        h("span", null, "F&G ", h("span", { style: { color: "var(--fg-1)" } },
          env.fear_greed != null ? env.fear_greed : "—")),
        h("span", null, "agree ", h("span", { style: { color: "var(--fg-1)" } },
          env.community_score != null ? env.community_score.toFixed(2) : (env.agreement ? "1" : "0"))),
        h("span", null, "n=", h("span", { style: { color: "var(--fg-1)" } },
          env.n_headlines != null ? env.n_headlines : "—"))
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

    const phaseLabels = ["pre-market", "pre-execute", "market-open", "midday"];
    const curIdx = Math.max(0, phases.length - 1);

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
      h("div", { className: "v3-shark-phases" },
        phaseLabels.map((label, idx) => {
          const active = idx < phases.length && phases[idx];
          const short = (idx + 1) + " — " + label;
          return h("div", {
            key: label,
            className: "v3-shark-pill" + (idx === curIdx ? " current" : ""),
            title: active ? ((active.time || "") + " " + (active.tz || "")) : "not logged yet",
          }, short);
        })
      ),
      phases.length === 0
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no phase blocks for today yet")
        : h(F, null,
            phases.map((p, i) => h("article", {
              key: i,
              className: "v3-shark-body",
              style: { marginBottom: "var(--s-4)", paddingBottom: "var(--s-3)", borderBottom: "var(--v3-hairline, 1px solid var(--line-2))" },
            },
              h("header", { style: { fontFamily: "var(--mono)", fontSize: "var(--t-2xs)", color: "var(--fg-3)", marginBottom: 8 } },
                "Phase ", h("span", { className: "v3-num" }, String(i + 1)), " · ", p.phase, " · ", p.time || "—", " ", p.tz || ""),
              p.confirmed && p.confirmed.length
                ? h("p", { style: { color: "var(--up)", margin: "0 0 6px" } }, "Confirmed: ", p.confirmed.join(", "))
                : h("p", { className: "dim", style: { margin: "0 0 6px" } }, "Confirmed: (none)"),
              p.skipped && p.skipped.length
                ? h("p", { className: "dim", style: { margin: "0 0 6px" } }, "Skipped: ", p.skipped.join(", "))
                : null,
              p.market_summary
                ? h("p", { className: "dim", style: { margin: "0 0 6px", fontSize: "var(--t-xs)" } }, p.market_summary)
                : null,
              p.regime && h("p", { style: { margin: 0 } }, "Regime ", h("span", { className: "mono" }, p.regime),
                p.macro ? h("span", null, " · Macro ", h("span", { className: "mono" }, p.macro)) : null),
              p.lessons && h("p", { className: "dim", style: { marginTop: 6, fontSize: "var(--t-xs)" } }, p.lessons)
            )),
            env.trade_block_explanation
              ? h("div", { style: { marginTop: "var(--s-3)", padding: "var(--s-3)", background: "var(--bg-inset)", borderRadius: 4, fontSize: "var(--t-xs)", color: "var(--fg-2)", lineHeight: 1.55 } }, env.trade_block_explanation)
              : null
          )
    );
  }

  // ─────────────── MODELFORGE CHAMPION (replaces retired EPT card 14) ───────────────
  function ChampionCardLive({ data }) {
    const slot = slotState(data, "weekly_training");
    const env = envelopeData(slot.env) || {};
    const tracks = env.tracks || [];
    const summary = env.summary || {};
    const promoted = (tracks || []).filter(t => t.eligibility === "promoted" || t.current_adapter);
    const champ = promoted[0] || tracks[0] || {};
    const adapter = champ.current_adapter || "—";
    const ver = champ.current_adapter_version != null ? String(champ.current_adapter_version) : "—";
    const lastTs = champ.last_train_ts || env.week_started || null;
    let ageLabel = "—";
    if (lastTs) {
      try {
        const d = new Date(lastTs);
        if (!isNaN(d.getTime())) {
          const hrs = Math.max(0, Math.floor((Date.now() - d.getTime()) / 3600000));
          ageLabel = hrs + "h";
        }
      } catch (_) { ageLabel = "—"; }
    }

    if (slot.phase === "down") {
      return h(Card, {
        num: "14", title: "ModelForge · champion adapter",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt)
      },
        h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 60 })
      );
    }

    return h(Card, {
      num: "14", title: "ModelForge · champion adapter",
      sub: (summary.n_promoted_this_week || 0) + " promoted this week · " + (tracks.length || 0) + " tracks",
      right: cardRight(slot.fetchedAt,
        env.model_forge_reachable === false
          ? h("span", { className: "pill warn" }, "MF offline")
          : h("span", { className: "pill up" }, "MF ok"))
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "ADAPTER"), h("div", { className: "v3-num accent" }, adapter),
        h("div", { className: "dim mono" }, "VERSION"), h("div", { className: "v3-num" }, ver),
        h("div", { className: "dim mono" }, "AGE"), h("div", { className: "v3-num" }, ageLabel),
        h("div", { className: "dim mono" }, "LAST TRAIN / WEEK"), h("div", { className: "mono", style: { fontSize: "var(--t-2xs)", color: "var(--fg-2)" } },
          (lastTs || "—").replace("T", " ").slice(0, 19)),
        h("div", { className: "dim mono" }, "TRACK"), h("div", { className: "mono" }, champ.track_id || champ.role || "—")
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
    const tape = (env.live_tape || []).slice(0, 12);
    const maxAbs = tape.reduce((m, r) => {
      const a = Math.abs(Number(r.pnl_abs || 0));
      return a > m ? a : m;
    }, 0) || 1;

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
        h("div", { className: "v3-num " + (dayPnl >= 0 ? "up" : "down") }, (dayPnl >= 0 ? "+$" : "−$") + fmtUSD(Math.abs(dayPnl))),
        h("div", { className: "dim mono" }, "DAY %"),
        h("div", { className: "v3-num " + (dayPct >= 0 ? "up" : "down") }, fmtPct(dayPct)),
        h("div", { className: "dim mono" }, "DD 30d"),
        h("div", { className: "v3-num " + (dd30 != null && dd30 < 0 ? "down" : "dim") }, dd30 != null ? fmtPct(dd30) : "—"),
        h("div", { className: "dim mono" }, "OPEN"),
        h("div", { className: "v3-num" }, (env.open_count || 0) + " / " + (env.max_open || 0))
      ),
      tape.length > 0 && h("div", null,
        h("div", { className: "hr" }),
        h("div", { className: "metric-label" }, "CLOSED TAPE · 24h"),
        h("div", { className: "v3-tape" },
          tape.map((r, i) => {
            const tapePct = Number(r.pnl_pct || 0) * 100;
            const pnlAbs = Number(r.pnl_abs || 0);
            const w = Math.round((Math.abs(pnlAbs) / maxAbs) * 100);
            const barCls = pnlAbs >= 0 ? "up" : "down";
            const t = (function (iso) {
              if (!iso) return "—";
              const d = new Date(iso);
              if (isNaN(d.getTime())) return "—";
              return d.toTimeString().slice(0, 8);
            })(r.exit_time);
            const chip = (r.regime_at_entry || "EXIT").replace(/_/g, " ");
            return h("div", { key: i, className: "v3-tape-row" },
              h("span", { className: "mono v3-num dim" }, t),
              h("span", { className: "mono" }, r.pair),
              h("span", { className: "pill " + (r.side === "short" ? "down" : "info"), style: { height: 16, fontSize: "var(--t-2xs)", justifySelf: "start" } }, chip),
              h("div", { className: "v3-tape-bar-wrap" },
                h("div", {
                  className: "v3-tape-bar " + barCls,
                  style: {
                    width: w + "%",
                    marginLeft: pnlAbs < 0 ? (100 - w) + "%" : 0,
                    background: pnlAbs >= 0 ? "var(--up)" : "var(--down)",
                  },
                })
              ),
              h("span", { className: "v3-num " + (tapePct >= 0 ? "up" : "down"), style: { textAlign: "right" } }, fmtPct(tapePct))
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
    // wheel_snapshot cron only runs Mon-Fri 9am-4pm ET. Outside those
    // hours the snapshot is *expected* to drift older than the 600s/7200s
    // thresholds — surfacing stale/untrusted as tripped rows produces
    // false alarms every evening + weekend. The unified-risk breaker
    // already gates the actual trip on market_open_now; mirror that here.
    const marketOpen = cp.market_open_now !== false; // undefined → assume open (legacy safety)
    const stocksStale = !!cp.stocks_data_stale && marketOpen;
    const stocksUntrusted = !!cp.stocks_data_untrusted && marketOpen;
    const snapAge = Number(cp.snapshot_age_seconds || 0);
    const portfolioConditions = [
      { name: "combined drawdown", tripped: dd >= ddThreshold,
        detail: dd.toFixed(2) + "% / " + ddThreshold.toFixed(1) + "% threshold" },
      { name: "stocks data stale", tripped: stocksStale,
        detail: !marketOpen
          ? "market closed — gate inactive"
          : (stocksStale ? ("snapshot " + Math.round(snapAge) + "s old (limit 600s)") : "snapshot fresh") },
      { name: "stocks data untrusted", tripped: stocksUntrusted,
        detail: !marketOpen
          ? "market closed — gate inactive"
          : (stocksUntrusted ? ("snapshot >2h old — combined-dd fail-safe") : "trust window OK") },
    ];

    const trSlot = slotState(data, "trades_risk");
    const tr = envelopeData(trSlot.env) || {};
    const riskGates = (cp.sources && cp.sources.risk_gates) || {};
    const haltPct = Number(riskGates.daily_loss_halt_pct != null ? riskGates.daily_loss_halt_pct : 0.03);
    const dayFrac = Number(tr.daily_pnl_pct || 0);
    const lossUtil = haltPct > 0 ? Math.abs(dayFrac) / haltPct : 0;
    const ddUtil = ddThreshold > 0 ? Math.abs(dd) / ddThreshold : 0;
    const dotRow = [];
    dotRow.push({
      st: portfolioTripped ? "red" : "ok",
      tip: "Portfolio halt · " + (portfolioTripped ? "TRIPPED" : "armed"),
    });
    dotRow.push({
      st: ddUtil >= 1 ? "red" : (ddUtil >= 0.8 ? "amber" : "ok"),
      tip: "Combined DD · " + Math.abs(dd).toFixed(2) + "% vs kill " + ddThreshold.toFixed(1) + "%",
    });
    dotRow.push({ st: stocksStale ? "red" : "ok", tip: "Stocks data stale · " + portfolioConditions[1].detail });
    dotRow.push({ st: stocksUntrusted ? "red" : "ok", tip: "Stocks untrusted · " + portfolioConditions[2].detail });
    dotRow.push({
      st: lossUtil >= 1 ? "red" : (lossUtil >= 0.7 ? "amber" : "ok"),
      tip: "Daily loss vs halt · day " + (dayFrac * 100).toFixed(2) + "% vs halt " + (haltPct * 100).toFixed(1) + "%",
    });
    for (let bi = 0; bi < Math.min(2, breakers.length); bi++) {
      const b = breakers[bi];
      const open = b.state === "open";
      const half = b.state === "half_open";
      dotRow.push({
        st: open ? "red" : (half ? "amber" : "ok"),
        tip: (b.name || "breaker") + " · state " + (b.state || "?") + " · thr " + (b.threshold_s != null ? b.threshold_s + "s" : "—"),
      });
    }
    const padKeys = ["weekly_loss_size_cut_pct", "correlation_cap", "vix_high_multiplier"];
    let pi = 0;
    while (dotRow.length < 8) {
      const k = padKeys[pi] || "risk_gate";
      pi++;
      dotRow.push({
        st: "ok",
        tip: k + " · threshold " + String(riskGates[k] != null ? riskGates[k] : "—") + " (live metric n/a)",
      });
    }

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
      h("div", { className: "v3-cb-dots", "aria-label": "breaker overview" },
        dotRow.slice(0, 8).map((d, i) => h("span", {
          key: i,
          className: "v3-cb-dot " + (d.st === "red" ? "red" : d.st === "amber" ? "amber" : "ok"),
          title: d.tip,
        }))
      ),
      // ── Section A: portfolio breaker (unified_risk) ──
      h("div", { style: { marginBottom: "var(--s-3)" } },
        h("div", { className: "dim2 mono", style: { fontSize: "var(--t-2xs)", letterSpacing: ".08em", textTransform: "uppercase", marginBottom: "var(--s-2)" } },
          "Portfolio · unified_risk"),
        h("div", { style: { display: "flex", flexDirection: "column", gap: "var(--s-1)" } },
          portfolioConditions.map((c, i) => h("div", { key: i,
            style: { display: "flex", alignItems: "center", gap: 8, fontSize: "var(--t-xs)",
              padding: "var(--s-1) var(--s-2)", borderLeft: "2px solid " + (c.tripped ? "var(--down)" : "var(--up)"),
              background: c.tripped ? "color-mix(in srgb, var(--down) 7%, transparent)" : "transparent" } },
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

  // ─────────────── BACKTEST QUALITY GATES — strategy promotion eligibility ───
  // bt_quality_gates.sh (Sun 4am ET). Each row = one strategy with 5 gate
  // badges. Click a row to expand the numeric values + thresholds. A
  // strategy is "promotion eligible" only when all 5 gates pass — even
  // then, no automatic flip to live happens; this is operator-decision
  // surface, not automation.
  function BacktestGatesLive({ data }) {
    const [expand, setExpand] = useState(null);
    const slot = slotState(data, "backtest_gates");
    const env = envelopeData(slot.env) || {};
    const strategies = env.strategies || [];
    const summary = env.summary || {};
    const anyEligible = !!env.any_eligible;
    const anyStale = !!env.any_stale;

    // Card title pill — global state at-a-glance.
    let pillCls = "info"; let pillText = "no data";
    if (strategies.length > 0) {
      if (anyStale) { pillCls = "warn"; pillText = "STALE"; }
      else if (anyEligible) { pillCls = "up"; pillText = (summary.n_eligible || 0) + " ELIGIBLE"; }
      else { pillCls = "down"; pillText = "NONE ELIGIBLE"; }
    }

    if (slot.phase !== "ok") {
      return h(Card, {
        num: "16b", title: "Backtest quality gates",
        sub: slot.phase === "loading" ? "loading…" : "endpoint unavailable",
        right: cardRight(slot.fetchedAt)
      },
        slot.phase === "loading" ? h(LoadingState)
          : h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }

    return h(Card, {
      num: "16b", title: "Backtest quality gates",
      sub: strategies.length === 0
        ? "no reports yet — weekly cron has not run"
        : (summary.n_eligible || 0) + "/" + strategies.length + " strategies eligible for promotion"
        + (anyStale ? " · " + (summary.n_stale || 0) + " stale" : ""),
      right: cardRight(slot.fetchedAt,
        h("span", { className: "pill " + pillCls, style: { height: 18 } },
          h("span", { className: "dot " + pillCls + (anyEligible ? " pulse" : "") }), " ",
          pillText))
    },
      strategies.length === 0
        ? h("div", { className: "v3-bt-empty" },
            h("div", { style: { fontSize: "var(--t-sm)", color: "var(--fg-1)", marginBottom: 8 } }, "Awaiting first gates report"),
            h("div", { className: "dim", style: { fontSize: "var(--t-xs)", lineHeight: 1.5 } },
              env.error || "No gates_report_*_latest.json on disk yet."),
            h("div", { className: "v3-bt-countdown dim mono", style: { marginTop: 14 } },
              "NEXT RUN · Sunday ",
              h("span", { className: "v3-num" }, "04"),
              ":",
              h("span", { className: "v3-num" }, "00"),
              " ET · Hermes ",
              h("code", null, "bt_quality_gates.sh"))
          )
        : h(F, null,
            strategies[0] && (strategies[0].gates || []).length > 0 && h("div", { className: "v3-bt-waterfall" },
              h("div", { className: "metric-label", style: { marginBottom: 6 } }, "QUALITY WATERFALL · " + (strategies[0].strategy || "lead")),
              (strategies[0].gates || []).map((g, gi) => h("div", { key: gi, className: "v3-bt-wf-row" },
                h("span", { className: "mono", style: { minWidth: 110, fontSize: "var(--t-2xs)" } }, g.gate),
                h("span", {
                  className: "v3-cb-dot " + (g.pass === true ? "ok" : g.pass === false ? "red" : "amber"),
                  title: (g.detail || "") + " · value " + formatGateValue(g.value) + " / thr " + formatGateValue(g.threshold),
                }),
                h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)", flex: 1 } }, g.detail || "")
              ))
            ),
            h("div", { style: { display: "flex", flexDirection: "column", gap: 0 } },
            strategies.map((s, i) => {
              const gates = s.gates || [];
              const passing = gates.filter(g => g.pass === true).length;
              const eligible = !!s.promotion_eligible;
              const stale = !!s.stale;
              const ageH = Math.round((s.report_age_seconds || 0) / 3600);
              const ageStr = ageH < 24 ? (ageH + "h") : (Math.round(ageH / 24) + "d");
              return h(F, { key: s.strategy }, [
                h("div", {
                  key: "row",
                  onClick: () => setExpand(expand === i ? null : i),
                  style: { cursor: "pointer", display: "grid",
                    gridTemplateColumns: "minmax(140px,2fr) minmax(120px,1.5fr) minmax(80px,1fr) minmax(80px,1fr) 18px",
                    gap: "var(--s-2)", alignItems: "center",
                    padding: "var(--s-2) var(--s-2)",
                    borderBottom: "1px solid var(--line-1)",
                    fontSize: "var(--t-xs)" }
                },
                  h("strong", { style: { color: "var(--fg-1)" } }, s.strategy),
                  h("span", { style: { display: "inline-flex", gap: 4, alignItems: "center", flexWrap: "wrap" } },
                    gates.map((g, gi) => h(GateDot, {
                      key: gi, state: g.pass, label: g.gate, detail: g.detail
                    }))),
                  h("span", { className: "mono dim", style: { fontSize: "var(--t-2xs)" } },
                    passing + "/" + gates.length + " pass"),
                  h("span", { className: "pill " + (eligible ? "up" : (stale ? "warn" : "down")),
                    style: { height: 18, justifySelf: "start" } },
                    eligible ? "PROMOTE OK" : (stale ? ("STALE " + ageStr) : "BLOCKED")),
                  h("span", { className: "dim mono", style: { fontSize: "var(--t-xs)" } },
                    expand === i ? "▾" : "▸")
                ),
                expand === i && h("div", {
                  key: "exp",
                  style: { background: "var(--bg-inset)", padding: "var(--s-3) var(--s-4)",
                    borderBottom: "1px solid var(--line-1)" }
                },
                  h("div", { className: "dim2 mono", style: { fontSize: "var(--t-2xs)",
                      letterSpacing: ".08em", textTransform: "uppercase",
                      marginBottom: "var(--s-2)" } },
                    s.timerange ? "timerange " + s.timerange : "timerange n/a",
                    " · n_trades " + (s.n_trades ?? "—"),
                    " · evaluated " + (s.evaluated_at || "—"),
                    " · age " + ageStr),
                  h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--s-2) var(--s-4)" } },
                    gates.map((g, gi) => h("div", { key: gi,
                      style: { display: "flex", alignItems: "center", gap: 8, fontSize: "var(--t-xs)" } },
                      h(GateBadge, { state: g.pass === true ? "PASS" : g.pass === false ? "BLOCK" : "NA" }),
                      h("span", { style: { color: "var(--fg-1)", minWidth: 160 } }, g.gate),
                      h("span", { className: "mono", style: { fontSize: "var(--t-xs)",
                          color: g.pass ? "var(--up)" : "var(--down)", minWidth: 70, textAlign: "right" } },
                        formatGateValue(g.value)),
                      h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } },
                        " / " + formatGateValue(g.threshold)),
                      h("span", { className: "dim", style: { fontSize: "var(--t-2xs)", flex: 1, textAlign: "right" } },
                        g.detail || "")
                    )))
                ),
              ].filter(Boolean));
            })
          )
        ),
      h("div", { className: "dim", style: { fontSize: "var(--t-2xs)", padding: "var(--s-2) var(--s-2) 0",
          fontFamily: "var(--mono)" } },
        "promotion-eligible = recommendation surface only · operator must flip live by hand")
    );
  }

  function formatGateValue(v) {
    if (v == null) return "—";
    if (typeof v === "string") return v;  // "inf", "-inf", "nan"
    if (typeof v === "number") {
      if (!isFinite(v)) return v > 0 ? "inf" : "-inf";
      if (Number.isInteger(v)) return String(v);
      return v.toFixed(4);
    }
    return String(v);
  }

  // ─────────────── LLM CALLS — activity feed + drill-down modal ───────────
  // Operator's complaint (2026-05-12): the raw JSONL at
  // stocks/memory/llm-calls.jsonl was "very ugly" — no way to read it
  // without `cat`, and with SHARK_LLM_LOG_FULL_TEXT=1 each line is 1–4 KB
  // of dense JSON. This card surfaces the feed with:
  //   • headline summary: calls / tokens / avg latency / ollama share
  //   • filter dropdown (by agent) + regex search box
  //   • per-row table with latency color-coded (green/yellow/orange/red)
  //   • click any row → slide-over modal with the FULL prompt + response,
  //     copy-to-clipboard, ESC to close
  // Polling: 30s via FAST_ENDPOINTS (the index payload is metadata-only).
  function fmtLatencyClass(s) {
    // Color spec from operator:
    //   green   <2s   (fast)
    //   yellow  2-5s
    //   orange  5-15s
    //   red     >15s
    const v = Number(s) || 0;
    if (v < 2) return "up";
    if (v < 5) return "warn";
    if (v < 15) return "warn-strong";
    return "down";
  }
  function fmtHHMMSS(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    return d.toTimeString().slice(0, 8);  // HH:MM:SS
  }
  function fmtTokensCount(n) {
    if (n == null || isNaN(n)) return "—";
    if (n < 1000) return String(n);
    if (n < 1_000_000) return (n / 1000).toFixed(1) + "k";
    return (n / 1_000_000).toFixed(2) + "M";
  }

  // ── helper: pretty-print a JSON-ish blob; fall back to the raw string
  function maybePrettyJSON(text) {
    if (!text) return "";
    const s = String(text).trim();
    if (!s) return "";
    // Heuristic: starts with { or [ → try to parse + pretty-print.
    if (s[0] === "{" || s[0] === "[") {
      try {
        return JSON.stringify(JSON.parse(s), null, 2);
      } catch (_e) { /* fallthrough */ }
    }
    return s;
  }

  // ── helper: copy-to-clipboard with graceful fallback for HTTP origins
  // (clipboard API requires HTTPS; the dashboard runs on plain HTTP on the
  // LAN, so document.execCommand is the realistic path on Chrome/Firefox).
  function copyToClipboard(text) {
    if (!text) return Promise.resolve(false);
    if (navigator && navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(() => true, () => false);
    }
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return Promise.resolve(ok);
    } catch (_e) {
      return Promise.resolve(false);
    }
  }

  function LLMCallModal({ call, onClose }) {
    const [copied, setCopied] = useState(null);  // "prompt" | "response" | null
    const promptRef = useRef(null);

    // ESC to close + Cmd-F focuses the search bar in the parent (handled there).
    // Trap focus inside the modal for accessibility — start by focusing the
    // close button on mount.
    const closeBtnRef = useRef(null);
    useEffect(() => {
      function onKey(e) {
        if (e.key === "Escape") { e.preventDefault(); onClose(); }
      }
      document.addEventListener("keydown", onKey);
      if (closeBtnRef.current) closeBtnRef.current.focus();
      // Prevent body scroll while modal is open
      const prev = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      return () => {
        document.removeEventListener("keydown", onKey);
        document.body.style.overflow = prev;
      };
    }, [onClose]);

    if (!call) return null;

    const lat = Number(call.latency_seconds || 0);
    const latCls = fmtLatencyClass(lat);
    const sys = maybePrettyJSON(call.system_message);
    const promptTxt = maybePrettyJSON(call.prompt);
    const respTxt = maybePrettyJSON(call.response_text);
    const hasAnyText = !!(sys || promptTxt || respTxt);

    function doCopy(key, text) {
      copyToClipboard(text).then(ok => {
        setCopied(ok ? key : null);
        setTimeout(() => setCopied(null), 1400);
      });
    }

    const overlay = {
      position: "fixed", inset: 0, background: "rgba(0,0,0,.55)",
      zIndex: 100, display: "flex", justifyContent: "flex-end",
    };
    const panel = {
      width: "min(720px, 92vw)", height: "100vh", overflowY: "auto",
      background: "var(--bg-card)", borderLeft: "1px solid var(--line-3)",
      padding: "var(--s-4) var(--s-5)", display: "flex", flexDirection: "column",
      gap: "var(--s-3)",
    };
    const sectionHead = {
      display: "flex", alignItems: "baseline", gap: "var(--s-3)",
      borderBottom: "1px solid var(--line-1)", paddingBottom: 4,
      marginBottom: 6,
    };
    const block = {
      background: "var(--bg-inset)", border: "1px solid var(--line-1)",
      borderRadius: 4, padding: "var(--s-2) var(--s-3)",
      fontFamily: "var(--mono)", fontSize: "var(--t-xs)",
      whiteSpace: "pre-wrap", wordBreak: "break-word",
      maxHeight: 320, overflow: "auto", lineHeight: 1.5,
    };
    const lbl = (t) => h("span", { className: "metric-label" }, t);

    const meta = (k, v) => h("div", { style: { display: "flex", gap: 6, fontSize: "var(--t-xs)" } },
      h("span", { className: "dim mono", style: { minWidth: 130 } }, k),
      h("span", { className: "num", style: { fontFamily: "var(--mono)" } }, v == null ? "—" : String(v))
    );

    return h("div", {
      style: overlay,
      role: "dialog",
      "aria-modal": "true",
      "aria-label": "LLM call detail",
      onClick: (e) => { if (e.target === e.currentTarget) onClose(); }
    },
      h("div", { style: panel },
        h("div", { style: { display: "flex", alignItems: "baseline", gap: "var(--s-3)" } },
          h("h2", { style: { fontSize: "var(--t-lg)", fontWeight: 500, letterSpacing: "-.01em", margin: 0 } },
            call.agent || "unknown"),
          h("span", { className: "pill " + latCls, style: { height: 18 } },
            h("span", { className: "dot " + latCls }), " ", lat.toFixed(2) + "s"),
          h("span", { className: "tb-spacer", style: { flex: 1 } }),
          h("button", {
            ref: closeBtnRef,
            type: "button",
            onClick: onClose,
            "aria-label": "Close modal",
            title: "Close (Esc)",
            style: {
              background: "transparent", border: "1px solid var(--line-2)",
              color: "var(--fg-2)", padding: "4px 10px", cursor: "pointer",
              fontFamily: "var(--mono)", fontSize: "var(--t-xs)", borderRadius: 4,
            }
          }, "✕  esc")
        ),

        // ── METADATA TABLE ────────────────────────────────────────
        h("div", null,
          h("div", { style: sectionHead }, lbl("METADATA")),
          h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4 } },
            meta("timestamp", call.timestamp),
            meta("provider", call.provider),
            meta("model", call.model),
            meta("tier", call.tier),
            meta("role", call.role),
            meta("latency", lat.toFixed(3) + "s"),
            meta("prompt tokens", call.prompt_tokens),
            meta("completion tokens", call.completion_tokens),
            meta("total tokens", (call.prompt_tokens || 0) + (call.completion_tokens || 0)),
            meta("redacted count", call.redacted_count)
          )
        ),

        // ── SYSTEM MESSAGE ────────────────────────────────────────
        sys && h("div", null,
          h("div", { style: sectionHead },
            lbl("SYSTEM MESSAGE"),
            h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } },
              sys.length + " chars")
          ),
          h("pre", { style: block, ref: promptRef }, sys)
        ),

        // ── USER PROMPT ───────────────────────────────────────────
        promptTxt && h("div", null,
          h("div", { style: sectionHead },
            lbl("USER PROMPT"),
            h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } },
              promptTxt.length + " chars"),
            h("span", { className: "tb-spacer", style: { flex: 1 } }),
            h("button", {
              type: "button",
              onClick: () => doCopy("prompt", call.prompt || promptTxt),
              style: {
                background: "transparent", border: "1px solid var(--line-2)",
                color: copied === "prompt" ? "var(--up)" : "var(--fg-2)",
                padding: "2px 8px", cursor: "pointer",
                fontFamily: "var(--mono)", fontSize: "var(--t-2xs)", borderRadius: 4,
              }
            }, copied === "prompt" ? "✓ copied" : "copy prompt")
          ),
          h("pre", { style: block }, promptTxt)
        ),

        // ── ASSISTANT RESPONSE ────────────────────────────────────
        respTxt && h("div", null,
          h("div", { style: sectionHead },
            lbl("ASSISTANT RESPONSE"),
            h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } },
              respTxt.length + " chars"),
            h("span", { className: "tb-spacer", style: { flex: 1 } }),
            h("button", {
              type: "button",
              onClick: () => doCopy("response", call.response_text || respTxt),
              style: {
                background: "transparent", border: "1px solid var(--line-2)",
                color: copied === "response" ? "var(--up)" : "var(--fg-2)",
                padding: "2px 8px", cursor: "pointer",
                fontFamily: "var(--mono)", fontSize: "var(--t-2xs)", borderRadius: 4,
              }
            }, copied === "response" ? "✓ copied" : "copy response")
          ),
          h("pre", { style: block }, respTxt)
        ),

        // ── EMPTY-STATE NOTE WHEN FLAG WAS OFF AT WRITE TIME ──────
        !hasAnyText && h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } },
          h("strong", null, "No prompt/response on this record."),
          h("br"),
          "This call was written while ", h("code", { className: "mono" }, "SHARK_LLM_LOG_FULL_TEXT"),
          " was unset. Future calls will include the text once the flag is enabled. ",
          "Existing metadata above is the full content of the record."
        )
      )
    );
  }

  // ─────────────── AgentFlow — pipeline strip above LLM Activity ───────────────
  // Renders 5–6 boxes for the conceptual trading-bot LLM pipeline:
  //   regime_tagger → indicator_selector? → bull_debater → bear_debater
  //   → arbiter → reflector
  // Piggybacks on the same /api/ops/llm_calls payload the existing LLM
  // Activity card uses — server-side aggregates land in
  // summary.by_role_detail. No new poll. Additive: the existing list below
  // is untouched; operators still scan raw rows for forensic detail.
  //
  // Click → scrolls the LLM activity list to the role's most-recent row
  // and pulses it for 800 ms. Implemented via a CustomEvent the
  // LLMCallsLive component listens for — no shared state, no refactor of
  // the existing component.
  //
  // Opt-out: localStorage.setItem("quanta.agent_flow", "0"); reload.

  // Fixed order of strip slots — gaps are rendered as "no calls today"
  // placeholder boxes (per spec edge-case) so the operator can see which
  // pipeline stage isn't firing. ``indicator_selector`` is the one
  // exception: if it has zero calls in the window we omit it entirely
  // because today's bot doesn't emit it at all (per spec).
  const DEBATE_FLOOR_ROLES = [
    "regime_tagger",
    "bull_debater",
    "bear_debater",
    "arbiter",
    "reflector",
  ];

  function _afAgeMs(iso) {
    if (!iso) return Infinity;
    const t = Date.parse(iso);
    if (isNaN(t)) return Infinity;
    return Date.now() - t;
  }
  function _afAgeLabel(iso) {
    const ms = _afAgeMs(iso);
    if (!isFinite(ms)) return "—";
    const s = Math.round(ms / 1000);
    if (s < 60) return s + "s ago";
    const m = Math.round(s / 60);
    if (m < 60) return m + "m ago";
    const hr = Math.round(m / 60);
    if (hr < 24) return hr + "h ago";
    const d = Math.round(hr / 24);
    return d + "d ago";
  }
  function _afFreshnessClass(detail) {
    if (!detail || !detail.count) return "is-empty";
    // Failure dominates over freshness — red wins even if latest call was
    // < 5 min ago, per spec.
    if (detail.last_success === false) return "is-fail";
    const ageMs = _afAgeMs(detail.last_ts);
    if (ageMs < 5 * 60_000) return "is-fresh";
    if (ageMs < 60 * 60_000) return "is-warm";
    return "is-cold";
  }
  function _afDotClass(detail) {
    if (!detail || !detail.count) return "";
    if (detail.last_success === false) return "down";
    const ageMs = _afAgeMs(detail.last_ts);
    if (ageMs < 5 * 60_000) return "up pulse";
    if (ageMs < 60 * 60_000) return "warn";
    return "";
  }

  function _debateMaxLastTsMs(detailByRole) {
    let max = 0;
    for (let ri = 0; ri < DEBATE_FLOOR_ROLES.length; ri++) {
      const role = DEBATE_FLOOR_ROLES[ri];
      const d = detailByRole[role];
      if (d && d.last_ts) {
        const t = Date.parse(d.last_ts);
        if (!isNaN(t) && t > max) max = t;
      }
    }
    return max;
  }

  function _debateIsLive(detailByRole) {
    const max = _debateMaxLastTsMs(detailByRole);
    if (!max) return false;
    return Date.now() - max < 60_000;
  }

  // Strip JSON syntax noise from a response gist so it renders as flat
  // human-readable text. Bot responses often start with `{` and embed
  // quoted keys; raw rendering looks like `{ "grade": "C", "pattern":...`
  // which is ugly. We strip braces/quotes, replace `:` with ` ` and `,`
  // with ` · `, and collapse whitespace.
  function _afCleanGist(s) {
    if (s == null) return "";
    let t = String(s).trim();
    // Quick path: if it doesn't look like JSON, just normalize whitespace.
    if (t[0] !== "{" && t[0] !== "[" && t.indexOf('"') === -1) {
      return t.replace(/\s+/g, " ");
    }
    t = t.replace(/[{}\[\]"]/g, "");
    t = t.replace(/\s*:\s*/g, " ");
    t = t.replace(/\s*,\s*/g, " · ");
    t = t.replace(/\s+/g, " ").trim();
    // Drop dangling separator if truncated mid-pair upstream.
    if (t.endsWith("·")) t = t.slice(0, -1).trim();
    return t;
  }

  function DebateFloorConnectors({ live, activeFlows }) {
    // Each segment now carries a destination tag so we can spawn an
    // animated "message dot" traveling along the path when the
    // destination role fires (operator wanted "one agent talking to
    // another" visual — this is the explicit message-passing layer).
    //
    // Active flows (set by AgentFlow when a role's last_ts < 8s):
    //   "in-regime"   regime tagger receives → fires from card top
    //   "regime→bull" regime fans out to bull
    //   "regime→arb"  regime fans out to arbiter
    //   "regime→bear" regime fans out to bear
    //   "→reflect"    bull/bear/arb feed reflector
    //   "out-reflect" reflector publishes
    const stroke = "color-mix(in srgb, var(--fg-3) 55%, transparent)";
    const segs = [
      { d: "M 160 6 L 160 38",                        key: "in-regime",   color: "var(--v3-cold-blue)" },
      { d: "M 160 38 L 52 38 L 52 58",                key: "regime→bull", color: "var(--up)" },
      { d: "M 160 38 L 160 58",                       key: "regime→arb",  color: "var(--accent)" },
      { d: "M 160 38 L 268 38 L 268 58",              key: "regime→bear", color: "var(--down)" },
      { d: "M 52 118 L 160 150 L 268 118",            key: "→reflect",    color: "var(--warn)" },
      { d: "M 160 150 L 160 178",                     key: "out-reflect", color: "var(--warn)" },
    ];
    const flows = activeFlows || {};
    return h(
      "svg",
      {
        className: "v3-debate-svg" + (live ? " is-live" : ""),
        viewBox: "0 0 320 200",
        preserveAspectRatio: "none",
        "aria-hidden": "true",
      },
      segs.map((s, i) =>
        h(F, { key: i },
          h("path", {
            id: "v3-debate-path-" + i,
            className: "v3-debate-path" + (flows[s.key] ? " v3-debate-path-active" : ""),
            d: s.d,
            fill: "none",
            stroke: flows[s.key] ? s.color : stroke,
            strokeWidth: flows[s.key] ? 1.75 : 1.25,
            strokeLinecap: "round",
            strokeLinejoin: "round",
          }),
          // When this flow is active, render a traveling "message" dot
          // that re-keys per firing so the SMIL animation restarts.
          flows[s.key] && h("circle", {
            key: "dot-" + i + "-" + flows[s.key],
            r: 3.5,
            fill: s.color,
            opacity: 1,
            style: { filter: "drop-shadow(0 0 6px " + s.color + ")" },
          },
            h("animateMotion", {
              dur: "1.2s",
              repeatCount: "indefinite",
              path: s.d,
              rotate: "auto",
            })
          )
        )
      )
    );
  }

  // Returns ms since the role's last_ts, or null if no recent activity.
  function _afAgeMs(detail) {
    if (!detail || !detail.last_ts) return null;
    const t = new Date(detail.last_ts).getTime();
    if (!isFinite(t)) return null;
    return Date.now() - t;
  }

  function DebateRoleCard({ role, headline, subline, variant, detail, onClick }) {
    const empty = !detail || !detail.count;
    const ageMs = _afAgeMs(detail);
    // Pulse animation re-keys the DOM each time the role fires so the
    // CSS @keyframes restarts. Key changes when last_ts moves into the
    // "just fired" window (< 8 s).
    const justFired = ageMs != null && ageMs < 8000;
    const cardCls = cls(
      "v3-debate-card",
      "v3-debate-card--" + variant,
      empty ? "is-empty" : _afFreshnessClass(detail),
      justFired ? "just-fired" : ""
    );
    const boxRef = useRef(null);
    const lastGist = detail && (detail.last_response_gist || detail.last_gist);
    const cleanGist = lastGist ? _afCleanGist(lastGist) : "";
    const ariaLabel = empty
      ? role + " — idle (no calls in 24h)"
      : role + " — " + detail.count + " calls, last " + _afAgeLabel(detail.last_ts);
    const dotCls = _afDotClass(detail);
    // Force a remount whenever last_ts changes so the .just-fired
    // animation restarts cleanly on each new call.
    const remountKey = detail && detail.last_ts ? String(detail.last_ts) : "idle";
    return h("div", {
      ref: boxRef,
      key: remountKey,
      className: cardCls,
      role: "button",
      tabIndex: 0,
      "aria-label": ariaLabel,
      onClick: () => onClick(role, detail, boxRef.current),
      onKeyDown: (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick(role, detail, boxRef.current);
        }
      },
    },
      h("div", { className: "v3-debate-card-head" },
        h("span", { className: "v3-debate-role" }, headline),
        !empty && h("span", { className: "v3-debate-model-chip mono" }, detail.model || "—")
      ),
      subline && h("div", { className: "v3-debate-sub mono dim" }, subline),
      empty
        ? h("div", { className: "v3-debate-idle dim" }, "idle")
        : h(F, null,
            h("div", { className: "v3-debate-live mono" },
              dotCls && h("span", { className: "dot " + dotCls }),
              h("span", null, _afAgeLabel(detail.last_ts))
            ),
            cleanGist && h("div", {
              className: "v3-debate-gist mono dim",
              title: cleanGist,
            }, _aldTrim(cleanGist, 90))
          )
    );
  }

  function AgentFlow({ data }) {
    const slot = slotState(data, "llm_calls");
    const env = envelopeData(slot.env) || {};
    const summary = env.summary || {};
    const detailByRole = summary.by_role_detail || {};

    // Opt-out — operator can hide the strip with one console line.
    const [hidden, setHidden] = useState(() => {
      try { return localStorage.getItem("quanta.agent_flow") === "0"; }
      catch (_) { return false; }
    });
    useEffect(() => {
      function onStorage(e) {
        if (e.key === "quanta.agent_flow") {
          setHidden(e.newValue === "0");
        }
      }
      window.addEventListener("storage", onStorage);
      return () => window.removeEventListener("storage", onStorage);
    }, []);
    if (hidden) return null;

    // Re-render every 30 s so the "Xm ago" labels stay current between
    // 10 s data polls — does NOT fetch anything, just bumps state.
    const [, _tick] = useState(0);
    useEffect(() => {
      const iv = setInterval(() => _tick(n => n + 1), 30_000);
      return () => clearInterval(iv);
    }, []);

    // 1.2Hz pulse when any frozen role fired within 60s (spec §5.2).
    const [, _liveTick] = useState(0);
    useEffect(() => {
      const iv = setInterval(() => _liveTick(n => n + 1), 500);
      return () => clearInterval(iv);
    }, []);

    // Regime-change banner — when /api/ops/regime label flips, show a
    // "REGIME → X" pulse on the Agent Flow card for 30s so the operator
    // can SEE that a regime trigger just hit the debate pipeline.
    const regimeEnv = envelopeData(data.regime) || {};
    const regimeNow = regimeEnv.current && regimeEnv.current.label
      || regimeEnv.label
      || regimeEnv.regime
      || null;
    const lastRegimeRef = useRef(regimeNow);
    const [regimeFlash, setRegimeFlash] = useState(null);
    useEffect(() => {
      if (regimeNow && lastRegimeRef.current && regimeNow !== lastRegimeRef.current) {
        const from = lastRegimeRef.current;
        setRegimeFlash({ from, to: regimeNow, at: Date.now() });
        const t = setTimeout(() => setRegimeFlash(null), 30_000);
        lastRegimeRef.current = regimeNow;
        return () => clearTimeout(t);
      }
      if (regimeNow && !lastRegimeRef.current) {
        lastRegimeRef.current = regimeNow;
      }
    }, [regimeNow]);

    const click = useCallback((role, detail, originEl) => {
      // Tier E: prefer the AgentLogsDrawer. If the operator opted out
      // via ``localStorage["quanta.agent_logs_drawer"] === "0"``, fall
      // back to the Tier-D scroll-and-pulse path on the activity list.
      let useDrawer = true;
      try {
        if (localStorage.getItem("quanta.agent_logs_drawer") === "0") {
          useDrawer = false;
        }
      } catch (_e) { /* localStorage may be unavailable */ }

      if (useDrawer) {
        const evt = new CustomEvent("quanta:agent-logs-open", {
          detail: {
            role,
            model: detail && detail.model || null,
            detail: detail || null,
            originEl: originEl || null,
          },
        });
        window.dispatchEvent(evt);
        return;
      }

      // Fallback (Tier-D behavior). Fires the existing pick event so the
      // LLMCallsLive component scrolls + pulses the matching row.
      const evt = new CustomEvent("quanta:agent-flow-pick", {
        detail: {
          role,
          rawAgents: detail && detail.raw_agents ? Object.keys(detail.raw_agents) : [],
          lastTs: detail && detail.last_ts || null,
        },
      });
      window.dispatchEvent(evt);
    }, []);

    // Loading / empty states — keep the strip visible but show a thin
    // skeleton so the operator can tell the difference between
    // "endpoint unreachable" and "no calls today".
    const empty = !env || Object.keys(detailByRole).length === 0;
    const subText = slot.phase === "loading" && empty
      ? "loading…"
      : slot.phase === "down"
        ? "endpoint unavailable — placeholders only"
        : empty
          ? "no canonical-role calls in 24h window — courtroom shows all five roles"
          : Object.keys(detailByRole).length + " roles with data · "
            + (summary.total_calls || 0) + " calls in 24h · Debate Floor";

    const debateLive = _debateIsLive(detailByRole);
    const regime = detailByRole.regime_tagger || null;
    const bull = detailByRole.bull_debater || null;
    const bear = detailByRole.bear_debater || null;
    const arb = detailByRole.arbiter || null;
    const refl = detailByRole.reflector || null;

    // Build a Phase Strip showing where in the canonical debate cycle
    // we are. "fired" = role has any call in the last 5 min;
    // "firing" = role's last call was within 8 s (currently animating).
    const _PHASES = [
      { key: "regime",  label: "REGIME",   detail: regime },
      { key: "bull",    label: "BULL",     detail: bull },
      { key: "bear",    label: "BEAR",     detail: bear },
      { key: "arbiter", label: "ARBITER",  detail: arb },
      { key: "reflect", label: "REFLECT",  detail: refl },
    ];
    const phaseSteps = _PHASES.map((p) => {
      const ms = _afAgeMs(p.detail);
      return {
        label: p.label,
        firing: ms != null && ms < 8000,
        fired:  ms != null && ms < 300_000,
      };
    });

    // Build the activeFlows map for the connector SVG. A flow segment
    // is "active" when its destination role has fired recently (last 8 s).
    // Value is the role's last_ts so the SVG re-keys + restarts the
    // animateMotion on each new fire.
    const _flowKey = (det) => det && det.last_ts ? String(det.last_ts) : "";
    const regimeMs = _afAgeMs(regime);
    const bullMs   = _afAgeMs(bull);
    const bearMs   = _afAgeMs(bear);
    const arbMs    = _afAgeMs(arb);
    const reflMs   = _afAgeMs(refl);
    const activeFlows = {
      "in-regime":   regimeMs != null && regimeMs < 8000 ? _flowKey(regime) : null,
      "regime→bull": bullMs != null && bullMs < 8000 ? _flowKey(bull) : null,
      "regime→arb":  arbMs != null && arbMs < 8000 ? _flowKey(arb) : null,
      "regime→bear": bearMs != null && bearMs < 8000 ? _flowKey(bear) : null,
      "→reflect":    reflMs != null && reflMs < 8000 ? _flowKey(refl) : null,
      "out-reflect": reflMs != null && reflMs < 8000 ? _flowKey(refl) : null,
    };

    return h(Card, {
      num: "21a",
      title: "Agent flow",
      sub: subText,
      right: h("div", { style: { display: "flex", alignItems: "center", gap: 8 } },
        regimeNow ? h("span", {
          className: "pill " + (regimeFlash ? "warn" : "info"),
          style: { fontSize: "var(--t-2xs)" },
          title: "current macro regime · debate fires on transition",
        }, "REGIME · " + String(regimeNow).toUpperCase()) : null,
        cardRight(slot.fetchedAt),
      ),
    },
      h("div", {
        className: "v3-debate-phase-strip",
        role: "group",
        "aria-label": "Debate phase indicator — regime → bull → bear → arbiter → reflector",
      },
        phaseSteps.flatMap((step, i) => {
          const items = [];
          if (i > 0) {
            items.push(h("span", {
              key: "arr-" + i,
              className: cls("v3-debate-phase-arrow", step.firing || phaseSteps[i - 1].firing ? "lit" : ""),
            }, "→"));
          }
          items.push(h("span", {
            key: step.label,
            className: cls("v3-debate-phase-step",
              step.firing ? "firing" : (step.fired ? "fired" : "")),
            title: step.firing ? "firing now" : (step.fired ? "fired in last 5 min" : "idle"),
          }, step.label));
          return items;
        })
      ),
      regimeFlash && h("div", {
        className: "v3-regime-flash",
        role: "status",
        "aria-live": "polite",
        style: {
          padding: "var(--s-2) var(--s-3)",
          marginBottom: "var(--s-2)",
          borderRadius: 6,
          background: "color-mix(in srgb, var(--warn) 18%, transparent)",
          border: "1px solid color-mix(in srgb, var(--warn) 60%, transparent)",
          fontFamily: "var(--mono)",
          fontSize: "var(--t-xs)",
          color: "var(--fg-1)",
          display: "flex",
          alignItems: "center",
          gap: 10,
        },
      },
        h("span", { className: "dot", style: { background: "var(--warn)", width: 8, height: 8, borderRadius: 4 } }),
        h("span", null, "REGIME CHANGED · "),
        h("span", { className: "dim2" }, regimeFlash.from),
        h("span", null, " → "),
        h("span", { style: { fontWeight: 600 } }, regimeFlash.to),
        h("span", { className: "dim2", style: { marginLeft: "auto", fontSize: "var(--t-2xs)" } },
          "debate triggered · watching " + Object.keys(detailByRole).length + " roles for activity"),
      ),
      h("div", { className: "v3-debate-floor", id: "agent-flow-strip" },
        h(DebateFloorConnectors, { live: debateLive, activeFlows: activeFlows }),
        debateLive && h("div", { className: "v3-debate-live-anchor" },
          h("span", { className: "v3-debate-live-pill", "aria-live": "polite" }, "DEBATE LIVE")),
        h("div", { className: "v3-debate-grid" },
          h("div", { className: "v3-debate-cell v3-debate-cell--regime" },
            h(DebateRoleCard, {
              role: "regime_tagger",
              headline: "REGIME TAGGER",
              subline: "scout · top of arena",
              variant: "cold",
              detail: regime,
              onClick: click,
            })
          ),
          h("div", { className: "v3-debate-cell v3-debate-cell--bull" },
            h(DebateRoleCard, {
              role: "bull_debater",
              headline: "BULL",
              subline: "assesses upside",
              variant: "bull",
              detail: bull,
              onClick: click,
            })
          ),
          h("div", { className: "v3-debate-cell v3-debate-cell--arbiter" },
            h(DebateRoleCard, {
              role: "arbiter",
              headline: "ARBITER ⚖",
              subline: "scales",
              variant: "arbiter",
              detail: arb,
              onClick: click,
            })
          ),
          h("div", { className: "v3-debate-cell v3-debate-cell--bear" },
            h(DebateRoleCard, {
              role: "bear_debater",
              headline: "BEAR",
              subline: "assesses downside",
              variant: "bear",
              detail: bear,
              onClick: click,
            })
          ),
          h("div", { className: "v3-debate-cell v3-debate-cell--reflector" },
            h(DebateRoleCard, {
              role: "reflector",
              headline: "REFLECTOR",
              subline: "post-mortem writer",
              variant: "reflector",
              detail: refl,
              onClick: click,
            })
          )
        )
      )
    );
  }

  // ─────────────── AgentLogsDrawer — Tier E ────────────────────────────
  // Right-anchored slide-in panel that lists the last 50 calls for ONE
  // canonical AgentFlow role (bull_debater, bear_debater, arbiter, …).
  // Replaces the Tier-D scroll-and-pulse behavior when an agent box is
  // clicked. The old behavior is still available as a fallback via
  // ``localStorage.setItem("quanta.agent_logs_drawer", "0")``.
  //
  // - Mounts to document.body via ReactDOM.createPortal so the strip
  //   never reflows. z-index 81 (above topbar, below LLM modal).
  // - Fetches /api/ops/llm_calls?role=ROLE&include_text=1&limit=50 ONCE
  //   on open. No new poll loop.
  // - Click a different agent box while open → re-fetches, no flicker
  //   (transform stays at translateX(0)).
  // - ESC or backdrop click closes; focus returns to the originally
  //   clicked box.
  //
  // Open via: window.dispatchEvent(new CustomEvent("quanta:agent-logs-open",
  //   { detail: { role, model, detail, originEl } }))

  function _aldFmtTime(iso) {
    if (!iso) return "—";
    try {
      const d = new Date(iso);
      if (isNaN(d.getTime())) return "—";
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      return hh + ":" + mm + ":" + ss;
    } catch (_e) { return "—"; }
  }
  function _aldTrim(s, n) {
    if (!s) return "";
    const flat = String(s).replace(/\s+/g, " ").trim();
    if (flat.length <= n) return flat;
    return flat.slice(0, n) + "…";
  }
  function _aldCallText(c, field) {
    // Records sometimes ship `prompt` as a JSON-encoded string; show
    // raw text when available. Falls back to system_message for the
    // prompt side if `prompt` is empty.
    if (field === "prompt") {
      return c.prompt || c.system_message || "";
    }
    return c.response_text || "";
  }

  function _AldEntry({ call, role, p95, query }) {
    const [expandPrompt, setExpandPrompt] = useState(false);
    const [expandResp, setExpandResp] = useState(false);
    const [copied, setCopied] = useState(null);  // "prompt" | "response" | null

    const lat = Number(call.latency_seconds || 0);
    const cTok = Number(call.completion_tokens || 0);
    const pTok = Number(call.prompt_tokens || 0);
    const totalTok = cTok + pTok;
    const failed = call.success === false || !!call.error || (lat === 0 && cTok === 0);
    const slow = isFinite(p95) && p95 > 0 && lat > p95;
    const prompt = _aldCallText(call, "prompt");
    const response = _aldCallText(call, "response");

    function doCopy(key, text) {
      copyToClipboard(text || "").then(ok => {
        if (!ok) return;
        setCopied(key);
        setTimeout(() => setCopied(null), 600);
      });
    }

    // Highlight matched text — for now we just rely on the row being
    // visible (filter already narrowed by query). Highlighting inside
    // <pre> would need DOM-string surgery; out of scope for Tier E.
    void query;

    const cls = "ald-entry" + (failed ? " is-fail" : "");
    return h("div", { className: cls },
      h("div", { className: "ald-entry-head" },
        h("span", { className: "ald-time" }, _aldFmtTime(call.timestamp)),
        h("span", { className: "ald-model" }, call.model || "—"),
        h("span", { className: failed ? "ald-status-bad" : "ald-status-ok" },
          failed ? "✕" : "✓"),
        h("span", { className: "ald-lat" }, lat.toFixed(2) + "s"),
        slow && h("span", { className: "ald-slow", title: "slower than p95" }, "slow"),
        h("span", { className: "ald-tok" }, totalTok + " tok")
      ),
      // Prompt line
      prompt && h("div", { className: "ald-line" },
        h("button", {
          type: "button",
          className: "ald-caret",
          "aria-expanded": expandPrompt,
          "aria-label": expandPrompt ? "collapse prompt" : "expand prompt",
          onClick: () => setExpandPrompt(v => !v),
        }, expandPrompt ? "▾" : "▸"),
        h("span", { className: "ald-label" }, "prompt:"),
        !expandPrompt && h("span", {
          className: "ald-snippet",
          title: "click to expand",
          onClick: () => setExpandPrompt(true),
        }, _aldTrim(prompt, 120)),
        h("button", {
          type: "button",
          className: "ald-copy" + (copied === "prompt" ? " is-flashed" : ""),
          title: copied === "prompt" ? "copied" : "copy prompt",
          "aria-label": "copy prompt",
          onClick: () => doCopy("prompt", prompt),
        }, copied === "prompt" ? "✓" : "📋")
      ),
      expandPrompt && prompt && h("pre", { className: "ald-pre" }, prompt),
      // Response line
      response && h("div", { className: "ald-line" },
        h("button", {
          type: "button",
          className: "ald-caret",
          "aria-expanded": expandResp,
          "aria-label": expandResp ? "collapse response" : "expand response",
          onClick: () => setExpandResp(v => !v),
        }, expandResp ? "▾" : "▸"),
        h("span", { className: "ald-label" }, "response:"),
        !expandResp && h("span", {
          className: "ald-snippet",
          title: "click to expand",
          onClick: () => setExpandResp(true),
        }, _aldTrim(response, 120)),
        h("button", {
          type: "button",
          className: "ald-copy" + (copied === "response" ? " is-flashed" : ""),
          title: copied === "response" ? "copied" : "copy response",
          "aria-label": "copy response",
          onClick: () => doCopy("response", response),
        }, copied === "response" ? "✓" : "📋")
      ),
      expandResp && response && h("pre", { className: "ald-pre" }, response),
      // No-text fallback so the operator can see we still rendered the call
      !prompt && !response && h("div", { className: "ald-line" },
        h("span", { className: "ald-label" },
          "no prompt/response captured (SHARK_LLM_LOG_FULL_TEXT was off)")
      )
    );
  }

  function AgentLogsDrawer() {
    // ── Drawer-level state ──────────────────────────────────────────
    const [open, setOpen] = useState(false);
    const [role, setRole] = useState(null);
    const [model, setModel] = useState(null);
    const [detail, setDetail] = useState(null);    // by_role_detail entry
    const [calls, setCalls] = useState([]);
    const [phase, setPhase] = useState("idle");    // idle|loading|ok|error
    const [errMsg, setErrMsg] = useState("");
    const [filter, setFilter] = useState("all");   // all|success|failures|slow
    const [search, setSearch] = useState("");
    const [searchInput, setSearchInput] = useState("");
    const [shown, setShown] = useState(20);
    const originElRef = useRef(null);
    const closeBtnRef = useRef(null);
    const searchRef = useRef(null);
    const reqIdRef = useRef(0);

    // Debounce search 200 ms
    useEffect(() => {
      const t = setTimeout(() => setSearch(searchInput), 200);
      return () => clearTimeout(t);
    }, [searchInput]);

    // Reset pagination + filter when role changes
    useEffect(() => {
      setShown(20);
      setFilter("all");
      setSearchInput("");
      setSearch("");
    }, [role]);

    function doFetch(r) {
      if (!r) return;
      const myReq = ++reqIdRef.current;
      setPhase("loading");
      setErrMsg("");
      const url = "/api/ops/llm_calls?role=" + encodeURIComponent(r)
        + "&include_text=1&limit=50";
      fetch(url)
        .then(res => res.ok ? res.json() : Promise.reject(new Error("HTTP " + res.status)))
        .then(env => {
          if (myReq !== reqIdRef.current) return;  // stale response
          const data = envelopeData(env) || {};
          const list = Array.isArray(data.calls) ? data.calls : [];
          setCalls(list);
          setPhase("ok");
        })
        .catch(err => {
          if (myReq !== reqIdRef.current) return;
          setErrMsg(String(err && err.message || err));
          setPhase("error");
        });
    }

    // ── Listen for open events ─────────────────────────────────────
    useEffect(() => {
      function onOpen(e) {
        const d = (e && e.detail) || {};
        // Stash the click origin so we can return focus to it on close.
        originElRef.current = d.originEl || null;
        setRole(d.role || null);
        setModel(d.model || null);
        setDetail(d.detail || null);
        setOpen(true);
        doFetch(d.role);
      }
      window.addEventListener("quanta:agent-logs-open", onOpen);
      return () => window.removeEventListener("quanta:agent-logs-open", onOpen);
    }, []);

    // ── ESC to close + body-scroll lock + focus management ────────
    useEffect(() => {
      if (!open) return;
      function onKey(e) {
        if (e.key === "Escape") { e.preventDefault(); close(); }
      }
      document.addEventListener("keydown", onKey);
      const prev = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      // Focus first interactive on open
      requestAnimationFrame(() => {
        if (closeBtnRef.current) closeBtnRef.current.focus();
      });
      return () => {
        document.removeEventListener("keydown", onKey);
        document.body.style.overflow = prev;
      };
    }, [open]);

    function close() {
      setOpen(false);
      // Return focus to the originating box
      const el = originElRef.current;
      if (el && typeof el.focus === "function") {
        try { el.focus(); } catch (_e) { /* ignore */ }
      }
    }

    function retry() { doFetch(role); }

    // ── Derived: counts per filter ─────────────────────────────────
    const p95 = (detail && Number(detail.p95_latency_s)) || 0;
    const counts = useMemo(() => {
      let ok = 0, bad = 0, slow = 0;
      for (const c of calls) {
        const lat = Number(c.latency_seconds || 0);
        const cTok = Number(c.completion_tokens || 0);
        const failed = c.success === false || !!c.error || (lat === 0 && cTok === 0);
        if (failed) bad++; else ok++;
        if (p95 > 0 && lat > p95) slow++;
      }
      return { all: calls.length, success: ok, failures: bad, slow };
    }, [calls, p95]);

    const filtered = useMemo(() => {
      const q = (search || "").toLowerCase();
      return calls.filter(c => {
        const lat = Number(c.latency_seconds || 0);
        const cTok = Number(c.completion_tokens || 0);
        const failed = c.success === false || !!c.error || (lat === 0 && cTok === 0);
        if (filter === "success" && failed) return false;
        if (filter === "failures" && !failed) return false;
        if (filter === "slow" && !(p95 > 0 && lat > p95)) return false;
        if (q) {
          const hay = ((_aldCallText(c, "prompt") || "") + " "
                    + (_aldCallText(c, "response") || "")).toLowerCase();
          if (hay.indexOf(q) === -1) return false;
        }
        return true;
      });
    }, [calls, filter, search, p95]);

    // Always render so the drawer can animate in. When closed, the
    // .is-open class is removed → transform slides it off-screen +
    // backdrop pointer-events disables, so nothing intercepts clicks.
    const showModel = model || (detail && detail.model) || "—";
    const title = role
      ? "AGENT LOGS — " + role + (showModel && showModel !== "—" ? " · " + showModel : "")
      : "AGENT LOGS";

    function aggregateLine() {
      const empty = !detail || !detail.count;
      if (empty) {
        return h("span", { className: "agg-empty" }, "no calls yet");
      }
      const ok = Number(detail.success || 0);
      const bad = Number(detail.fail || 0);
      const avg = Number(detail.avg_latency_s || 0).toFixed(1);
      const p95v = Number(detail.p95_latency_s || 0).toFixed(1);
      const tokAvg = Math.round(Number(detail.tokens_avg || 0));
      return h(F, null,
        "today: ",
        h("span", { className: "agg-ok" }, ok),
        " ✓",
        h("span", { className: "agg-sep" }, "·"),
        h("span", { className: "agg-bad" }, bad),
        " ✕",
        h("span", { className: "agg-sep" }, "·"),
        "avg ", avg, "s",
        h("span", { className: "agg-sep" }, "·"),
        "p95 ", p95v, "s",
        h("span", { className: "agg-sep" }, "·"),
        tokAvg, " tokens/call avg"
      );
    }

    function bodyContent() {
      if (phase === "loading") return h("div", { className: "ald-loading" }, "loading…");
      if (phase === "error") {
        return h("div", { className: "ald-error" },
          "fetch failed: " + errMsg,
          h("button", { type: "button", onClick: retry }, "retry"));
      }
      if (filtered.length === 0) {
        const reason = (calls.length === 0)
          ? "no calls for this role in the 24h window"
          : "no matches for current filter / search";
        return h("div", { className: "ald-empty" }, reason);
      }
      const slice = filtered.slice(0, shown);
      const remaining = filtered.length - slice.length;
      return h(F, null,
        slice.map((c, i) => h(_AldEntry, {
          key: (c.timestamp || "") + "_" + i,
          call: c, role, p95, query: search,
        })),
        remaining > 0 && h("button", {
          type: "button",
          className: "ald-show-more",
          onClick: () => setShown(n => n + 30),
        }, "show " + Math.min(30, remaining) + " more (" + remaining + " hidden)")
      );
    }

    const drawerEl = h(F, null,
      h("div", {
        className: "ald-backdrop" + (open ? " is-open" : ""),
        onClick: close,
        "aria-hidden": "true",
      }),
      h("div", {
        className: "ald-drawer" + (open ? " is-open" : ""),
        role: "dialog",
        "aria-modal": "true",
        "aria-label": role ? ("agent logs for " + role) : "agent logs",
        tabIndex: -1,
      },
        h("div", { className: "ald-head" },
          h("span", { className: "ald-title" }, title),
          h("button", {
            ref: closeBtnRef,
            type: "button",
            className: "ald-close",
            onClick: close,
            "aria-label": "close drawer",
            title: "close (esc)",
          }, "×")
        ),
        h("div", { className: "ald-aggregate" }, aggregateLine()),
        h("div", { className: "ald-toolbar" },
          ["all", "success", "failures", "slow"].map(k =>
            h("button", {
              key: k,
              type: "button",
              className: "ald-chip" + (filter === k ? " is-active" : ""),
              onClick: () => setFilter(k),
              "aria-pressed": filter === k,
            }, k + " (" + (counts[k] || 0) + ")")
          ),
          h("input", {
            ref: searchRef,
            type: "text",
            className: "ald-search",
            placeholder: "search prompt + response…",
            value: searchInput,
            onChange: (e) => setSearchInput(e.target.value),
            "aria-label": "search prompt and response",
          })
        ),
        h("div", { className: "ald-body" }, bodyContent())
      )
    );

    // Portal to body so the drawer escapes the AgentFlow card layout.
    return ReactDOM.createPortal(drawerEl, document.body);
  }

  function parseGradeFromGistText(text) {
    if (text == null) return null;
    const t = String(text);
    const key = "\"grade\"";
    let pos = 0;
    while (true) {
      const i = t.indexOf(key, pos);
      if (i < 0) return null;
      const slice = t.slice(i, i + 48);
      if (slice.indexOf("\"A\"") >= 0 || slice.indexOf("'A'") >= 0) return "A";
      if (slice.indexOf("\"B\"") >= 0 || slice.indexOf("'B'") >= 0) return "B";
      if (slice.indexOf("\"C\"") >= 0 || slice.indexOf("'C'") >= 0) return "C";
      pos = i + 1;
    }
  }

  function llmActivityRowRoleTag(agent) {
    const a = String(agent || "").toLowerCase();
    if (a.indexOf("trade_reviewer") >= 0) {
      return { label: "ARBITER", cls: "v3-llm-tag--arbiter" };
    }
    if (a.indexOf("aggressive") >= 0) {
      return { label: "BULL", cls: "v3-llm-tag--bull" };
    }
    if (a.indexOf("conservative") >= 0) {
      return { label: "BEAR", cls: "v3-llm-tag--bear" };
    }
    if (a.indexOf("neutral") >= 0) {
      return { label: "RISK", cls: "v3-llm-tag--risk" };
    }
    return { label: "LLM", cls: "v3-llm-tag--def" };
  }

  function LLMCallsLive({ data }) {
    const slot = slotState(data, "llm_calls");
    const env = envelopeData(slot.env) || {};
    const summary = env.summary || {};
    const callsAll = env.calls || [];
    const logSize = env.log_size_bytes || 0;

    // Local UI state — filter + search are client-side so the operator
    // doesn't pay an extra round-trip per keystroke. Server-side filtering
    // is still available via the same endpoint params for the modal's
    // "search across the whole window" flow.
    const [agentFilter, setAgentFilter] = useState("");
    const [tierPill, setTierPill] = useState("all");
    const [search, setSearch] = useState("");
    const [shown, setShown] = useState(50);  // "load more" pagination
    const [selectedTs, setSelectedTs] = useState(null);
    const [modalRec, setModalRec] = useState(null);
    const [modalLoading, setModalLoading] = useState(false);
    const [modalError, setModalError] = useState(null);
    const searchRef = useRef(null);
    // Tier C P1-2: track current in-flight modal fetch so closeModal /
    // unmount can abort it. ESC also triggers closeModal which now aborts
    // mid-flight — operator can cancel a slow LLM-call drilldown.
    const modalCtrlRef = useRef(null);

    // Cmd-F / Ctrl-F focuses the search input (within this card only —
    // we don't fight the browser shortcut globally).
    useEffect(() => {
      function onKey(e) {
        if ((e.metaKey || e.ctrlKey) && e.key === "f") {
          // Only intercept when the LLM card is in viewport — otherwise
          // let browser find-in-page handle it as normal.
          const card = document.getElementById("llm-calls-card");
          if (!card) return;
          const rect = card.getBoundingClientRect();
          if (rect.bottom < 0 || rect.top > window.innerHeight) return;
          e.preventDefault();
          if (searchRef.current) searchRef.current.focus();
        }
      }
      document.addEventListener("keydown", onKey);
      return () => document.removeEventListener("keydown", onKey);
    }, []);

    // Listen for AgentFlow box clicks. Scrolls the table to the newest
    // matching row (by raw agent name) and pulses it for 800 ms. Falls
    // back to clearing the agent filter so the matching row is visible
    // if it was hidden by a previous filter. Additive integration — does
    // not change row behavior in any other code path.
    useEffect(() => {
      function onPick(e) {
        const d = (e && e.detail) || {};
        const raws = Array.isArray(d.rawAgents) ? d.rawAgents : [];
        // If a filter is set and it doesn't match any of the role's raw
        // agents, drop it so the scroll target becomes visible.
        if (agentFilter && !raws.includes(agentFilter)) {
          setAgentFilter("");
          setTierPill("all");
        }
        // Defer DOM lookup to next paint so the (possibly-reset) filter
        // has a chance to re-render.
        requestAnimationFrame(() => {
          let target = null;
          if (d.lastTs) {
            target = document.querySelector('[data-llm-ts="' + d.lastTs.replace(/"/g, '\\"') + '"]');
          }
          if (!target && raws.length) {
            // First (newest) row with a matching agent. The list is
            // already newest-first.
            for (const raw of raws) {
              const nodes = document.querySelectorAll('[data-llm-agent="' + raw.replace(/"/g, '\\"') + '"]');
              if (nodes.length) { target = nodes[0]; break; }
            }
          }
          if (!target) return;
          target.scrollIntoView({ behavior: "smooth", block: "center" });
          target.classList.remove("af-pulse-row");
          // Force reflow so the animation restarts when clicked twice.
          void target.offsetWidth;
          target.classList.add("af-pulse-row");
          setTimeout(() => target.classList.remove("af-pulse-row"), 900);
        });
      }
      window.addEventListener("quanta:agent-flow-pick", onPick);
      return () => window.removeEventListener("quanta:agent-flow-pick", onPick);
    }, [agentFilter, tierPill]);

    // Click a row → fetch full record (include_text=1) for the modal.
    const openCall = useCallback((rec) => {
      if (!rec || !rec.timestamp) return;
      // Tier C P1-2: cancel any in-flight previous open so rapid-click on
      // different rows doesn't race; the latest click always wins.
      if (modalCtrlRef.current) modalCtrlRef.current.abort();
      const ctrl = new AbortController();
      modalCtrlRef.current = ctrl;
      setSelectedTs(rec.timestamp);
      setModalLoading(true); setModalError(null); setModalRec(rec);  // optimistic
      const url = "/api/ops/llm_calls/" + encodeURIComponent(rec.timestamp);
      fetch(url, { signal: ctrl.signal })
        .then(r => {
          if (r.ok) return r.json();
          if (r.status === 410) {
            return r.json().then(j => {
              const detail = j.detail || {};
              if (detail.call) return { status: "ok", data: { call: detail.call, source: "archive", archive_path: detail.archive_path } };
              throw new Error("record archived: " + (detail.hint || detail.error || ""));
            });
          }
          throw new Error("HTTP " + r.status);
        })
        .then(env => {
          if (ctrl.signal.aborted) return;
          const c = (env && env.data && env.data.call) || rec;
          setModalRec(c);
          setModalLoading(false);
        })
        .catch(err => {
          if (isAbortError(err)) return;
          // Fallback: render the row data we have (metadata-only) and
          // surface the error so operator knows full text isn't available.
          setModalRec(rec);
          setModalError(String(err && err.message || err));
          setModalLoading(false);
        });
    }, []);

    const closeModal = useCallback(() => {
      // P1-2: abort any in-flight modal fetch so closing the modal
      // (including via ESC) cancels the request immediately.
      if (modalCtrlRef.current) { modalCtrlRef.current.abort(); modalCtrlRef.current = null; }
      setSelectedTs(null);
      setModalRec(null);
      setModalError(null);
    }, []);

    // P1-2: cleanup any in-flight modal fetch when the card unmounts.
    useEffect(() => {
      return () => {
        if (modalCtrlRef.current) { modalCtrlRef.current.abort(); modalCtrlRef.current = null; }
      };
    }, []);

    // ── Filter rows client-side ──────────────────────────────────
    const agentOptions = useMemo(() => {
      const set = new Set();
      callsAll.forEach(c => { if (c.agent) set.add(c.agent); });
      return Array.from(set).sort();
    }, [callsAll]);

    const filtered = useMemo(() => {
      let pat = null;
      if (search) {
        try { pat = new RegExp(search, "i"); }
        catch (_e) { pat = null; }
      }
      return callsAll.filter(c => {
        if (agentFilter && c.agent !== agentFilter) return false;
        if (tierPill === "fast" && c.tier !== "fast") return false;
        if (tierPill === "deep" && c.tier !== "deep") return false;
        if (tierPill === "ollama" && c.provider !== "ollama") return false;
        if (tierPill === "anthropic" && c.provider !== "anthropic") return false;
        if (pat) {
          const hay = [c.agent, c.model, c.tier, c.role, c.provider]
            .filter(Boolean).join(" ");
          if (!pat.test(hay)) return false;
        }
        return true;
      });
    }, [callsAll, agentFilter, tierPill, search]);

    // Skeleton — only show loading on first paint; subsequent polls
    // re-use the previous data so the table doesn't flash.
    if (slot.phase === "loading" && callsAll.length === 0) {
      return h(Card, {
        num: "21", title: "LLM activity · last 24h",
        sub: "loading…",
        right: cardRight(slot.fetchedAt),
      }, h(LoadingState));
    }
    if (slot.phase === "down" && callsAll.length === 0) {
      return h(Card, {
        num: "21", title: "LLM activity · last 24h",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt),
      }, h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 30 }));
    }

    // ── Layout ───────────────────────────────────────────────────
    const stat = (lbl, val, toneCls) => h("div", { style: { display: "flex", flexDirection: "column", gap: 2, minWidth: 100 } },
      h("div", { className: "dim2 mono", style: { fontSize: "var(--t-2xs)", letterSpacing: ".08em", textTransform: "uppercase" } }, lbl),
      h("div", { className: cls("num", "v3-num", toneCls || ""), style: { fontSize: "var(--t-md)", fontFamily: "var(--mono)", fontVariantNumeric: "tabular-nums" } }, val)
    );

    const ollamaPct = Number(summary.ollama_pct || 0);
    const ollamaCls = ollamaPct >= 80 ? "up" : ollamaPct >= 50 ? "warn" : "down";
    const successCls = Number(summary.success_pct || 100) >= 99 ? "up"
                    : Number(summary.success_pct || 100) >= 95 ? "warn" : "down";
    const isEmpty = callsAll.length === 0;
    const byTier = summary.by_tier || {};
    const providers = summary.providers || {};
    const fastCount = Number(byTier.fast || 0);
    const deepCount = Number(byTier.deep || 0);
    const totalCalls = Number(summary.total_calls || 0);
    const ollamaCalls = providers.ollama != null ? Number(providers.ollama) : Math.round(totalCalls * ollamaPct / 100);
    const anthropicCalls = Math.max(0, Math.round(totalCalls * (1 - ollamaPct / 100)));
    const arbGist = (summary.by_role_detail && summary.by_role_detail.arbiter && summary.by_role_detail.arbiter.last_gist) || "";

    // Stale detection — feed is "fresh" if any call in last 5 min, "warm"
    // up to 30 min, "stale" beyond that. Operator-visible so they can tell
    // "no recent activity" vs "log file frozen".
    const latestCallMs = (() => {
      if (!callsAll.length) return null;
      const ts = callsAll[0] && (callsAll[0].timestamp || callsAll[0].ts);
      const t = ts ? new Date(ts).getTime() : NaN;
      return isFinite(t) ? Date.now() - t : null;
    })();
    const staleClass = latestCallMs == null ? "info"
      : latestCallMs < 5 * 60_000  ? "up"
      : latestCallMs < 30 * 60_000 ? "warn"
      :                              "down";
    const staleLabel = latestCallMs == null ? null
      : latestCallMs < 60_000      ? "live · just now"
      : latestCallMs < 3_600_000   ? "quiet · last call " + Math.floor(latestCallMs / 60_000) + "m ago"
      :                              "quiet · last call " + Math.floor(latestCallMs / 3_600_000) + "h " + Math.floor((latestCallMs % 3_600_000) / 60_000) + "m ago";

    const rightPill = h("span", { className: "pill " + staleClass, style: { height: 18 } },
      h("span", { className: "dot " + staleClass + (latestCallMs != null && latestCallMs < 60_000 ? " pulse" : "") }), " ",
      isEmpty ? "NO CALLS YET" : (summary.total_calls || 0) + " · 24H");

    return h(F, null,
      h("div", { id: "llm-calls-card" },
        h(Card, {
          num: "21", title: "LLM activity · last 24h",
          sub: isEmpty
            ? "No calls written yet — tracker hasn't fired or log file missing"
            : (staleLabel ? staleLabel + " · " : "")
              + "feed · " + (summary.total_calls || 0) + " calls · "
              + fmtTokensCount(summary.total_tokens) + " tokens"
              + (logSize ? " · " + Math.round(logSize / 1024) + " KB on disk" : ""),
          right: cardRight(slot.fetchedAt, rightPill),
        },
          // ── HEADLINE NUMBERS ─────────────────────────────────────
          h("div", {
            style: {
              display: "flex", flexWrap: "wrap", gap: "var(--s-5)",
              alignItems: "baseline", paddingBottom: "var(--s-3)",
              borderBottom: "1px solid var(--line-1)",
            }
          },
            stat("Calls", summary.total_calls || 0),
            stat("Tokens", fmtTokensCount(summary.total_tokens)),
            stat("Avg lat", (summary.avg_latency_s || 0).toFixed(2) + "s",
              fmtLatencyClass(summary.avg_latency_s)),
            stat("P95 lat", (summary.p95_latency_s || 0).toFixed(2) + "s",
              fmtLatencyClass(summary.p95_latency_s)),
            stat("Ollama", ollamaPct.toFixed(0) + "%", ollamaCls),
            stat("Success", (summary.success_pct || 100).toFixed(1) + "%", successCls)
          ),

          h("div", { className: "v3-llm-tier-pills" },
            ["all", "fast", "deep", "ollama", "anthropic"].map((pid) => {
              const labels = {
                all: "all",
                fast: "fast " + fastCount,
                deep: "deep " + deepCount,
                ollama: "ollama " + ollamaCalls,
                anthropic: "anthropic " + anthropicCalls,
              };
              const active = tierPill === pid;
              return h("button", {
                key: pid,
                type: "button",
                className: "v3-llm-tier-pill" + (active ? " is-active" : ""),
                "aria-pressed": active,
                onClick: () => setTierPill(pid),
              }, labels[pid]);
            })
          ),

          // ── FILTERS ─────────────────────────────────────────────
          h("div", {
            style: {
              display: "flex", gap: "var(--s-2)", alignItems: "center",
              padding: "var(--s-3) 0", flexWrap: "wrap",
            }
          },
            h("label", { className: "dim mono", style: { fontSize: "var(--t-2xs)", letterSpacing: ".08em" } }, "AGENT"),
            h("select", {
              value: agentFilter,
              onChange: (e) => setAgentFilter(e.target.value),
              style: {
                background: "var(--bg-inset)", color: "var(--fg-1)",
                border: "1px solid var(--line-2)", borderRadius: 4,
                padding: "4px 6px", fontFamily: "var(--mono)",
                fontSize: "var(--t-xs)", minWidth: 160,
              }
            },
              h("option", { value: "" }, "all agents (" + agentOptions.length + ")"),
              agentOptions.map(a => h("option", { key: a, value: a }, a))
            ),
            h("label", { className: "dim mono", style: { fontSize: "var(--t-2xs)", letterSpacing: ".08em", marginLeft: "var(--s-3)" } }, "SEARCH"),
            h("input", {
              ref: searchRef,
              type: "text",
              value: search,
              onChange: (e) => setSearch(e.target.value),
              placeholder: "regex over agent/model/tier · ⌘F to focus",
              style: {
                background: "var(--bg-inset)", color: "var(--fg-1)",
                border: "1px solid var(--line-2)", borderRadius: 4,
                padding: "4px 8px", fontFamily: "var(--mono)",
                fontSize: "var(--t-xs)", minWidth: 260, flex: 1, maxWidth: 360,
              }
            }),
            (agentFilter || search || tierPill !== "all") && h("button", {
              type: "button",
              onClick: () => { setAgentFilter(""); setSearch(""); setTierPill("all"); },
              style: {
                background: "transparent", border: "1px solid var(--line-2)",
                color: "var(--fg-2)", padding: "3px 8px", cursor: "pointer",
                fontFamily: "var(--mono)", fontSize: "var(--t-2xs)", borderRadius: 4,
              }
            }, "clear"),
            h("span", { className: "tb-spacer", style: { flex: 1 } }),
            h("span", { className: "dim mono v3-num", style: { fontSize: "var(--t-2xs)" } },
              filtered.length + " / " + callsAll.length + " rows")
          ),

          // ── TABLE ───────────────────────────────────────────────
          isEmpty
            ? h("div", {
                className: "dim",
                style: {
                  fontSize: "var(--t-xs)", padding: "var(--s-4) 0",
                  textAlign: "center",
                },
              },
                "No LLM calls captured yet. The tracker writes ",
                h("code", { className: "mono" }, "stocks/memory/llm-calls.jsonl"),
                " on every chat_json call — first record appears within minutes of the next agent run."
              )
            : h(F, null,
              h("div", {
                className: "v3-llm-table-head",
                style: {
                  display: "grid",
                  gridTemplateColumns: "64px 44px 1fr 118px 52px 76px 40px 18px",
                  gap: "var(--s-2)", alignItems: "center",
                  fontSize: "var(--t-2xs)", fontFamily: "var(--mono)",
                  color: "var(--fg-3)", textTransform: "uppercase",
                  letterSpacing: ".08em", padding: "6px 6px",
                  borderBottom: "var(--v3-hairline-strong)",
                  background: "var(--bg-inset)",
                }
              },
                h("span", null, "time"),
                h("span", null, "role"),
                h("span", null, "agent"),
                h("span", null, "model · tier"),
                h("span", { style: { textAlign: "right" } }, "lat"),
                h("span", { style: { textAlign: "right" } }, "tok"),
                h("span", null, "grade"),
                h("span", null, "")
              ),
              filtered.slice(0, shown).map((c, i) => {
                const lat = Number(c.latency_seconds || 0);
                const latCls = fmtLatencyClass(lat);
                const pTok = c.prompt_tokens || 0;
                const cTok = c.completion_tokens || 0;
                const isOpen = selectedTs === c.timestamp;
                const failed = c.success === false || (lat === 0 && cTok === 0);
                const statusCls = failed ? "down" : "up";
                const tag = llmActivityRowRoleTag(c.agent);
                const agentLower = String(c.agent || "").toLowerCase();
                const gradeFromRow = parseGradeFromGistText(c.response_text || "");
                const gradeFromArb = agentLower.indexOf("trade_reviewer") >= 0
                  ? parseGradeFromGistText(arbGist)
                  : null;
                const gradeLetter = gradeFromRow || gradeFromArb;
                const gradeChipCls = "v3-llm-grade" + (gradeLetter
                  ? " v3-llm-grade--" + String(gradeLetter).toLowerCase()
                  : " v3-llm-grade--na");
                return h("div", {
                  key: c.timestamp + "_" + i,
                  className: "v3-llm-row" + (i % 2 === 1 ? " is-alt" : ""),
                  "data-llm-ts": c.timestamp,
                  "data-llm-agent": c.agent || "",
                  onClick: () => openCall(c),
                  tabIndex: 0,
                  role: "button",
                  "aria-label": "Open call " + c.agent + " " + c.timestamp,
                  onKeyDown: (e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault(); openCall(c);
                    }
                  },
                  style: {
                    display: "grid",
                    gridTemplateColumns: "64px 44px 1fr 118px 52px 76px 40px 18px",
                    gap: "var(--s-2)", alignItems: "center",
                    fontSize: "var(--t-xs)", fontFamily: "var(--mono)",
                    padding: "6px 6px",
                    borderBottom: "1px solid var(--line-1)",
                    cursor: "pointer",
                    background: isOpen ? "var(--bg-inset)" : "transparent",
                  }
                },
                  h("span", { className: "dim v3-num" }, fmtHHMMSS(c.timestamp)),
                  h("span", { className: cls("v3-llm-role-tag", tag.cls), title: tag.label }, tag.label.slice(0, 1)),
                  h("span", { style: { color: "var(--fg-1)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } },
                    c.agent || "—"),
                  h("span", { className: "dim", style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } },
                    (c.model || "—") + " · " + (c.tier || "?")),
                  h("span", { className: cls(latCls, "v3-num"), style: { textAlign: "right" } },
                    lat.toFixed(2) + "s"),
                  h("span", { className: "dim v3-num", style: { textAlign: "right" } },
                    pTok + "/" + cTok),
                  h("span", { className: gradeChipCls, "aria-label": "grade" }, gradeLetter || "—"),
                  h("span", { className: "dot " + statusCls, style: { justifySelf: "end" } })
                );
              }),
              filtered.length > shown && h("div", { style: { padding: "var(--s-3) 0", textAlign: "center" } },
                h("button", {
                  type: "button",
                  onClick: () => setShown(n => n + 50),
                  style: {
                    background: "transparent", border: "1px solid var(--line-2)",
                    color: "var(--fg-2)", padding: "5px 14px", cursor: "pointer",
                    fontFamily: "var(--mono)", fontSize: "var(--t-xs)", borderRadius: 4,
                  }
                }, "load more (" + (filtered.length - shown) + " more)")
              ),
              h("div", { className: "dim mono", style: { fontSize: "var(--t-2xs)", padding: "6px 6px 0" } },
                "click any row · Esc closes modal · ⌘F focuses search"
              )
            )
        )
      ),
      modalRec && h(LLMCallModal, { call: modalRec, onClose: closeModal }),
      modalRec && modalLoading && h("div", {
        style: {
          position: "fixed", top: 12, right: 12,
          background: "var(--bg-card)", color: "var(--fg-1)",
          border: "1px solid var(--line-2)", padding: "4px 10px",
          fontFamily: "var(--mono)", fontSize: "var(--t-2xs)",
          borderRadius: 4, zIndex: 110,
        }
      }, "loading full text…"),
      modalRec && modalError && h("div", {
        style: {
          position: "fixed", top: 12, right: 12,
          background: "var(--bg-card)", color: "var(--warn)",
          border: "1px solid var(--line-2)", padding: "4px 10px",
          fontFamily: "var(--mono)", fontSize: "var(--t-2xs)",
          borderRadius: 4, zIndex: 110,
        }
      }, "full text unavailable: " + modalError)
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

    function fmtDecisionTft(d) {
      const tft = d.tft_probs;
      if (tft == null) return "—";
      if (typeof tft === "object") {
        try { return JSON.stringify(tft).slice(0, 72); } catch (_e) { return "—"; }
      }
      return String(tft).slice(0, 72);
    }
    function fmtDecisionGates(d) {
      if (d.kind === "blocked") {
        return "blocked · " + String(d.constraint || "—");
      }
      return "passed · journal";
    }

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
        : h("div", { className: "v3-decision-lane" },
            decisions.map((d, i) => {
              const isBlocked = d.kind === "blocked";
              const verdictCls = isBlocked ? "warn" : "up";
              const verdict = isBlocked ? "NO ENTRY" : ("ENTRY · " + (d.side || "long"));
              const reason = isBlocked
                ? (d.reason || "—") + " (constraint=" + (d.constraint || "—") + ")"
                : (d.reasoning || ((d.regime || "—") + " · conf " + (d.confidence != null ? Number(d.confidence).toFixed(2) : "—")));
              const ts = (d.ts || "").replace("T", " ").slice(0, 19);
              return h("div", { key: i, className: "v3-decision-node" },
                h("div", { className: "v3-decision-rail", "aria-hidden": "true" }),
                h("div", { className: "v3-decision-card" },
                  h("div", { className: "v3-decision-card-head" },
                    h("span", { className: "mono dim v3-decision-ts" }, ts || "—"),
                    h("span", { className: "tb-spacer", style: { flex: 1 } }),
                    h("span", { className: "pill " + verdictCls + " v3-decision-verdict" }, verdict)
                  ),
                  h("div", { className: "v3-decision-swim-meta" },
                    h("span", { className: "v3-decision-chip mono" },
                      "REGIME ", h("span", { className: "v3-num" }, String(d.regime || "—"))),
                    h("span", { className: "v3-decision-chip mono" }, "TFT ", fmtDecisionTft(d)),
                    h("span", { className: "v3-decision-chip mono" }, fmtDecisionGates(d))
                  ),
                  h("div", { className: "dim v3-decision-reason", style: { fontSize: "var(--t-xs)", lineHeight: 1.45 } }, reason)
                )
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

    // Tier C P1-1: feed Topbar from useOpsData state to eliminate duplicate
    // polling. Previously Topbar polled mode/services/combined_portfolio
    // every 30s on top of useOpsData's 10s. With these three envelopes as
    // props, Topbar's local fetch path is skipped — only /api/ops/uptime
    // (the one endpoint NOT in FAST_ENDPOINTS) is still polled by Topbar.

    // Move #9 · Regime-aware page chrome.
    // Tints the topbar's 2px bottom border with the current BTC regime so
    // the operator gets ambient peripheral-vision regime tracking. Reads
    // from the existing data.regime envelope (already polled every 10s by
    // useOpsData) — ZERO new endpoint calls. Writes a CSS custom property
    // (--regime-tint) on the .topbar element; the CSS rule in quanta.css
    // picks it up. Default falls back to --line-2 so unknown / null /
    // unmounted-yet keeps the original 2px hairline.
    const regimeCurrent = (function () {
      const env = envelopeData(data && data.regime);
      return env && env.current ? String(env.current) : null;
    })();
    useEffect(() => {
      const tintFor = (r) => {
        switch (r) {
          case "trending_up":     return "var(--up)";
          case "trending_down":   return "var(--down)";
          case "mean_reverting":  return "var(--warn)";
          case "high_volatility": return "var(--accent)";
          default:                return "var(--line-2)";
        }
      };
      const tb = document.querySelector(".topbar");
      if (!tb) return;
      tb.style.setProperty("--regime-tint", tintFor(regimeCurrent));
      tb.setAttribute("data-regime", regimeCurrent || "unknown");
    }, [regimeCurrent]);

    const scoreSnap = useMemo(() => computeScoreboardMetrics(data), [data]);
    const forceKillExpand = useMemo(() => {
      const halt = (scoreSnap.haltFrac || 0.03) * 100;
      return Math.abs(scoreSnap.liveDayPct) >= halt - 1e-9;
    }, [scoreSnap]);

    const heartbeatStatus = useMemo(() => deriveHeartbeatStatus({
      services: data.services,
      circuitBreakers: data.circuit_breakers,
      mode: data.mode,
      weeklyTraining: data.weekly_training,
      ollamaHealth: data.ollama_health,
      killState: killState,
    }), [data.services, data.circuit_breakers, data.mode, data.weekly_training, data.ollama_health, killState]);

    const scrollToServiceHealth = useCallback(() => {
      const heads = document.querySelectorAll(".card-head h3");
      for (let i = 0; i < heads.length; i++) {
        const t = heads[i].textContent || "";
        if (t.indexOf("Service health") >= 0) {
          const sec = heads[i].closest("section.card");
          if (sec && sec.scrollIntoView) sec.scrollIntoView({ behavior: "smooth", block: "center" });
          return;
        }
      }
    }, []);

    const kbPause = useCallback(() => fetch("/api/ops/pause", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: "operator kill bar pause via spa" }),
    }).then(r => (r.ok ? "PAUSED" : "PAUSE HTTP " + r.status)), []);

    const kbFlatten = useCallback(() => fetch("/api/ops/pause", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: "operator kill bar flatten+halt via spa" }),
    }).then(r => (r.ok ? "HALT issued — close positions manually if needed" : "HTTP " + r.status)), []);

    return h(F, null,
      h(CommandPalette, { variant: "ops", opsData: data, killState, setKillState }),
      h("div", { className: "app" },
        h(Topbar, {
          killState, setKillState, active: "ops",
          combinedPortfolio: data.combined_portfolio,
          mode: data.mode,
          services: data.services,
          heartbeatStatus: heartbeatStatus,
          onHeartbeatClick: scrollToServiceHealth,
        }),
        h(Sidebar, { active: "ops" }),
        h("main", { className: "main" },
          h("div", { className: "page-title" },
            h("h1", null, "Operations console"),
            h("span", { className: "breadcrumb" }, "/ ops_spa"),
            h("span", { className: "tb-spacer", style: { flex: 1 } }),
            h("span", { className: "mono dim", style: { fontSize: "var(--t-xs)" } }, "scroll · sections snap to view")
          ),
          // Move #7 · BLOCKER BANNER — global "why isn't anything trading?"
          // summary. Only renders when blockers exist; zero footprint at rest.
          // Reads from the existing data.gates slot — no new endpoint.
          h(BlockerBanner, { data }),
          // TODAY SCOREBOARD — operator's at-a-glance: capital, day P&L,
          // trades done, open positions, drawdown. Mounted ABOVE the hero
          // so it's the first thing the eye lands on (top of the page).
          h(TodayScoreboard, { data }),
          // SHARK OVERRIDE HEALTH — verifier card for the BEAR_VOLATILE
          // paper-mode override. Sits directly under the scoreboard so a
          // single glance tells the operator "override is healthy" or
          // "override has not fired in N runs — investigate."
          h(SharkOverrideHealthLive, { data }),
          // WEEKLY TRAINING — ModelForge LoRA adapter pipeline status.
          // Surfaces the 6 trading-bot LLM-role tracks (Reflector, Bull,
          // Bear, Arbiter, RegimeTagger, IndicatorSelector) with their
          // current champion adapter + last train timestamp + headline
          // eval score. Sits next to SharkOverrideHealthLive — these are
          // the two "what's the AI doing right now" health cards.
          // Degrade-soft: still renders with local-only metrics when
          // model-forge is offline.
          h(WeeklyTrainingLive, { data }),
          // 2026-05-13 post-V4-cutover: TrainingHealthLive (per-pair FreqAI
          // model.zip validation) and TrainingCardLive ("Training · FreqAI /
          // TFT retrain status") are dead surfaces. V4 has no FreqAI/TFT
          // retrain loop; both cards rendered "12/12 HEALTHY" + "IDLE"
          // perpetually, confusing the operator. Removed from layout (the
          // component definitions remain in this file as dead code pending
          // a separate cleanup pass).
          // HERO
          h(HeroLive, { data, killState }),
          // STOCKS-ONLY TRAINING ROW — crypto-side FreqAI training is dead
          // post-cutover; only the stocks Shark TFT card remains useful.
          h("div", { id: "training", className: "grid g-12 anchor", style: { gap: "var(--gap-grid)" } },
            h("div", { style: { gridColumn: "span 12" } }, h(StocksMLLive, { data }))
          ),
          // LLM ACTIVITY — live feed of stocks/memory/llm-calls.jsonl with
          // drill-down modal. Mounted below the training row per spec so
          // the top stays uncrowded — operator clicks a row to see the full
          // prompt + response (provided SHARK_LLM_LOG_FULL_TEXT=1 was on
          // when the call was logged).
          //
          // AgentFlow strip sits ABOVE the activity list — same data, but
          // a per-role pipeline view (regime_tagger → … → reflector). Both
          // consume the same /api/ops/llm_calls response; no extra poll.
          h("div", { id: "llm-calls", className: "anchor", style: { gridColumn: "span 12" } },
            h(AgentFlow, { data }),
            h(LLMCallsLive, { data }),
            // Tier E: AgentLogsDrawer renders via React portal to
            // document.body, so its position in the tree is irrelevant
            // for layout. Mounted here so its lifecycle is tied to the
            // same LLM-activity section.
            h(AgentLogsDrawer)
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
          h("div", { id: "pair-telemetry-crypto", className: "anchor" }, h(PairTelemetryLive, { data })),
          h("div", { id: "pair-telemetry-stocks", className: "anchor" }, h(StocksPairTelemetryLive, { data })),
          // SERVICES + POSITIONS
          h("div", { className: "grid g-12", style: { gap: "var(--gap-grid)" } },
            h("div", { id: "service-health", className: "anchor", style: { gridColumn: "span 4" } }, h(ServicesLive, { data, killState })),
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
            h("div", { id: "quick-actions", className: "anchor", style: { gridColumn: "span 6" } }, h(QuickActions, { killState, setKillState }))
          ),
          // BACKTEST QUALITY GATES — full-width under breakers/quick-actions row.
          // Sits in the risk-gates section per stage/18 spec; clicking a row
          // expands to show numeric values vs thresholds.
          h("div", { id: "backtest-gates", className: "anchor", style: { gridColumn: "span 12" } },
            h(BacktestGatesLive, { data })
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
      ),
      h(KillBar, {
        killState: killState,
        setKillState: setKillState,
        forceOpen: forceKillExpand,
        onPause: kbPause,
        onFlatten: kbFlatten,
        onKill: () => { setKillState("killed"); },
        onResume: () => {
          setKillState("normal");
          return Promise.resolve("RESUME sent");
        },
        resumeDisabled: killState !== "killed",
      })
    );
  }

  // Mount
  const root = ReactDOM.createRoot(document.getElementById("root"));
  root.render(h(OpsApp));
})();
