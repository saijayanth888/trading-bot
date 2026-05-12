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

  // ─────────────── TODAY SCOREBOARD — single-card at-a-glance summary ─────
  // Operator's stated need (2026-05-11): "top right corner, daily P&L,
  // capital, trades done, all the things". This card distills 6 numbers
  // that answer "where are we right now" without scrolling.
  function TodayScoreboard({ data }) {
    const cpSlot = slotState(data, "combined_portfolio");
    const cp = envelopeData(cpSlot.env) || {};
    const tr = envelopeData(data.trades_risk) || {};
    const stocks = envelopeData(data.stocks) || {};
    const wheelOpen = ((stocks.wheel || {}).open_positions || []).length;

    const equity = Number(cp.total_equity ?? 0);
    const peak = Number(cp.combined_peak_equity ?? equity);
    const dd = Math.abs(Number(cp.combined_drawdown_pct ?? 0));
    // Closed-trade day P&L (from trade_journal, server-side at ops_routes:2549).
    // This is the "realized" component — only moves when a trade closes.
    const closedPnl = Number(cp.day_pnl_usd ?? 0);
    // Live unrealized P&L on open positions — this is what makes the number
    // TICK with market moves. Operator complaint (2026-05-11 ~3 PM): "I don't
    // see the drop in -23.37, that is not getting changed" because the page
    // was showing closed-only. Sum crypto-unrealized (sources.crypto_unrealised_pnl
    // — freqtrade hot-quotes) and stocks day-move (stocks_equity − stocks_peak_equity,
    // captures wheel MTM since the wheel_snapshot cron now fires every minute).
    const srcs = cp.sources || {};
    const cryptoUnrl = Number(srcs.crypto_unrealised_pnl ?? 0);
    const stocksEq = Number(cp.stocks_equity ?? 0);
    const stocksPeak = Number(cp.stocks_peak_equity ?? stocksEq);
    const stocksMove = stocksEq - stocksPeak;
    const liveDayPnl = closedPnl + cryptoUnrl + stocksMove;
    // Percent against starting combined capital — use peak as a sane proxy
    // when peak ≈ start (early in the campaign). Operator-readable %.
    const baseCap = peak > 0 ? peak : equity;
    const liveDayPct = baseCap > 0 ? (liveDayPnl / baseCap) * 100 : 0;
    const closedToday = Number(tr.closed_today ?? 0);
    const openCrypto = Number(tr.open_count ?? 0);
    const totalOpen = openCrypto + wheelOpen;
    const dayCls = liveDayPnl >= 0 ? "up" : "down";
    const ddCls = dd >= 8 ? "down" : dd >= 5 ? "warn" : "up";

    const stat = (lbl, val, cls) => h("div", { style: { display: "flex", flexDirection: "column", gap: 2, minWidth: 110 } },
      h("div", { className: "dim2 mono", style: { fontSize: "var(--t-2xs)", letterSpacing: ".08em", textTransform: "uppercase" } }, lbl),
      h("div", { className: "num " + (cls || ""), style: { fontSize: "var(--t-lg)", fontFamily: "var(--mono)", fontWeight: 500, fontVariantNumeric: "tabular-nums" } }, val)
    );

    return h(Card, {
      num: "00", title: "Today · scoreboard",
      sub: "live · realized + unrealized · refreshes every 10s",
      right: cardRight(cpSlot.fetchedAt,
        h("span", { className: "pill " + dayCls, style: { height: 18 } },
          h("span", { className: "dot " + dayCls + (liveDayPnl === 0 ? "" : " pulse") }),
          " ", (liveDayPct >= 0 ? "+" : "") + liveDayPct.toFixed(2) + "% live"))
    },
      h("div", { style: { display: "flex", flexWrap: "wrap", gap: "var(--s-5)", alignItems: "baseline" } },
        stat("Capital", "$" + fmtUSD(equity)),
        stat("Live P&L", (liveDayPnl >= 0 ? "+$" : "−$") + fmtUSD(Math.abs(liveDayPnl)), dayCls),
        stat("Realized today", (closedPnl >= 0 ? "+$" : "−$") + fmtUSD(Math.abs(closedPnl)),
          closedPnl >= 0 ? "up" : "down"),
        stat("Unrealized", (cryptoUnrl + stocksMove >= 0 ? "+$" : "−$") + fmtUSD(Math.abs(cryptoUnrl + stocksMove)),
          (cryptoUnrl + stocksMove) >= 0 ? "up" : "down"),
        stat("Drawdown", dd.toFixed(2) + "%", ddCls),
        stat("Peak", "$" + fmtUSD(peak)),
        stat("Open", totalOpen + " (" + openCrypto + "C + " + wheelOpen + "S)"),
        stat("Closed today", closedToday)
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
    let cls, dot, label;
    if (stalled >= 3 || status === "stalled") {
      cls = "down"; dot = "down"; label = "STALLED";
    } else if (stalled >= 1 || status === "degraded") {
      cls = "warn"; dot = "warn"; label = "DEGRADED";
    } else if (status === "unknown") {
      cls = "info"; dot = "info"; label = "UNKNOWN";
    } else {
      cls = "up"; dot = "up"; label = "HEALTHY";
    }

    const stat = (lbl, val, valCls) => h("div", { style: { display: "flex", flexDirection: "column", gap: 2, minWidth: 100 } },
      h("div", { className: "dim2 mono", style: { fontSize: "var(--t-2xs)", letterSpacing: ".08em", textTransform: "uppercase" } }, lbl),
      h("div", { className: "num " + (valCls || ""), style: { fontSize: "var(--t-base)", fontFamily: "var(--mono)", fontWeight: 500, fontVariantNumeric: "tabular-nums" } }, val)
    );

    const reason = env.reason || "—";
    const checkedAt = env.checked_at;

    return h(Card, {
      num: "00b", title: "Shark · BEAR_VOLATILE override health",
      sub: "verifier · cron 09:45 ET · " + (regime || "—"),
      right: cardRight(slot.fetchedAt,
        h("span", { className: "pill " + cls, style: { height: 18 } },
          h("span", { className: "dot " + dot + (label === "HEALTHY" ? "" : " pulse") }),
          " ", label))
    },
      h("div", { style: { display: "flex", flexWrap: "wrap", gap: "var(--s-5)", alignItems: "baseline" } },
        stat("Regime", regime, regime.indexOf("BEAR") >= 0 ? "warn" : "up"),
        stat("Override expected", overrideExpected ? "yes" : "no"),
        stat("Override applied", overrideApplied ? "yes" : "no",
          overrideApplied ? "up" : (overrideExpected ? "warn" : "")),
        stat("Candidates", evald),
        stat("Passed override", passed, passed > 0 ? "up" : ""),
        stat("Trades placed", trades, trades > 0 ? "up" : ""),
        stat("Stalled runs", stalled, stalled >= 3 ? "down" : (stalled >= 1 ? "warn" : "up"))
      ),
      h("div", {
        className: "dim mono",
        style: { fontSize: "var(--t-2xs)", marginTop: "var(--s-3)", lineHeight: 1.4 }
      },
        reason,
        lastTrade ? h("span", null, " · last trade: " + lastTrade) : null,
        checkedAt ? h("span", null, " · checked " + checkedAt) : null
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
    // one track has a champion; orange when MF is offline; gray when MF
    // is reachable but pipeline is still starting up.
    let pillCls, pillText;
    if (!mfReachable) {
      pillCls = "warn"; pillText = "MODEL-FORGE OFFLINE";
    } else if ((summary.n_tracks_trained || 0) === 0) {
      pillCls = "info"; pillText = "STARTING UP";
    } else if ((summary.n_promoted_this_week || 0) > 0) {
      pillCls = "up"; pillText = (summary.n_promoted_this_week) + " PROMOTED THIS WEEK";
    } else {
      pillCls = "info"; pillText = (summary.n_tracks_trained || 0) + "/6 TRAINED";
    }

    return h(Card, {
      num: "00c", title: "Weekly training · LoRA adapters",
      sub: mfReachable
        ? ("model-forge @ " + (env.model_forge_url || "—")
           + " · Sun 02:00 ET refresh")
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
    // Countdown to next Sunday 02:00 ET (recomputed on each render via the
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
           "Sunday 02:00 ET")
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
    const ftStatus = (services.freqtrade && services.freqtrade.up) ? "freqtrade · ok"
                   : (services.freqtrade ? "freqtrade · down" : "—");
    return h("div", { className: "card mountin", style: { padding: "var(--s-3) var(--s-4)" } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "var(--s-2)" } },
        h("span", { className: "metric-label" }, "BOT STATE"),
        h("span", { className: "tb-spacer", style: { flex: 1 } }),
        h("span", { className: "pill " + klass }, h("span", { className: "dot " + klass + " pulse" }), " ", lbl)
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
        h("span", { className: "pill accent" }, h("span", { className: "dot accent pulse" }), " ", items.length, " EVENTS · 24h"))
    },
      h("div", { style: { display: "flex", flexDirection: "column", maxHeight: 420, overflowY: "auto" } },
        items.length === 0
          ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)", padding: "var(--s-3) 0" } }, "no recent activity")
          : items.map((r, i) => {
              const dot = r.level === "warn" ? "warn"
                        : r.level === "down" ? "down"
                        : r.level === "up" ? "up"
                        : "accent";
              const srcVar = "var(--" + dot + ")";
              // Stable identity prevents React from reusing DOM nodes when
              // a new event unshifts at the top — expanded state previously
              // pointed at index N which mapped to a different event after
              // the refresh tick.
              const stableKey = `${r.ts}:${r.src}:${(r.title || '').slice(0, 32)}`;
              const open = expanded === stableKey;
              return h("div", {
                key: stableKey,
                style: {
                  display: "grid", gridTemplateColumns: "60px 12px 1fr", gap: "var(--s-3)",
                  padding: "var(--s-3) 0", borderBottom: "1px solid var(--line-1)", cursor: "pointer",
                },
                onClick: () => setExpanded(open ? null : stableKey)
              },
                h("div", { className: "mono dim", style: { fontSize: "var(--t-xs)", paddingTop: 2 } },
                  h(TimeSince, { ts: r.ts })),
                h("div", { style: { position: "relative", paddingTop: 6 } },
                  h("span", { className: "dot " + dot, style: { position: "relative", zIndex: 1 } }),
                  h("div", { style: { position: "absolute", left: 2, top: 12, bottom: -16, width: 1, background: "var(--line-2)" } })
                ),
                h("div", null,
                  h("div", { style: { display: "flex", alignItems: "baseline", gap: "var(--s-2)" } },
                    h("span", { className: "tl-source", style: { color: srcVar } }, r.src),
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
  function GateDot({ state, label, detail }) {
    // tiny inline dot used in EntryGatesLive's per-pair gate-strip. hover
    // title surfaces gate name + detail so operator gets per-gate context
    // without expanding the row.
    const color = state === true ? "var(--c-up)"
      : state === false ? "var(--c-down)"
      : "color-mix(in srgb, var(--fg-3) 60%, transparent)";
    return h("span", {
      title: label + " — " + (state === true ? "PASS" : state === false ? "BLOCK" : "n/a") + (detail ? " · " + detail : ""),
      style: { width: 9, height: 9, borderRadius: "50%", background: color, display: "inline-block", flexShrink: 0 },
    });
  }

  function EntryGatesLive({ data }) {
    const [expand, setExpand] = useState(null);
    const slot = slotState(data, "gates");
    const env = envelopeData(slot.env) || {};
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

    // Aggregate which gate is the most common blocker (operator wants
    // "what's keeping everything offline" at a glance).
    const blockerCounts = {};
    all.forEach(p => p.gates.filter(g => g.pass === false).forEach(g => {
      blockerCounts[g.gate] = (blockerCounts[g.gate] || 0) + 1;
    }));
    const topBlockers = Object.entries(blockerCounts).sort((a, b) => b[1] - a[1]).slice(0, 2);

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
      sub: passing + "/" + all.length + " pair" + (all.length === 1 ? "" : "s") + " eligible",
      right: cardRight(slot.fetchedAt,
        h("span", { className: "pill " + (blocked > 0 ? "down" : "up"), style: { height: 18 } },
          h("span", { className: "dot " + (blocked > 0 ? "down pulse" : "up") }), " ",
          blocked > 0 ? (blocked + " BLOCKED") : "ALL CLEAR"))
    },
      // ── aggregate banner: tells operator "why is everything off" in one line ──
      blocked > 0 && topBlockers.length > 0 && h("div", {
        style: { fontSize: "var(--t-xs)", padding: "var(--s-2) var(--s-3)",
          marginBottom: "var(--s-2)", borderLeft: "2px solid var(--c-down)",
          background: "color-mix(in srgb, var(--c-down) 6%, transparent)" }
      },
        h("span", { style: { color: "var(--fg-1)" } }, blocked + " of " + all.length + " pairs blocked"),
        h("span", { className: "dim", style: { marginLeft: 8 } }, "most common: "),
        topBlockers.map(([g, n], i) => h("span", { key: g, className: "mono", style: { marginLeft: 6 } },
          (i > 0 ? "· " : "") + g + " (" + n + "×)"))
      ),

      // ── per-pair rows: pair · regime · gate-strip dots · n/M · first blocker · ▸ ──
      all.length === 0
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)", padding: "var(--s-3)" } },
            "no gate data — endpoint returned empty")
        : h("div", { style: { display: "flex", flexDirection: "column", gap: 0 } },
            all.map((p, i) => h(F, { key: p.sym }, [
              h("div", {
                key: "row",
                onClick: () => setExpand(expand === i ? null : i),
                style: { cursor: "pointer", display: "grid",
                  gridTemplateColumns: "minmax(80px,1fr) minmax(110px,1fr) minmax(120px,2fr) minmax(60px,80px) minmax(120px,1fr) 18px",
                  gap: "var(--s-2)", alignItems: "center",
                  padding: "var(--s-2) var(--s-2)",
                  borderBottom: "1px solid var(--line-1)",
                  fontSize: "var(--t-xs)" }
              },
                h("strong", { style: { color: "var(--fg-1)" } }, p.sym),
                h("span", { className: "pill " + (p.regime === "trending_up" ? "up" : p.regime === "trending_down" ? "down" : "info"),
                  style: { height: 18, justifySelf: "start" } }, p.regime || "—"),
                h("span", { style: { display: "inline-flex", gap: 4, alignItems: "center", flexWrap: "wrap" } },
                  p.gates.map((g, gi) => h(GateDot, { key: gi, state: g.pass, label: g.gate, detail: g.detail }))),
                h("span", { className: "mono dim", style: { fontSize: "var(--t-2xs)" } },
                  (p.gates.length - p.blocking) + "/" + p.gates.length + " pass"),
                h("span", { className: p.first_blocker ? "mono" : "dim", style: { fontSize: "var(--t-xs)", color: p.first_blocker ? "var(--c-down)" : undefined } },
                  p.first_blocker || "—"),
                h("span", { className: "dim mono", style: { fontSize: "var(--t-xs)" } }, expand === i ? "▾" : "▸")
              ),
              expand === i && h("div", {
                key: "exp",
                style: { background: "var(--bg-inset)", padding: "var(--s-3) var(--s-4)",
                  borderBottom: "1px solid var(--line-1)" }
              },
                h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--s-2) var(--s-4)" } },
                  p.gates.map((g, gi) => h("div", { key: gi,
                    style: { display: "flex", alignItems: "center", gap: 8, fontSize: "var(--t-xs)" } },
                    h(GateBadge, { state: g.pass === true ? "PASS" : g.pass === false ? "BLOCK" : "NA" }),
                    h("span", { style: { color: "var(--fg-1)", minWidth: 140 } }, g.gate),
                    h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)", flex: 1, textAlign: "right" } }, g.detail)
                  )))
              ),
            ].filter(Boolean)))
          )
    );
  }

  // ─────────────── PAIR TELEMETRY — sparklines live ───────────────
  function PairTelemetryLive({ data }) {
    const slot = slotState(data, "sparklines");
    const env = envelopeData(slot.env) || {};
    const pairs = env.pairs || {};
    const entries = Object.entries(pairs);

    if (slot.phase === "down") {
      return h(Card, {
        num: "06", title: "Pair telemetry · 5m closes · trailing 24h",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt)
      },
        h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }

    return h(Card, {
      num: "06", title: "Pair telemetry · 5m closes · trailing 24h",
      sub: entries.length + " pairs · auto-refresh 10s",
      right: cardRight(slot.fetchedAt)
    },
      slot.phase === "loading"
        ? h(LoadingState)
        : entries.length === 0
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no sparkline data")
        : h("div", { className: "grid g-4", style: { gap: "var(--s-3)" } },
            entries.map(([sym, p]) => {
              const data = p.closes || [];
              const pct = Number(p.pct_24h || 0);
              const px = Number(p.current || 0);
              const href = "/?pair=" + encodeURIComponent(sym) + "&venue=crypto";
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

  // ─────────────── STOCKS PAIR TELEMETRY — sparklines live ───────────────
  // Stocks-side parity to PairTelemetryLive. Reads from /api/ops/stocks_sparklines
  // (5Min × 78 bars ≈ one US trading session by default). NYSE-closed window
  // dims the card and swaps the sub-line to "last session close".
  function StocksPairTelemetryLive({ data }) {
    const slot = slotState(data, "stocks_sparklines");
    const env = envelopeData(slot.env) || {};
    const symbols = env.symbols || {};
    const basket = Array.isArray(env.basket) ? env.basket : Object.keys(symbols);
    const marketOpen = env.market_open;
    const tfLabel = env.timeframe || "5Min";

    if (slot.phase === "down") {
      return h(Card, {
        num: "23", title: "Stocks pair telemetry · " + tfLabel + " · session-to-date",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt),
      },
        h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }
    if (slot.phase === "loading") {
      return h(Card, {
        num: "23", title: "Stocks pair telemetry · " + tfLabel + " · session-to-date",
        sub: "loading…",
        right: cardRight(slot.fetchedAt),
      }, h(LoadingState));
    }

    const subLine = marketOpen
      ? basket.length + " symbols · NYSE open · auto-refresh 10s"
      : basket.length + " symbols · NYSE closed · last session close";

    const wrapperStyle = marketOpen
      ? null
      : { opacity: 0.78 };  // visually dim when market closed, per spec

    return h(Card, {
      num: "23", title: "Stocks pair telemetry · " + tfLabel + " · session window",
      sub: subLine,
      right: cardRight(slot.fetchedAt),
    },
      h("div", { style: wrapperStyle },
        basket.length === 0
          ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no stock symbols configured")
          : h("div", { className: "grid g-4", style: { gap: "var(--s-3)" } },
              basket.map(sym => {
                const p = symbols[sym] || {};
                const closes = p.closes || [];
                const pct = (p.pct_session == null) ? null : Number(p.pct_session);
                const px = (p.current == null) ? null : Number(p.current);
                const err = p.error;
                const cellStyle = { padding: "var(--s-3)", textDecoration: "none", color: "inherit" };
                const sparkColor = pct == null ? "--fg-3" : (pct >= 0 ? "--up" : "--down");
                const pctCls = pct == null ? "" : (pct >= 0 ? "up" : "down");

                return h("div", {
                  key: sym,
                  className: "card",
                  style: cellStyle,
                  "data-test": "stocks-spark-" + sym,
                },
                  h("div", { style: { display: "flex", alignItems: "baseline", gap: 8 } },
                    h("strong", { className: "mono" }, sym),
                    pct == null
                      ? h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, "—")
                      : h("span", { className: "pill " + pctCls, style: { height: 16, fontSize: "var(--t-2xs)" } }, fmtPct(pct)),
                    h("span", { className: "tb-spacer", style: { flex: 1 } })
                  ),
                  h("div", { style: { marginTop: 6 } },
                    closes.length >= 2
                      ? h(Sparkline, { data: closes, color: sparkColor, height: 32 })
                      : h("div", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } },
                          err ? err : "no closes")
                  ),
                  h("div", { style: { display: "flex", justifyContent: "space-between", marginTop: 4 } },
                    h("span", { className: "num", style: { fontSize: "var(--t-sm)" } },
                      px == null ? "—" : "$" + fmtUSD(px)),
                    h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } },
                      (p.bars_count != null ? p.bars_count : closes.length) + " bars")
                  )
                );
              })
            )
      )
    );
  }

  // ─────────────── SERVICES — 8-row health probe ───────────────
  function ServicesLive({ data }) {
    const slot = slotState(data, "services");
    const services = envelopeData(slot.env) || {};
    const rows = Object.entries(services);
    const totalUp = rows.filter(([, info]) => info && info.up).length;

    if (slot.phase !== "ok") {
      return h(Card, {
        num: "07a", title: "Service health · probes",
        sub: slot.phase === "loading" ? "loading…" : "endpoint unavailable",
        right: cardRight(slot.fetchedAt)
      },
        slot.phase === "loading"
          ? h(LoadingState)
          : h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 10 })
      );
    }

    return h(Card, {
      num: "07a", title: "Service health · " + rows.length + " probes",
      sub: totalUp + "/" + rows.length + " up",
      right: cardRight(slot.fetchedAt)
    },
      h("div", { style: { display: "flex", flexDirection: "column" } },
        rows.length === 0
          ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no probes registered")
          : rows.map(([name, info]) => h(StatusRow, {
              key: name,
              status: info && info.up ? "up" : "down",
              name: name,
              sub: info ? ("via " + (info.via || "?") + (info.code != null ? " · " + info.code : "")) : "",
              value: h("span", null,
                info && info.age_s != null ? h("span", { className: "dim", style: { marginRight: 10 } }, Math.round(info.age_s) + "s") : null,
                info && info.endpoint ? h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, info.endpoint) : null
              )
            }))
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
        h("span", { className: "up num", style: { fontSize: "var(--t-lg)" } }, "$" + fmtUSD(saved, 2))
      )
    },
      h("div", { style: { display: "flex", flexDirection: "column" } },
        h(StatusRow, {
          status: oh.healthy ? "up" : "down",
          name: "Ollama (primary)",
          sub: oh.healthy
            ? (ollamaModels.length + " models" + (oh.status_age_seconds != null ? " · probed " + Math.round(oh.status_age_seconds) + "s ago" : ""))
            : (oh.error || "down"),
          value: h("span", null, h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, "lat ", ollamaLatencyMs != null ? ollamaLatencyMs + "ms" : "—"))
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
    const slot = slotState(data, "live_trades");
    const env = envelopeData(slot.env) || {};
    const trades = env.trades || [];

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

    return h(Card, {
      num: "11", title: "MCP · wire status",
      sub: reachable ? "Hermes MCP reachable" : "MCP unreachable",
      right: cardRight(slot.fetchedAt,
        h("span", { className: "pill " + (reachable ? "up" : "down") }, h("span", { className: "dot " + (reachable ? "up" : "down") + " pulse" }), " ", reachable ? "OK" : "DOWN"))
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 2fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "URL"),
        h("div", { className: "num mono", style: { fontSize: "var(--t-2xs)", wordBreak: "break-all" } }, env.endpoint || "—"),
        h("div", { className: "dim mono" }, "TRANSPORT"),
        h("div", { className: "num mono", style: { fontSize: "var(--t-2xs)" } }, env.transport || "—"),
        h("div", { className: "dim mono" }, "PROBE"),
        h("div", { className: "num" },
          (probe.via || "—") + (probe.age_s != null ? " · " + Math.round(probe.age_s) + "s" : "")),
        h("div", { className: "dim mono" }, "TOOLS"),
        h("div", { className: "num" }, env.tools_count != null ? env.tools_count : "—"),
        h("div", { className: "dim mono" }, "LAST CALL"),
        h("div", { className: "num mono", style: { fontSize: "var(--t-2xs)" } },
          lastCall.tool ? (lastCall.tool + (lastCall.ts ? " · " + lastCall.ts.replace("T", " ").slice(0, 19) : "")) : "—")
      )
    );
  }

  // ─────────────── QUICK ACTIONS — fully wired ───────────────
  // Each button shows a status indicator (success/error/info) below the button row.
  function QuickActions({ setKillState, killState }) {
    const [status, setStatus] = useState({ msg: "", level: "info", ts: 0 });
    const toast = (msg, level) => setStatus({ msg, level: level || "info", ts: Date.now() });

    const postJSON = (url, body) => fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });

    const doPause = () => postJSON("/api/ops/pause", { reason: "operator manual pause via spa" })
      .then(r => r.ok ? toast("PAUSED · dry_run=true", "ok") : toast("PAUSE failed · HTTP " + r.status, "warn"))
      .catch(e => toast("PAUSE error · " + e.message, "warn"));

    // RESUME re-enables order placement; it's irreversible-on-fill, so we
    // require an explicit operator confirmation. Pause is one-click by design
    // (always safe to pause), but resume is two-step.
    const doResume = () => {
      if (!window.confirm("Resume trading? This re-enables order placement on the live freqtrade instance.")) {
        toast("RESUME cancelled", "info");
        return;
      }
      return postJSON("/api/ops/resume", { reason: "operator manual resume via spa", confirm: true })
        .then(r => r.ok ? toast("RESUMED · dry_run=false", "ok") : r.json().then(j => toast("RESUME refused · " + (j.detail || ("HTTP " + r.status)), "warn")))
        .catch(e => toast("RESUME error · " + e.message, "warn"));
    };

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
      sub: "atomic config writes · snapshots auto-saved"
    },
      h("div", { className: "grid g-2", style: { gap: "var(--s-3)" } },
        h("button", { className: "btn", onClick: doPause, "aria-label": "Pause trading" }, "PAUSE TRADING"),
        h("button", { className: "btn", onClick: doResume, "aria-label": "Resume trading" }, "RESUME"),
        h("button", { className: "btn warn", onClick: doEvolve, "aria-label": "Trigger evolution cycle" }, "TRIGGER EVOLUTION"),
        h("button", { className: "btn", onClick: doRebalance, "aria-label": "Rebalance portfolio weights" }, "REBALANCE WEIGHTS"),
        h("button", { className: "btn", onClick: doSlackBrief, "aria-label": "Generate daily Slack brief" }, "DAILY SLACK BRIEF")
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
    return h(Card, {
      num: "17", title: "Training · FreqAI / TFT retrain status",
      sub: cur ? ("training " + cur.pair + " · epoch " + cur.last_epoch + "/" + cur.max_epoch) : (done.length + " pairs trained"),
      right: h(F, null,
        h(TimeSince, { ts: data.training_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        cur
          ? h("span", { className: "pill accent" }, h("span", { className: "dot accent pulse" }), " LIVE")
          : h("span", { className: "pill up" }, "IDLE")
      )
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "CURRENT PAIR"),
        h("div", { className: "num accent" }, (cur && cur.pair) || "—"),
        h("div", { className: "dim mono" }, "EPOCH"),
        h("div", { className: "num" }, cur ? (cur.last_epoch + " / " + cur.max_epoch) : "—"),
        h("div", { className: "dim mono" }, "VAL SHARPE"),
        h("div", { className: "num " + ((cur && cur.val_sharpe >= 0) ? "up" : "down") }, cur && cur.val_sharpe != null ? Number(cur.val_sharpe).toFixed(3) : "—"),
        h("div", { className: "dim mono" }, "LOSS"),
        h("div", { className: "num" }, cur && cur.loss != null ? Number(cur.loss).toFixed(4) : "—"),
        h("div", { className: "dim mono" }, "AVG EPOCH"),
        h("div", { className: "num" }, tft.avg_epoch_seconds != null ? tft.avg_epoch_seconds + "s" : "—"),
        h("div", { className: "dim mono" }, "ETA"),
        h("div", { className: "num" }, etaMin != null ? etaMin + "m" : "—"),
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
            h("span", { className: "num" }, "ep " + (p.last_epoch != null ? p.last_epoch : "—")),
            h("span", { className: "num " + ((p.val_sharpe || 0) >= 0 ? "up" : "down") }, p.val_sharpe != null ? Number(p.val_sharpe).toFixed(2) : "—"),
            h("span", { className: "dim mono" }, p.early_stopped ? "early-stop" : (p.end_ts || p.start_ts || ""))
          ))
        )
      )
    );
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
        : h("table", { className: "t" },
            h("thead", null, h("tr", null,
              h("th", null, "Gate"),
              h("th", { style: { textAlign: "right" } }, "Current"),
              h("th", { style: { textAlign: "right" } }, "Threshold"),
              h("th", null, "Status")
            )),
            h("tbody", null, checks.map((c, i) => h("tr", { key: i },
              h("td", null, labelOf(c.name)),
              h("td", { className: "num " + (c.passed ? "up" : "down"), style: { textAlign: "right" } }, fmtVal(c.name, c.value)),
              h("td", { className: "dim mono", style: { textAlign: "right" } }, fmtTh(c.name, c.threshold, c.op)),
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
  // Operator education block, ported verbatim from legacy ops.html:1204-1278
  // with italic tags (<i>, <em>) and decorative emojis stripped per the
  // operator design spec. Regime-name spans flattened to <code> since the
  // legacy regime-tag-* CSS classes don't live in quanta.css.
  const REGIME_PARAMS_GUIDE_HTML = (
    '<h4 style="font-family:var(--mono);font-size:var(--t-2xs);font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--fg-3);margin:8px 0 6px;">The 5 market regimes</h4>' +
    '<p style="margin:0 0 10px;">A 4-state HMM classifies each candle into one of these regimes per pair. The strategy adapts entries, exits, sizing, and trailing-stop behaviour to the active regime.</p>' +
    '<dl style="display:grid;grid-template-columns:160px 1fr;gap:4px 14px;margin:0 0 6px;">' +
      '<dt><code>trending_up</code></dt>' +
      '<dd>Sustained uptrend. <strong>Strategy:</strong> loosen entries, hold longer, activate trailing stop on winners.</dd>' +
      '<dt><code>trending_down</code></dt>' +
      '<dd>Sustained downtrend. <strong>Strategy:</strong> longs are <strong>hard-blocked</strong> — bot waits for regime change. The <code>entry_delta</code> here is belt-and-suspenders.</dd>' +
      '<dt><code>mean_reverting</code></dt>' +
      '<dd>Range-bound, oscillating market. <strong>Strategy:</strong> quick scalps with tight take-profit (<code>mean_rev_take_profit</code>).</dd>' +
      '<dt><code>high_volatility</code></dt>' +
      '<dd>Whippy, hard-to-predict. <strong>Strategy:</strong> shrink position size (<code>high_vol_stake_factor</code>) and require higher conviction (<code>high_vol_min_confidence</code>).</dd>' +
      '<dt><code>unknown</code></dt>' +
      '<dd>HMM uncertain. <strong>Strategy:</strong> conservative defaults — neither blocked nor preferred.</dd>' +
    '</dl>' +
    '<h4 style="font-family:var(--mono);font-size:var(--t-2xs);font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--fg-3);margin:14px 0 6px;">Entry &amp; exit deltas</h4>' +
    '<p style="margin:0 0 10px;">These add a per-regime offset to the base thresholds. Base entry = <code>0.62</code> (TFT up-probability needed to fire a long); base exit = <code>0.55</code> (down-probability needed to close).</p>' +
    '<dl style="display:grid;grid-template-columns:160px 1fr;gap:4px 14px;margin:0 0 6px;">' +
      '<dt><code>entry_delta = +0.15</code></dt>' +
      '<dd>Require <code>up_prob ≥ 0.62 + 0.15 = 0.77</code>. <strong>Harder to enter</strong> in this regime.</dd>' +
      '<dt><code>entry_delta = −0.05</code></dt>' +
      '<dd>Require <code>up_prob ≥ 0.62 − 0.05 = 0.57</code>. <strong>Easier to enter</strong>.</dd>' +
      '<dt><code>entry_delta = blank</code></dt>' +
      '<dd><strong>Hard-block</strong> — no longs allowed in this regime. Same as setting threshold to ∞.</dd>' +
      '<dt><code>exit_delta = −0.20</code></dt>' +
      '<dd>Require <code>down_prob ≥ 0.55 − 0.20 = 0.35</code>. <strong>Faster exits</strong> — close on weaker signals.</dd>' +
      '<dt><code>exit_delta = +0.05</code></dt>' +
      '<dd>Require <code>down_prob ≥ 0.60</code>. <strong>Hold longer</strong>, only exit on strong reversal.</dd>' +
    '</dl>' +
    '<h4 style="font-family:var(--mono);font-size:var(--t-2xs);font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--fg-3);margin:14px 0 6px;">Scalar parameters</h4>' +
    '<dl style="display:grid;grid-template-columns:160px 1fr;gap:4px 14px;margin:0 0 6px;">' +
      '<dt><code>high_vol_stake_factor</code></dt>' +
      '<dd>In <code>high_volatility</code>, multiply position size by this. Default <code>0.7</code> (30% smaller). Set <code>0.5</code> for half-size, <code>0</code> to skip entries entirely. Lower if drawdowns spike in volatile markets.</dd>' +
      '<dt><code>high_vol_min_confidence</code></dt>' +
      '<dd>In <code>high_volatility</code>, require <code>up_prob ≥ this</code> on top of the regular threshold. Default <code>0.65</code>. Higher = fewer but higher-conviction trades.</dd>' +
      '<dt><code>mean_rev_take_profit</code></dt>' +
      '<dd>In <code>mean_reverting</code>, exit immediately when profit reaches this fraction. Default <code>0.012</code> = +1.2%. Lower = quicker scalps; higher = let winners run further.</dd>' +
      '<dt><code>trending_up_trail_trigger</code></dt>' +
      '<dd>In <code>trending_up</code>, when profit exceeds this, activate trailing stop. Default <code>0.025</code> = 2.5%. Lower = trail sooner (lock in smaller wins); higher = wait for bigger wins before trailing.</dd>' +
      '<dt><code>trending_up_trail_distance</code></dt>' +
      '<dd>Once trailing is active, trail this far below the high-water mark (must be negative). Default <code>−0.02</code> = 2% below peak. More negative (e.g. <code>−0.03</code>) = wider trail, more room for noise; closer to <code>0</code> = tighter trail, gives back less but stops out sooner.</dd>' +
      '<dt><code>tft_min_confidence</code></dt>' +
      '<dd>TFT model\'s quantile-spread confidence floor. Default <code>0.35</code>. Below this, no entries fire in any regime. Raise to <code>0.45</code>+ to filter out low-conviction signals at the cost of fewer trades.</dd>' +
      '<dt><code>meta_min_confidence</code></dt>' +
      '<dd>When the DRL meta-agent (PPO + A2C + DQN ensemble) is active, require this confidence on the <code>meta_signal</code>. Same logic for entries (<code>signal=+1</code>) and exits (<code>signal=−1</code>). Default <code>0.35</code>.</dd>' +
    '</dl>' +
    '<h4 style="font-family:var(--mono);font-size:var(--t-2xs);font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--fg-3);margin:14px 0 6px;">Recommended tuning order</h4>' +
    '<ol style="margin:0 0 10px;padding-left:20px;">' +
      '<li><strong>Start with defaults</strong> — they\'re calibrated to work end-to-end.</li>' +
      '<li><strong>Too many losing entries:</strong> raise <code>tft_min_confidence</code> (+0.05 increments) or <code>meta_min_confidence</code>.</li>' +
      '<li><strong>No trades firing:</strong> lower <code>tft_min_confidence</code>, then check that no allowed regime has <code>entry_delta = blank</code>.</li>' +
      '<li><strong>Drawdowns in volatile markets:</strong> drop <code>high_vol_stake_factor</code> to <code>0.4</code>, raise <code>high_vol_min_confidence</code> to <code>0.8</code>.</li>' +
      '<li><strong>Profits get given back in trends:</strong> tighten <code>trending_up_trail_distance</code> closer to <code>0</code> (e.g. <code>−0.015</code>).</li>' +
      '<li><strong>Whipsawing in chop:</strong> raise <code>mean_rev_take_profit</code> to <code>0.018</code>+ to ignore tiny moves.</li>' +
    '</ol>' +
    '<div style="margin-top:10px;padding:8px 12px;background:var(--warn-bg);border-left:3px solid var(--warn);border-radius:4px;color:var(--fg-1);">' +
      '<strong>Apply changes</strong> writes <code>config.json</code> atomically (with timestamped backup) and triggers a freqtrade reload — the bot keeps running, but new candles will use the updated values. Open trades are not affected mid-flight; only future entries/exits use the new parameters.' +
    '</div>'
  );

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
      f[group][regime] = v;
      setForm(f);
    };
    const setScalar = (k, v) => {
      const f = JSON.parse(JSON.stringify(form));
      f[k] = v;
      setForm(f);
    };
    const reset = () => setForm(JSON.parse(JSON.stringify(cfg)));
    const submit = () => {
      // Compute diff
      const diff = [];
      regimes.forEach(r => {
        const oldE = (cfg.entry_delta || {})[r];
        const newE = form.entry_delta[r];
        if (Number(oldE) !== Number(newE)) diff.push("entry_delta[" + r + "] " + oldE + " → " + newE);
        const oldX = (cfg.exit_delta || {})[r];
        const newX = form.exit_delta[r];
        if (Number(oldX) !== Number(newX)) diff.push("exit_delta[" + r + "] " + oldX + " → " + newX);
      });
      scalars.forEach(k => {
        if (Number(cfg[k]) !== Number(form[k])) diff.push(k + " " + cfg[k] + " → " + form[k]);
      });
      if (diff.length === 0) { setToastMsg({ msg: "no changes to write", level: "info" }); return; }
      if (!confirm("Apply " + diff.length + " change(s)?\n\n" + diff.join("\n"))) {
        setToastMsg({ msg: "submission cancelled", level: "info" });
        return;
      }
      fetch("/api/ops/regime_config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      })
        .then(r => r.json())
        .then(env => {
          if (env.status === "ok") {
            setToastMsg({ msg: "wrote " + diff.length + " change(s) · " + diff[0] + (diff.length > 1 ? " (+ " + (diff.length - 1) + " more)" : ""), level: "ok" });
          } else {
            setToastMsg({ msg: "write failed · " + (env.error || "unknown"), level: "warn" });
          }
        })
        .catch(e => setToastMsg({ msg: "POST error · " + e.message, level: "warn" }));
    };

    const numCell = (val, range, onChange, ariaLabel) => h("input", {
      type: "number",
      value: val != null ? val : 0,
      step: 0.01,
      min: range ? range[0] : undefined,
      max: range ? range[1] : undefined,
      onChange: e => onChange(Number(e.target.value)),
      className: "select",
      "aria-label": ariaLabel,
      style: { width: 86, fontFamily: "var(--mono)", fontSize: "var(--t-xs)", textAlign: "right" },
    });

    return h(Card, {
      num: "19", title: "Regime config editor",
      sub: "atomic write · " + (env.config_path || "config.json"),
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
          // Static template literal embedded above in this file; no user
          // input ever flows here — XSS surface is nil.
          dangerouslySetInnerHTML: { __html: REGIME_PARAMS_GUIDE_HTML }
        })
      ),
      h("div", { className: "metric-label" }, "ENTRY DELTA · per regime"),
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 6, marginTop: 4 } },
        regimes.map(r => h("label", { key: r, style: { display: "flex", flexDirection: "column", gap: 4 } },
          h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, r),
          numCell(form.entry_delta[r], schema.delta_range, (v) => setDelta("entry_delta", r, v), `entry delta for ${r}`)
        ))
      ),
      h("div", { className: "hr" }),
      h("div", { className: "metric-label" }, "EXIT DELTA · per regime"),
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 6, marginTop: 4 } },
        regimes.map(r => h("label", { key: r, style: { display: "flex", flexDirection: "column", gap: 4 } },
          h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, r),
          numCell(form.exit_delta[r], schema.delta_range, (v) => setDelta("exit_delta", r, v), `exit delta for ${r}`)
        ))
      ),
      h("div", { className: "hr" }),
      h("div", { className: "metric-label" }, "SCALAR PARAMS"),
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginTop: 4 } },
        scalars.map(k => h("label", { key: k, style: { display: "grid", gridTemplateColumns: "1fr auto", gap: 6, alignItems: "center" } },
          h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, k),
          numCell(form[k], (schema.scalar_ranges || {})[k], (v) => setScalar(k, v), k)
        ))
      ),
      h("div", { className: "hr" }),
      h("div", { style: { display: "flex", gap: "var(--s-3)", alignItems: "center" } },
        h("button", { className: "btn", onClick: submit }, "APPLY"),
        h("button", { className: "btn", onClick: reset }, "RESET"),
        toastMsg.msg && h("span", {
          className: toastMsg.level === "ok" ? "up" : toastMsg.level === "warn" ? "down" : "dim",
          style: { fontSize: "var(--t-xs)", fontFamily: "var(--mono)", flex: 1, textAlign: "right" }
        }, toastMsg.msg)
      )
    );
  }

  // ─────────────── SLACK PREVIEW — next daily report (data-num 20) ───────────────
  function SlackPreviewLive({ data }) {
    const env = envelopeData(data.slack_preview) || {};
    const sign = (env.pnl_usd || 0) >= 0 ? "+" : "−";
    const pnlAbs = Math.abs(Number(env.pnl_usd || 0));
    const emoji = (env.pnl_usd || 0) >= 0 ? "📈" : "📉";
    const regimeRows = env.regime_distribution || [];
    return h(Card, {
      num: "20", title: "Slack preview · next daily brief",
      sub: "fires at 00:00 UTC · " + (env.date_utc || ""),
      right: h(F, null,
        h(TimeSince, { ts: data.slack_preview_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        h("span", { className: "pill accent" }, h("span", { className: "dot accent pulse" }), " PREVIEW")
      )
    },
      h("div", {
        style: {
          background: "var(--bg-inset)", padding: "var(--s-3) var(--s-4)", borderRadius: 4,
          fontFamily: "var(--mono)", fontSize: "var(--t-xs)", lineHeight: 1.7, color: "var(--fg-1)",
          borderLeft: "3px solid var(--accent)",
        }
      },
        h("div", { style: { fontWeight: 600 } },
          emoji + " Quanta · daily P&L · " + (env.date_utc || "")),
        h("div", null,
          "• Day P&L: ",
          h("span", { className: (env.pnl_usd || 0) >= 0 ? "up" : "down" },
            sign + "$" + fmtUSD(pnlAbs, 2) + "  (" + fmtPct(env.pnl_pct || 0) + ")")),
        h("div", null, "• Trades: " + (env.trades || 0) + " · wins " + (env.wins || 0) + " · losses " + (env.losses || 0) + " · win rate " + (env.win_rate_pct || 0).toFixed(1) + "%"),
        h("div", null, "• Sharpe (trailing): " + (env.sharpe_trailing != null ? Number(env.sharpe_trailing).toFixed(2) : "—") +
          " · MaxDD: " + (env.max_dd_trailing != null ? Number(env.max_dd_trailing).toFixed(2) + "%" : "—")),
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
    const [selected, setSelected] = useState("");
    const [argsText, setArgsText] = useState("{}");
    const [result, setResult] = useState(null);
    const [running, setRunning] = useState(false);
    const [err, setErr] = useState(null);

    useEffect(() => {
      if (!selected && tools.length) setSelected(tools[0].name);
    }, [tools.length]);

    const cur = tools.find(t => t.name === selected);
    useEffect(() => {
      // Generate a default args body matching the tool's params
      if (!cur) return;
      const defaults = {};
      (cur.params || []).forEach(p => {
        if (p.default !== null && p.default !== undefined) defaults[p.name] = p.default;
        else if (p.type === "int") defaults[p.name] = 0;
        else if (p.type === "bool") defaults[p.name] = false;
        else defaults[p.name] = "";
      });
      setArgsText(JSON.stringify(defaults, null, 2));
      setResult(null);
      setErr(null);
    }, [selected]);

    const run = () => {
      if (!selected) return;
      let body;
      try { body = JSON.parse(argsText || "{}"); }
      catch (e) { setErr("invalid JSON: " + e.message); return; }
      setRunning(true);
      setErr(null);
      setResult(null);
      fetch("/api/ops/mcp/" + selected, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })
        .then(r => r.json().then(j => ({ ok: r.ok, status: r.status, j })))
        .then(({ ok, status, j }) => {
          setRunning(false);
          if (!ok) setErr("HTTP " + status + " · " + (j && j.error ? j.error : ""));
          setResult(j);
        })
        .catch(e => { setRunning(false); setErr("fetch error: " + e.message); });
    };

    return h(Card, {
      num: "21", title: "MCP tool console",
      sub: tools.length + " tools · POST /api/ops/mcp/{name}",
      right: h(F, null,
        h(TimeSince, { ts: data.tools_fetched_at, className: "mono dim", style: { fontSize: "var(--t-2xs)", marginRight: 8 } }),
        cur && cur.mutating
          ? h("span", { className: "pill warn" }, h("span", { className: "dot warn pulse" }), " MUTATING")
          : h("span", { className: "pill" }, "read-only")
      )
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "260px 1fr", gap: "var(--s-3)" } },
        h("div", null,
          h("div", { className: "metric-label" }, "TOOL"),
          h("select", {
            className: "select",
            value: selected,
            onChange: e => setSelected(e.target.value),
            style: { width: "100%", marginTop: 4, fontFamily: "var(--mono)", fontSize: "var(--t-xs)" }
          },
            tools.map(t => h("option", { key: t.name, value: t.name }, (t.mutating ? "❗ " : "") + t.name))
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

    return h(Card, {
      num: "13", title: "Sentiment aggregate",
      sub: score != null ? "net " + (score >= 0 ? "+" : "") + score.toFixed(2) : "—",
      right: cardRight(slot.fetchedAt,
        h("span", { className: "pill " + klass }, score == null ? "—" : score >= 0 ? "BULLISH" : "BEARISH"))
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "DEEP (Claude)"),
        h("div", { className: "num " + ((env.deep_score || 0) >= 0 ? "up" : "down") },
          env.deep_score != null ? ((env.deep_score >= 0 ? "+" : "") + Number(env.deep_score).toFixed(2)) : "—"),
        h("div", { className: "dim mono" }, "FAST (Llama)"),
        h("div", { className: "num " + ((env.fast_score || 0) >= 0 ? "up" : "down") },
          env.fast_score != null ? ((env.fast_score >= 0 ? "+" : "") + Number(env.fast_score).toFixed(2)) : "—"),
        h("div", { className: "dim mono" }, "F&G"),
        h("div", { className: "num" }, env.fear_greed != null ? (env.fear_greed + (env.fear_greed_label ? " · " + env.fear_greed_label : "")) : "—"),
        h("div", { className: "dim mono" }, "AGREEMENT"),
        h("div", { className: "num " + (env.agreement ? "up" : "warn") }, env.agreement ? "yes" : "no"),
        h("div", { className: "dim mono" }, "HEADLINES"),
        h("div", { className: "num" }, env.n_headlines != null ? env.n_headlines : "—"),
        h("div", { className: "dim mono" }, "AGE"),
        h("div", { className: "num" }, env.age_s != null ? Math.floor(env.age_s / 60) + "m" : "—")
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
      phases.length === 0
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)" } }, "no phase blocks for today yet")
        : h(F, null,
            // Per-phase rows
            phases.map((p, i) => h("div", { key: i, style: { display: "flex", gap: 12, padding: "6px 0", borderBottom: "1px solid var(--line-2)", alignItems: "flex-start" } },
              h("div", { className: "mono dim", style: { fontSize: "var(--t-xs)", minWidth: 100 } }, p.phase + " · " + p.time + " " + p.tz),
              h("div", { style: { flex: 1, fontSize: "var(--t-xs)" } },
                p.confirmed.length
                  ? h("div", { style: { color: "var(--up)" } }, "✓ confirmed: ", p.confirmed.join(", "))
                  : h("div", { className: "dim" }, "✓ confirmed: (none)"),
                p.skipped.length
                  ? h("div", { className: "dim" }, "✗ skipped: " + p.skipped.join(", "))
                  : null,
                p.market_summary
                  ? h("div", { className: "dim", style: { fontSize: "var(--t-2xs)" } }, p.market_summary)
                  : null,
              ),
            )),
            // Trade-block explanation block — clarifies WHY no entries fired
            env.trade_block_explanation
              ? h("div", { style: { marginTop: 10, padding: "8px 10px", background: "var(--bg-inset)", borderRadius: 4, fontSize: "var(--t-2xs)", color: "var(--fg-2)", lineHeight: 1.5 } }, env.trade_block_explanation)
              : null
          )
    );
  }

  // ─────────────── CHAMPION GENOME (slow card, 60s) ───────────────
  function ChampionCardLive({ data }) {
    const slot = slotState(data, "ept_champion");
    const env = envelopeData(slot.env) || {};
    const id = env.member_id || env.genome_id || env.id || "—";
    const metrics = env.metrics || {};
    const sharpe = metrics.sharpe_ratio != null ? metrics.sharpe_ratio : metrics.sharpe;
    const maxDd = metrics.max_drawdown;
    const profitFactor = metrics.profit_factor;
    const nTrades = metrics.num_trades != null ? metrics.num_trades : metrics.n_trades;
    const fitness = env.fitness;
    const genome = env.genome || {};

    if (slot.phase === "down") {
      return h(Card, {
        num: "14", title: "EPT · champion genome",
        sub: "endpoint unavailable",
        right: cardRight(slot.fetchedAt)
      },
        h(EmptyState, { reason: slot.reason, fetchedAt: slot.fetchedAt, period: 60 })
      );
    }

    return h(Card, {
      num: "14", title: "EPT · champion genome",
      sub: "evolution head · refresh 60s",
      right: cardRight(slot.fetchedAt)
    },
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, fontSize: "var(--t-xs)" } },
        h("div", { className: "dim mono" }, "ID"),            h("div", { className: "num accent" }, id),
        h("div", { className: "dim mono" }, "FITNESS"),       h("div", { className: "num up" }, fitness != null ? Number(fitness).toFixed(3) : "—"),
        h("div", { className: "dim mono" }, "SHARPE"),        h("div", { className: "num" }, sharpe != null ? Number(sharpe).toFixed(2) : "—"),
        h("div", { className: "dim mono" }, "MAX DD"),        h("div", { className: "num down" }, maxDd != null ? "−" + (Number(maxDd) * 100).toFixed(2) + "%" : "—"),
        h("div", { className: "dim mono" }, "PROFIT FACTOR"), h("div", { className: "num" }, profitFactor != null ? Number(profitFactor).toFixed(2) : "—"),
        h("div", { className: "dim mono" }, "N TRADES"),      h("div", { className: "num" }, nTrades != null ? nTrades : "—"),
        h("div", { className: "dim mono" }, "STOP/TP"),       h("div", { className: "num mono", style: { fontSize: "var(--t-2xs)" } },
          (genome.stop_loss != null ? (Number(genome.stop_loss) * 100).toFixed(2) + "%" : "—") + " / "
          + (genome.take_profit != null ? (Number(genome.take_profit) * 100).toFixed(2) + "%" : "—")),
        h("div", { className: "dim mono" }, "FEATURES"),      h("div", { className: "num" }, (genome.feature_subset || []).length)
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
        h("div", { className: "num " + (dayPnl >= 0 ? "up" : "down") }, (dayPnl >= 0 ? "+$" : "−$") + fmtUSD(Math.abs(dayPnl))),
        h("div", { className: "dim mono" }, "DAY %"),
        h("div", { className: "num " + (dayPct >= 0 ? "up" : "down") }, fmtPct(dayPct)),
        h("div", { className: "dim mono" }, "DD 30d"),
        h("div", { className: "num " + (dd30 != null && dd30 < 0 ? "down" : "dim") }, dd30 != null ? fmtPct(dd30) : "—"),
        h("div", { className: "dim mono" }, "OPEN"),
        h("div", { className: "num" }, (env.open_count || 0) + " / " + (env.max_open || 0))
      ),
      env.live_tape && env.live_tape.length > 0 && h("div", null,
        h("div", { className: "hr" }),
        h("div", { className: "metric-label" }, "LAST CLOSED · TAPE"),
        h("div", { style: { fontSize: "var(--t-xs)", maxHeight: 120, overflowY: "auto", marginTop: 4 } },
          env.live_tape.slice(0, 8).map((r, i) => {
            const tapePct = Number(r.pnl_pct || 0) * 100;
            return h("div", {
              key: i, style: { display: "grid", gridTemplateColumns: "1fr 60px 70px 1fr", gap: 6, padding: "2px 0" }
            },
              h("span", { className: "mono" }, r.pair),
              h("span", { className: "mono dim" }, r.side),
              h("span", { className: "num " + (tapePct >= 0 ? "up" : "down"), style: { textAlign: "right" } }, fmtPct(tapePct)),
              h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } }, r.regime_at_entry || "—")
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
      // ── Section A: portfolio breaker (unified_risk) ──
      h("div", { style: { marginBottom: "var(--s-3)" } },
        h("div", { className: "dim2 mono", style: { fontSize: "var(--t-2xs)", letterSpacing: ".08em", textTransform: "uppercase", marginBottom: "var(--s-2)" } },
          "Portfolio · unified_risk"),
        h("div", { style: { display: "flex", flexDirection: "column", gap: "var(--s-1)" } },
          portfolioConditions.map((c, i) => h("div", { key: i,
            style: { display: "flex", alignItems: "center", gap: 8, fontSize: "var(--t-xs)",
              padding: "var(--s-1) var(--s-2)", borderLeft: "2px solid " + (c.tripped ? "var(--c-down)" : "var(--c-up)"),
              background: c.tripped ? "color-mix(in srgb, var(--c-down) 7%, transparent)" : "transparent" } },
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
  // Reads /api/ops/backtest_gates which is written by the weekly Hermes cron
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
        ? h("div", { className: "dim", style: { fontSize: "var(--t-xs)", padding: "var(--s-3)" } },
            "No gates_report_*_latest.json on disk. Sunday 4am ET cron will populate.")
        : h("div", { style: { display: "flex", flexDirection: "column", gap: 0 } },
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
                          color: g.pass ? "var(--c-up)" : "var(--c-down)", minWidth: 70, textAlign: "right" } },
                        formatGateValue(g.value)),
                      h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } },
                        " / " + formatGateValue(g.threshold)),
                      h("span", { className: "dim", style: { fontSize: "var(--t-2xs)", flex: 1, textAlign: "right" } },
                        g.detail || "")
                    )))
                ),
              ].filter(Boolean));
            })
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
                color: copied === "prompt" ? "var(--c-up)" : "var(--fg-2)",
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
                color: copied === "response" ? "var(--c-up)" : "var(--fg-2)",
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
  const AGENT_FLOW_ORDER = [
    "regime_tagger",
    "indicator_selector",
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

  function AgentFlowBox({ role, detail, onClick }) {
    const empty = !detail || !detail.count;
    const cls = "af-box " + _afFreshnessClass(detail);
    const dotCls = _afDotClass(detail);
    const ageLabel = empty ? "no calls today" : _afAgeLabel(detail.last_ts);
    const ariaLabel = empty
      ? role + " — no calls in 24h window"
      : role + " — " + detail.count + " calls, last " + ageLabel;
    const boxRef = useRef(null);
    // Tier E: inline "last:" preview line uses last_response_gist (added
    // server-side in commit 6528a7f). Falls back to last_gist for compat
    // with any older payload shape still in cache. Empty when no calls.
    const lastGist = detail && (detail.last_response_gist || detail.last_gist);
    return h("div", {
      ref: boxRef,
      className: cls,
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
      h("div", { className: "af-title" }, role),
      h("div", { className: "af-model" }, empty ? "—" : (detail.model || "—")),
      h("div", { className: "af-live" },
        dotCls && h("span", { className: "dot " + dotCls }),
        h("span", null, ageLabel)
      ),
      empty
        ? h("div", { className: "af-counts af-placeholder" }, "—")
        : h("div", { className: "af-counts" },
            h("span", { className: "af-ok" }, detail.success, " ✓"),
            h("span", { className: "af-sep" }, "·"),
            h("span", { className: "af-bad" }, detail.fail, " ✕")
          ),
      empty
        ? h("div", { className: "af-latency af-placeholder" }, "—")
        : h("div", { className: "af-latency" },
            "avg ", (detail.avg_latency_s || 0).toFixed(1), "s · p95 ",
            (detail.p95_latency_s || 0).toFixed(1), "s"),
      h("div", { className: "af-gist", title: detail && detail.last_gist || "" },
        empty ? "no calls today" : (detail.last_gist || "—")),
      // Tier E: inline "last:" preview row — render-skipped entirely when
      // the role has no calls, so empty boxes don't gain a stray "last: -".
      !empty && lastGist && h("div", {
        className: "af-last",
        title: lastGist,
      },
        h("span", { className: "af-last-key" }, "last:"),
        '"', _aldTrim(lastGist, 60), '"'
      )
    );
  }

  function AgentFlowArrow({ hopSec }) {
    return h("div", { className: "af-arrow", "aria-hidden": "true" },
      h("span", { className: "af-arrow-line" }),
      hopSec != null && h("span", { className: "af-hop" }, hopSec.toFixed(1) + "s"),
      h("span", { className: "af-glyph" }, "→")
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

    // Skip indicator_selector when it has zero calls — today's bot doesn't
    // emit it. The other five stay even when empty so gaps are visible.
    const slots = AGENT_FLOW_ORDER.filter(r =>
      !(r === "indicator_selector" && !detailByRole[r])
    );

    // Re-render every 30 s so the "Xm ago" labels stay current between
    // 10 s data polls — does NOT fetch anything, just bumps state.
    const [, _tick] = useState(0);
    useEffect(() => {
      const iv = setInterval(() => _tick(n => n + 1), 30_000);
      return () => clearInterval(iv);
    }, []);

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
          ? "no canonical-role calls in 24h window — strip shows pipeline shape"
          : Object.keys(detailByRole).length + " of " + slots.length + " roles active · "
            + (summary.total_calls || 0) + " calls in 24h";

    // Build the box+arrow sequence. Each gap between adjacent boxes gets
    // one arrow; hop latency is the avg latency of the destination box
    // (proxy for "how long does this stage typically take"). When the
    // destination is empty we omit the latency chip.
    const children = [];
    slots.forEach((role, idx) => {
      const detail = detailByRole[role] || null;
      children.push(h(AgentFlowBox, { key: "box_" + role, role, detail, onClick: click }));
      if (idx < slots.length - 1) {
        const next = detailByRole[slots[idx + 1]] || null;
        const hopSec = next && next.count ? next.avg_latency_s : null;
        children.push(h(AgentFlowArrow, { key: "arr_" + role, hopSec }));
      }
    });

    return h(Card, {
      num: "21a",
      title: "Agent flow",
      sub: subText,
      right: cardRight(slot.fetchedAt),
    },
      h("div", { className: "agent-flow", id: "agent-flow-strip" }, children)
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
    const [search, setSearch] = useState("");
    const [shown, setShown] = useState(50);  // "load more" pagination
    const [selectedTs, setSelectedTs] = useState(null);
    const [modalRec, setModalRec] = useState(null);
    const [modalLoading, setModalLoading] = useState(false);
    const [modalError, setModalError] = useState(null);
    const searchRef = useRef(null);

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
        if (agentFilter && !raws.includes(agentFilter)) setAgentFilter("");
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
    }, [agentFilter]);

    // Click a row → fetch full record (include_text=1) for the modal.
    const openCall = useCallback((rec) => {
      if (!rec || !rec.timestamp) return;
      setSelectedTs(rec.timestamp);
      setModalLoading(true); setModalError(null); setModalRec(rec);  // optimistic
      const url = "/api/ops/llm_calls/" + encodeURIComponent(rec.timestamp);
      fetch(url)
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
          const c = (env && env.data && env.data.call) || rec;
          setModalRec(c);
          setModalLoading(false);
        })
        .catch(err => {
          // Fallback: render the row data we have (metadata-only) and
          // surface the error so operator knows full text isn't available.
          setModalRec(rec);
          setModalError(String(err && err.message || err));
          setModalLoading(false);
        });
    }, []);

    const closeModal = useCallback(() => {
      setSelectedTs(null);
      setModalRec(null);
      setModalError(null);
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
        if (pat) {
          const hay = [c.agent, c.model, c.tier, c.role, c.provider]
            .filter(Boolean).join(" ");
          if (!pat.test(hay)) return false;
        }
        return true;
      });
    }, [callsAll, agentFilter, search]);

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
    const stat = (lbl, val, cls) => h("div", { style: { display: "flex", flexDirection: "column", gap: 2, minWidth: 100 } },
      h("div", { className: "dim2 mono", style: { fontSize: "var(--t-2xs)", letterSpacing: ".08em", textTransform: "uppercase" } }, lbl),
      h("div", { className: "num " + (cls || ""), style: { fontSize: "var(--t-md)", fontFamily: "var(--mono)", fontVariantNumeric: "tabular-nums" } }, val)
    );

    const ollamaPct = Number(summary.ollama_pct || 0);
    const ollamaCls = ollamaPct >= 80 ? "up" : ollamaPct >= 50 ? "warn" : "down";
    const successCls = Number(summary.success_pct || 100) >= 99 ? "up"
                    : Number(summary.success_pct || 100) >= 95 ? "warn" : "down";
    const isEmpty = callsAll.length === 0;

    const rightPill = h("span", { className: "pill info", style: { height: 18 } },
      h("span", { className: "dot info" + (isEmpty ? "" : " pulse") }), " ",
      isEmpty ? "NO CALLS YET" : (summary.total_calls || 0) + " · 24H");

    return h(F, null,
      h("div", { id: "llm-calls-card" },
        h(Card, {
          num: "21", title: "LLM activity · last 24h",
          sub: isEmpty
            ? "No calls written yet — tracker hasn't fired or log file missing"
            : "feed · " + (summary.total_calls || 0) + " calls · "
              + fmtTokensCount(summary.total_tokens) + " tokens · "
              + (logSize ? Math.round(logSize / 1024) + " KB on disk" : "—"),
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
            (agentFilter || search) && h("button", {
              type: "button",
              onClick: () => { setAgentFilter(""); setSearch(""); },
              style: {
                background: "transparent", border: "1px solid var(--line-2)",
                color: "var(--fg-2)", padding: "3px 8px", cursor: "pointer",
                fontFamily: "var(--mono)", fontSize: "var(--t-2xs)", borderRadius: 4,
              }
            }, "clear"),
            h("span", { className: "tb-spacer", style: { flex: 1 } }),
            h("span", { className: "dim mono", style: { fontSize: "var(--t-2xs)" } },
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
              // Column header
              h("div", {
                style: {
                  display: "grid",
                  gridTemplateColumns: "76px 1fr 130px 64px 90px 22px",
                  gap: "var(--s-2)", alignItems: "center",
                  fontSize: "var(--t-2xs)", fontFamily: "var(--mono)",
                  color: "var(--fg-3)", textTransform: "uppercase",
                  letterSpacing: ".08em", padding: "4px 6px",
                  borderBottom: "1px solid var(--line-1)",
                }
              },
                h("span", null, "time"),
                h("span", null, "agent"),
                h("span", null, "model · tier"),
                h("span", { style: { textAlign: "right" } }, "lat"),
                h("span", { style: { textAlign: "right" } }, "tokens"),
                h("span", null, "")
              ),
              filtered.slice(0, shown).map((c, i) => {
                const lat = Number(c.latency_seconds || 0);
                const latCls = fmtLatencyClass(lat);
                const pTok = c.prompt_tokens || 0;
                const cTok = c.completion_tokens || 0;
                const isOpen = selectedTs === c.timestamp;
                // Status dot: success unless ``success===false`` (future); we
                // also treat completion_tokens===0 + latency===0 as failed
                // because a real round-trip should produce at least one of
                // them.
                const failed = c.success === false || (lat === 0 && cTok === 0);
                const statusCls = failed ? "down" : "up";
                return h("div", {
                  key: c.timestamp + "_" + i,
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
                    gridTemplateColumns: "76px 1fr 130px 64px 90px 22px",
                    gap: "var(--s-2)", alignItems: "center",
                    fontSize: "var(--t-xs)", fontFamily: "var(--mono)",
                    padding: "5px 6px",
                    borderBottom: "1px solid var(--line-1)",
                    cursor: "pointer",
                    background: isOpen ? "var(--bg-inset)" : "transparent",
                  }
                },
                  h("span", { className: "dim" }, fmtHHMMSS(c.timestamp)),
                  h("span", { style: { color: "var(--fg-1)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } },
                    c.agent || "—"),
                  h("span", { className: "dim", style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } },
                    (c.model || "—") + " · " + (c.tier || "?")),
                  h("span", { className: latCls, style: { textAlign: "right" } },
                    lat.toFixed(2) + "s"),
                  h("span", { className: "dim", style: { textAlign: "right" } },
                    pTok + "/" + cTok),
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
          background: "var(--bg-card)", color: "var(--c-warn)",
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
        : h("div", { style: { display: "flex", flexDirection: "column", gap: "var(--s-3)" } },
            decisions.map((d, i) => {
              const isBlocked = d.kind === "blocked";
              const verdictCls = isBlocked ? "warn" : "up";
              const verdict = isBlocked ? "NO ENTRY · blocked" : ("ENTRY · " + (d.side || "long"));
              const reason = isBlocked
                ? (d.reason || "—") + " (constraint=" + (d.constraint || "—") + ")"
                : (d.reasoning || ((d.regime || "—") + " · conf " + (d.confidence != null ? Number(d.confidence).toFixed(2) : "—")));
              const ts = (d.ts || "").replace("T", " ").slice(0, 19);
              return h("div", {
                key: i,
                style: {
                  border: "1px solid var(--line-1)", borderRadius: 4,
                  padding: "var(--s-2) var(--s-3)",
                  display: "flex", flexDirection: "column", gap: 4,
                }
              },
                h("div", { style: { display: "flex", alignItems: "baseline", gap: "var(--s-2)" } },
                  h("span", { className: "mono dim", style: { fontSize: "var(--t-2xs)" } }, ts || "—"),
                  h("span", { className: "tb-spacer", style: { flex: 1 } }),
                  h("span", { className: "pill " + verdictCls, style: { height: 18 } }, verdict)),
                h("div", { className: "dim", style: { fontSize: "var(--t-xs)", lineHeight: 1.45 } }, reason)
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
          // HERO
          h(HeroLive, { data, killState }),
          // TRAINING ROW — crypto FreqAI + stocks Shark TFT side-by-side.
          // Operator wants both pipelines visible in one glance near the top
          // of the page (not buried at row 17). Wired data-num 17 (crypto)
          // and 13 (stocks) cards to a single training band.
          h("div", { id: "training", className: "grid g-12 anchor", style: { gap: "var(--gap-grid)" } },
            h("div", { style: { gridColumn: "span 6" } }, h(TrainingCardLive, { data })),
            h("div", { style: { gridColumn: "span 6" } }, h(StocksMLLive, { data }))
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
          h(PairTelemetryLive, { data }),
          h(StocksPairTelemetryLive, { data }),
          // SERVICES + POSITIONS
          h("div", { className: "grid g-12", style: { gap: "var(--gap-grid)" } },
            h("div", { style: { gridColumn: "span 4" } }, h(ServicesLive, { data })),
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
            h("div", { style: { gridColumn: "span 6" } }, h(QuickActions, { killState, setKillState }))
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
      )
    );
  }

  // Mount
  const root = ReactDOM.createRoot(document.getElementById("root"));
  root.render(h(OpsApp));
})();
