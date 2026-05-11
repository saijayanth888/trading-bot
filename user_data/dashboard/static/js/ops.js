/* Ops tab — fetches the six /api/ops/* endpoints on staggered intervals
 * and renders them. No framework; vanilla DOM. Fail-soft: any panel that
 * times out / errors goes amber/red without crashing the page.
 *
 * Defense-in-depth: every variable interpolated into HTML goes through
 * esc() first. Data ultimately comes from our own DB, but a malicious
 * pair name / regime label / log line shouldn't be able to inject script.
 */

const REFRESH_MS = {
  services:        5000,
  training:       10000,
  regime:         30000,
  sentiment:      30000,
  mcp:            15000,
  trades:          5000,
  sparklines:     60000,   // 5m candles only change every 5 min
  slack_preview:  60000,   // daily report preview — once a min is plenty
  explainability: 30000,   // trade journal updates on each entry/exit
  stocks:         30000,   // stocks state writes on cron tick (~5min) + nightly
  live_trades:     5000,   // top hero strip — pulse on every fill
  stock_regime:   60000,   // SPY MA bands move slowly
  gates:          15000,   // gates change with each candle
  combined:       30000,   // unified crypto + stocks portfolio
  llm:            60000,   // LLM tracker stats
  cb:             10000,   // circuit-breaker state — pulse fast for failovers
};

const REGIME_ARROW = {
  trending_up:    "↑",
  trending_down:  "↓",
  mean_reverting: "↔",
  high_volatility: "⚡",
};

const FETCH_TIMEOUT_MS = 3000;

// ─── HTML escaper ───────────────────────────────────────────────────
function esc(v) {
  if (v === null || v === undefined) return "";
  return String(v)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

async function jsonFetch(url, opts = {}) {
  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const r = await fetch(url, { ...opts, signal: controller.signal });
    return { code: r.status, body: await r.json().catch(() => ({})) };
  } catch (err) {
    return { code: 0, body: { status: "down", error: err.message || "fetch failed" } };
  } finally {
    clearTimeout(t);
  }
}

function fmt(n, d = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toFixed(d);
}

function fmtPct(n) {
  if (n === null || n === undefined) return "—";
  const s = (n >= 0 ? "+" : "") + Number(n).toFixed(2) + "%";
  return s.replace("-", "−"); // real minus sign
}

function fmtUsd(n) {
  if (n === null || n === undefined) return "—";
  const sign = n >= 0 ? "+" : "−";
  return sign + "$" + Math.abs(Number(n)).toFixed(2);
}

function setStatus(id, status) {
  const el = document.getElementById(id);
  if (el) el.dataset.status = status;
}

function setRefresh(ts) {
  const el = document.getElementById("last-refresh");
  if (!el || !ts) return;
  const d = new Date(ts);
  el.textContent = d.toLocaleTimeString("en-US", {
    hour: "numeric", minute: "2-digit", second: "2-digit",
    hour12: true, timeZone: "America/New_York",
  }) + " ET";
}

// ─── Hero (regime + sentiment) ──────────────────────────────────────
async function refreshRegime() {
  const r = await jsonFetch("/api/ops/regime");
  const env = r.body;
  setStatus("hero", env.status || "down");
  setRefresh(env.checked_at);
  if (env.status === "down") {
    document.getElementById("hero-regime").textContent = "Regime — DOWN";
    document.getElementById("hero-meta").textContent = env.error || "";
    return;
  }
  const d = env.data || {};
  const arrow = REGIME_ARROW[d.current] || "·";
  document.getElementById("hero").dataset.regime = d.current || "";
  const heroEl = document.getElementById("hero-regime");
  while (heroEl.firstChild) heroEl.removeChild(heroEl.firstChild);
  heroEl.appendChild(document.createTextNode("Market: "));
  const strong = document.createElement("strong");
  strong.textContent = (d.current || "—").replace("_", " ");
  heroEl.appendChild(strong);
  heroEl.appendChild(document.createTextNode(" "));
  const arrowSpan = document.createElement("span");
  arrowSpan.className = "arrow";
  arrowSpan.textContent = arrow;
  heroEl.appendChild(arrowSpan);
  const dur = d.duration_hours ? `${Number(d.duration_hours).toFixed(1)}h` : "—";
  const prob = d.probability ? Number(d.probability).toFixed(2) : "—";
  // The HMM model writes once per closed hour. row-age is therefore
  // expected to be 0–60 min mid-cycle; that's not staleness, that's
  // cadence. Show as minutes + a "next tick" hint so it stops looking
  // alarming.
  const ageMin = d.age_s == null ? null : Math.floor(d.age_s / 60);
  const tickHint = ageMin == null ? "" : ` · next tick in ~${Math.max(0, 60 - ageMin)}m`;
  document.getElementById("hero-meta").textContent =
    `prob ${prob} · active ${dur}` + (ageMin != null ? ` · last tick ${ageMin}m ago${tickHint}` : "");
}

async function refreshSentiment() {
  const r = await jsonFetch("/api/ops/sentiment");
  const env = r.body;
  if (env.status === "down" || !env.data) return;
  const d = env.data;
  const direction = d.score > 0.1 ? "bullish" : d.score < -0.1 ? "bearish" : "neutral";
  const agree = d.agreement ? "✓ agree" : "✗ disagree";
  // Per-model breakdown shows WHY the aggregate is what it is. A 0.00
  // aggregate with deep=0.45/fast=-0.30 means real disagreement between
  // models, not "no signal" — the operator can see and act on that.
  const fastStr = d.fast_score == null ? "—" : `${fmt(d.fast_score, 2)}/${d.fast_impact || "?"}`;
  const deepStr = d.deep_score == null ? "—" : `${fmt(d.deep_score, 2)}/${d.deep_impact || "?"}`;
  const fgStr = d.fear_greed == null ? "" : ` · F&G ${d.fear_greed} ${d.fear_greed_label || ""}`;
  const meta = document.getElementById("hero-meta");
  meta.textContent = meta.textContent +
    ` · agg ${fmt(d.score, 2)} (${direction}, ${agree}, conf ${fmt(d.confidence, 2)}) · fast ${fastStr} · deep ${deepStr}${fgStr} · ${d.n_headlines} hl`;
}

// ─── Services ───────────────────────────────────────────────────────
async function refreshServices() {
  const r = await jsonFetch("/api/ops/services");
  const env = r.body;
  setStatus("card-services", env.status || "down");
  document.getElementById("services-age").textContent = env.error || "live";
  const body = document.getElementById("services-body");
  if (!env.data) { body.textContent = env.error || "—"; return; }
  body.replaceChildren();
  const labels = {
    ollama: "ollama", hermes_mcp: "hermes-mcp", hermes_gateway: "hermes-gateway",
    hermes_dashboard: "hermes UI", freqtrade: "freqtrade", postgres: "postgres",
    influxdb: "influxdb", grafana: "grafana",
  };
  for (const k of Object.keys(labels)) {
    const v = env.data[k] || {};
    const ok = v.up;
    const tickHtml = ok
      ? "<span class=\"ok\">✓</span>"
      : "<span class=\"bad\">✗</span>";
    const note = ok ? "" : ` <span class="muted">(${esc(v.error || v.via || "")})</span>`;
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML =
      `<span class="label">${tickHtml} ${esc(labels[k])}</span>` +
      `<span class="value muted">${esc(v.via || "")}</span>${note}`;
    body.appendChild(row);
  }
}

// ─── Training ───────────────────────────────────────────────────────
function fmtDur(s) {
  if (s === null || s === undefined) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s/60)}m ${Math.round(s%60)}s`;
  return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m`;
}

async function refreshTraining() {
  const r = await jsonFetch("/api/ops/training");
  const env = r.body;
  setStatus("card-training", env.status || "down");
  document.getElementById("training-age").textContent = env.error ? "amber" : "live";
  const body = document.getElementById("training-body");
  if (!env.data) { body.textContent = env.error || "—"; return; }
  const d = env.data;
  body.replaceChildren();

  // ── TFT block: per-pair queue + current progress ──
  if (d.tft) {
    const t = d.tft;
    const readyClass = t.pair_dict_ready ? "ok" : "warn";
    const readyText = t.pair_dict_ready ? "READY (pair_dict written)" : "WARM-UP (pair_dict pending)";

    // Header status line
    body.insertAdjacentHTML("beforeend",
      `<div class="row"><span class="label">freqai state</span>` +
      `<span class="value ${readyClass}">${esc(readyText)}</span></div>`
    );

    // Current pair training (if any)
    if (t.current_pair) {
      const pct = t.max_epoch ? (t.epoch / t.max_epoch * 100) : 0;
      body.insertAdjacentHTML("beforeend",
        `<div class="row"><span class="label">training</span>` +
        `<span class="value">${esc(t.current_pair)} · epoch ${esc(t.epoch || "?")}/${esc(t.max_epoch || "?")}</span></div>` +
        `<div class="progress"><div style="width:${esc(pct.toFixed(1))}%"></div></div>` +
        `<div class="row"><span class="label muted">val_sharpe · ETA · avg/epoch</span>` +
        `<span class="value">${fmt(t.val_sharpe, 2)} · <strong>${esc(fmtDur(t.current_pair_eta_s))}</strong> · ${esc(fmtDur(t.avg_epoch_seconds))}</span></div>`
      );
    }

    // Per-pair queue table (compact)
    if (t.pairs && t.pairs.length) {
      let table = `<table class="tape" style="margin-top:8px;"><thead><tr><th>pair</th><th>status</th><th>last ep</th><th>sharpe</th></tr></thead><tbody>`;
      for (const p of t.pairs) {
        const tick = p.status === "done" ? "<span class=\"ok\">✓</span>" : "<span class=\"warn\">…</span>";
        const ep = p.last_epoch != null ? `${esc(p.last_epoch)}/${esc(p.max_epoch || "?")}` : "—";
        const sharpe = p.val_sharpe != null ? fmt(p.val_sharpe, 2) : "—";
        const stop = p.early_stopped ? " <span class=\"muted\">(early-stop)</span>" : "";
        table += `<tr><td>${esc(p.pair)}</td><td>${tick} ${esc(p.status)}${stop}</td><td>${ep}</td><td>${sharpe}</td></tr>`;
      }
      table += `</tbody></table>`;
      body.insertAdjacentHTML("beforeend", table);
    }
  } else {
    body.insertAdjacentHTML("beforeend",
      `<div class="row"><span class="label">TFT</span><span class="muted">no log yet</span></div>`);
  }

  // ── DRL ──
  const drlText = (d.drl && d.drl.status) ? d.drl.status : "—";
  body.insertAdjacentHTML("beforeend",
    `<div class="row" style="margin-top:6px;"><span class="label">DRL</span><span class="value muted">${esc(drlText)}</span></div>`);

  // ── EPT ──
  if (d.ept && d.ept.generation !== undefined) {
    body.insertAdjacentHTML("beforeend",
      `<div class="row"><span class="label">EPT gen</span><span class="value">${esc(d.ept.generation)} (champ ${esc(d.ept.champion_id || "?")})</span></div>`);
  } else {
    body.insertAdjacentHTML("beforeend",
      `<div class="row"><span class="label">EPT</span><span class="muted">${esc((d.ept && d.ept.note) || "no generation yet")}</span></div>`);
  }

  // ── Warm-up banner (renders on the hero tile) ──
  const banner = document.getElementById("warmup-banner");
  if (banner) {
    if (d.warmup && d.warmup.message) {
      banner.style.display = "block";
      banner.textContent = "⚠ " + d.warmup.message;
    } else {
      banner.style.display = "none";
    }
  }
}

// ─── MCP ────────────────────────────────────────────────────────────
async function refreshMcp() {
  const r = await jsonFetch("/api/ops/mcp");
  const env = r.body;
  setStatus("card-mcp", env.status || "down");
  document.getElementById("mcp-age").textContent = env.error ? "amber" : "live";
  const body = document.getElementById("mcp-body");
  if (!env.data) { body.textContent = env.error || "—"; return; }
  const d = env.data;
  const okBad = d.probe.ok_for_streamable_http
    ? "<span class=\"ok\">OK</span>"
    : "<span class=\"bad\">BAD</span>";
  const lastCallStr = d.last_call
    ? `${esc(d.last_call.tool)} @ ${esc(d.last_call.ts)}`
    : "<span class=\"muted\">none in log</span>";
  const probeDetail = d.probe.via === "heartbeat"
    ? `heartbeat ${esc(d.probe.age_s ?? "?")}s ago (${esc(d.probe.content || "?")})`
    : `${esc(d.probe.code)}`;
  body.innerHTML =
    `<div class="row"><span class="label">endpoint</span><span class="value muted">${esc(d.endpoint)}</span></div>` +
    `<div class="row"><span class="label">transport</span><span class="value">${esc(d.transport)}</span></div>` +
    `<div class="row"><span class="label">probe</span><span class="value">${probeDetail} ${okBad}</span></div>` +
    `<div class="row"><span class="label">last call</span><span class="value">${lastCallStr}</span></div>`;
}

// ─── Trades + risk ──────────────────────────────────────────────────
async function refreshTrades() {
  const r = await jsonFetch("/api/ops/trades_risk");
  const env = r.body;
  setStatus("card-trades", env.status || "down");
  document.getElementById("trades-age").textContent = env.error || "live";
  const body = document.getElementById("trades-body");
  if (!env.data) { body.textContent = env.error || "—"; return; }
  const d = env.data;

  const dailyClass = (d.daily_pnl_pct || 0) > 0 ? "ok" : (d.daily_pnl_pct || 0) < 0 ? "bad" : "muted";
  const dd30 = d.drawdown_pct_30d ?? 0;
  const ddClass = dd30 < -8 ? "bad" : dd30 < -5 ? "warn" : "muted";
  const breakerHtml = d.circuit_breaker && d.circuit_breaker.active
    ? "<span class=\"bad\">ACTIVE</span>"
    : "<span class=\"ok\">clear</span>";

  let html =
    `<div style="display:grid;grid-template-columns:repeat(4, 1fr);gap:14px;">` +
      `<div><div class="muted" style="font-size:11px;">open</div>` +
      `<div style="font-size:22px;">${esc(d.open_count)}<span class="muted">/${esc(d.max_open)}</span></div></div>` +
      `<div><div class="muted" style="font-size:11px;">daily P&amp;L</div>` +
      `<div class="${dailyClass}" style="font-size:22px;">${esc(fmtUsd(d.daily_pnl_usd))} <small>${esc(fmtPct(d.daily_pnl_pct))}</small></div></div>` +
      `<div><div class="muted" style="font-size:11px;">DD 30d</div>` +
      `<div class="${ddClass}" style="font-size:22px;">${esc(fmtPct(dd30))}</div></div>` +
      `<div><div class="muted" style="font-size:11px;">breaker</div>` +
      `<div style="font-size:22px;">${breakerHtml}</div></div>` +
    `</div>`;

  if (d.open_trades && d.open_trades.length) {
    html += `<h4 style="margin:14px 0 6px;font-size:13px;">Open positions</h4>`;
    html += `<table class="tape"><thead><tr><th>pair</th><th>side</th><th>entry</th><th>P&amp;L</th><th>held</th></tr></thead><tbody>`;
    for (const t of d.open_trades.slice(0, 10)) {
      const pair = t.pair || t.trading_pair || "—";
      const side = (t.is_short ? "short" : "long");
      const entry = t.open_rate ?? t.entry_price ?? "—";
      const pnlRaw = t.profit_pct !== undefined ? t.profit_pct : (t.profit_ratio !== undefined ? t.profit_ratio * 100 : null);
      const pnl = pnlRaw !== null && pnlRaw !== undefined ? fmtPct(pnlRaw) : "—";
      const dur = t.trade_duration_s ? `${Math.floor(t.trade_duration_s/60)}m` : "—";
      html += `<tr><td>${esc(pair)}</td><td>${esc(side)}</td><td>${esc(entry)}</td><td>${esc(pnl)}</td><td>${esc(dur)}</td></tr>`;
    }
    html += `</tbody></table>`;
  } else {
    html += `<p class="muted" style="margin:14px 0 0;font-size:12px;">— no open positions —</p>`;
  }

  if (d.live_tape && d.live_tape.length) {
    html += `<h4 style="margin:14px 0 6px;font-size:13px;">Recent closes</h4>`;
    html += `<table class="tape"><thead><tr><th>pair</th><th>side</th><th>regime@entry</th><th>P&amp;L</th><th>closed</th></tr></thead><tbody>`;
    for (const t of d.live_tape) {
      const t_str = t.exit_time ? new Date(t.exit_time).toLocaleTimeString("en-US", {
        hour: "numeric", minute: "2-digit", hour12: true, timeZone: "America/New_York",
      }) + " ET" : "—";
      html += `<tr><td>${esc(t.pair)}</td><td>${esc(t.side)}</td><td class="muted">${esc(t.regime_at_entry || "—")}</td><td>${esc(fmtPct(t.pnl_pct))}</td><td class="muted">${esc(t_str)}</td></tr>`;
    }
    html += `</tbody></table>`;
  }

  body.innerHTML = html;
}

// ─── Stocks ML — TFT training status card ────────────────────────
async function refreshStocksML() {
  const r = await jsonFetch("/api/ops/stocks_ml");
  const env = r.body;
  setStatus("card-stocks-ml", env.status || "down");
  const body = document.getElementById("stocks-ml-body");
  const ageEl = document.getElementById("stocks-ml-age");
  if (!body) return;
  if (!env.data) { body.textContent = env.error || "—"; return; }
  const d = env.data;

  const isTraining = d.training_state === "running";

  // Status pill in header — show "TRAINING" prominently when a worker is active.
  if (ageEl) {
    if (isTraining) {
      const ep = d.current_epoch != null
        ? `epoch ${d.current_epoch}/${d.epochs_target || "?"}`
        : "starting up";
      ageEl.textContent = `🟡 TRAINING · ${ep} · pid ${d.training_pid}`;
      ageEl.style.color = "#f4b942";
    } else {
      const enabled = d.ml_enabled ? "🟢 INFLUENCING TRADES" : "⚪ COMPUTE ONLY";
      const age = d.weights_age_seconds == null
        ? "no model yet"
        : d.weights_age_seconds < 86400
          ? `${Math.floor(d.weights_age_seconds/3600)}h old`
          : `${Math.floor(d.weights_age_seconds/86400)}d old`;
      ageEl.textContent = `${enabled} · model ${age}${d.ml_alpha ? " · ALPHA" : ""}`;
      ageEl.style.color = d.ml_enabled ? "#3fb950" : "var(--text-muted)";
    }
  }

  // Live training progress banner — only while a worker is mid-flight.
  let html = "";
  if (isTraining) {
    const ep = d.current_epoch != null ? `${d.current_epoch}/${d.epochs_target || "?"}` : "—";
    const progressPct = (d.current_epoch && d.epochs_target)
      ? Math.min(100, Math.round(100 * d.current_epoch / d.epochs_target))
      : 0;
    const loss = d.current_loss != null ? d.current_loss.toFixed(4) : "—";
    const valAcc = d.current_val_acc != null ? (d.current_val_acc * 100).toFixed(1) + "%" : "—";
    const elapsed = d.training_started_at
      ? Math.max(0, Math.floor((Date.now() - new Date(d.training_started_at).getTime()) / 1000))
      : null;
    const elapsedStr = elapsed != null
      ? (elapsed < 60 ? `${elapsed}s` : `${Math.floor(elapsed/60)}m ${elapsed%60}s`)
      : "—";
    html += `<div style="margin-bottom:14px;padding:12px;background:rgba(244,185,66,0.08);border:1px solid rgba(244,185,66,0.35);border-radius:6px;">` +
      `<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;">` +
      `<span style="font-weight:600;color:#f4b942;">⚙ TRAINING IN PROGRESS</span>` +
      `<span class="muted" style="font-size:11px;font-family:var(--mono);">elapsed ${elapsedStr}</span>` +
      `</div>` +
      `<div style="height:6px;background:var(--bg-inset);border-radius:3px;overflow:hidden;margin-bottom:10px;">` +
      `<div style="height:100%;width:${progressPct}%;background:#f4b942;transition:width 0.5s ease;"></div>` +
      `</div>` +
      `<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;font-family:var(--mono);font-size:12px;">` +
      `<div><div class="muted" style="font-size:10px;">EPOCH</div><div style="font-size:16px;">${esc(ep)}</div></div>` +
      `<div><div class="muted" style="font-size:10px;">LOSS</div><div style="font-size:16px;">${esc(loss)}</div></div>` +
      `<div><div class="muted" style="font-size:10px;">VAL ACC</div><div style="font-size:16px;color:#3fb950;">${esc(valAcc)}</div></div>` +
      `<div><div class="muted" style="font-size:10px;">PID</div><div style="font-size:16px;">${esc(d.training_pid || "—")}</div></div>` +
      `</div></div>`;
  }

  // 4-up KPI: model present, val acc, training samples, next train
  const ok = d.weights_present && d.best_val_acc != null;
  const valAcc = d.best_val_acc != null ? (d.best_val_acc * 100).toFixed(1) + "%" : "—";
  const baseline = "33.3%"; // 3-class random baseline
  const valColor = ok && d.best_val_acc > 0.40 ? "#3fb950" : ok && d.best_val_acc > 0.36 ? "#f4b942" : "#f85149";

  html += `<div class="ks-grid">` +
    `<div><div class="kpi-label">Model</div>` +
    `<div class="kpi-value">${ok ? "stock_tft_v1" : "<span class=\"muted\">not trained yet</span>"}</div>` +
    `<div class="kpi-sub">device: ${esc(d.device || "cpu")} · best ep ${esc(d.best_epoch || "—")}</div></div>` +

    `<div><div class="kpi-label">Validation accuracy</div>` +
    `<div class="kpi-value" style="color:${valColor};">${esc(valAcc)}</div>` +
    `<div class="kpi-sub">3-class random = ${baseline} · target ≥45%</div></div>` +

    `<div><div class="kpi-label">Train / val samples</div>` +
    `<div class="kpi-value">${esc(d.n_train ?? "—")}</div>` +
    `<div class="kpi-sub">val ${esc(d.n_val ?? "—")} · ${esc(d.n_tickers ?? 0)} tickers</div></div>` +

    `<div><div class="kpi-label">Next training</div>` +
    `<div class="kpi-value" style="font-size:14px;">${esc(d.next_train_cron || "—")}</div>` +
    `<div class="kpi-sub">cron: stocks_ml_train</div></div>` +
    `</div>`;

  // Training history (sparkline-ish)
  if (d.history && d.history.length) {
    html += `<h4 style="margin:14px 0 6px;font-size:13px;">Recent epochs</h4>`;
    html += `<table class="tape"><thead><tr><th>epoch</th><th>train loss</th><th>val acc</th><th>elapsed</th></tr></thead><tbody>`;
    for (const h of d.history) {
      html += `<tr><td>${esc(h.epoch)}</td><td>${esc(h.train_loss)}</td><td>${esc((h.val_acc * 100).toFixed(1))}%</td><td>${esc(h.elapsed_s)}s</td></tr>`;
    }
    html += `</tbody></table>`;
  }

  // EPT generation log
  if (d.evolution && d.evolution.length) {
    html += `<h4 style="margin:14px 0 6px;font-size:13px;">Evolution generations</h4>`;
    html += `<table class="tape"><thead><tr><th>gen</th><th>week ending</th><th>champion</th><th>members</th></tr></thead><tbody>`;
    for (const g of d.evolution) {
      html += `<tr><td>${esc(g.generation)}</td><td>${esc(g.week_ending)}</td><td>${esc(g.champion_id || "—")}</td><td>${esc((g.members||[]).length)}</td></tr>`;
    }
    html += `</tbody></table>`;
  }

  // Log tail (collapsed by default)
  if (d.log_tail && d.log_tail.length) {
    html += `<details style="margin-top:14px;"><summary class="muted" style="cursor:pointer;font-size:11px;">last training run · log tail (${d.log_tail.length} lines)</summary>` +
            `<pre style="background:var(--bg-inset);padding:10px 12px;font-size:11px;overflow-x:auto;border-radius:6px;margin-top:6px;">${esc(d.log_tail.join("\n"))}</pre>` +
            `</details>`;
  }

  body.innerHTML = html;
}

// ─── LLM circuit breakers (Ollama primary + Anthropic fallback) ──
async function refreshCircuitBreakers() {
  const [cbResp, ohResp] = await Promise.all([
    jsonFetch("/api/ops/circuit_breakers"),
    jsonFetch("/api/ops/ollama_health"),
  ]);
  const cbEnv = cbResp.body;
  const ohEnv = ohResp.body;
  setStatus("card-cb", cbEnv.status || "down");
  const ageEl = document.getElementById("cb-age");
  const body = document.getElementById("cb-body");
  if (!body) return;

  const breakers = (cbEnv.data && cbEnv.data.breakers) || [];
  const summary = (cbEnv.data && cbEnv.data.summary) || {};
  const oh = (ohEnv.data) || null;

  // Header line
  if (ageEl) {
    const ohState = oh
      ? (oh.healthy ? `Ollama OK · ${oh.last_probe_latency_s ?? "—"}s probe`
                    : `Ollama UNHEALTHY · ${oh.error || ""}`.slice(0, 60))
      : "no Ollama health data yet";
    const fb = summary.any_failover_active ? "  ·  ⚠ FAILOVER ACTIVE" : "";
    ageEl.textContent = `${ohState}${fb}`;
    ageEl.style.color = summary.any_failover_active ? "var(--down)" : "";
  }

  // Wipe + render
  while (body.firstChild) body.removeChild(body.firstChild);

  // 1. Ollama health summary (if we have it)
  if (oh) {
    const healthBlock = document.createElement("div");
    healthBlock.style.cssText =
      "background:var(--bg-inset);border:1px solid var(--border-subtle);" +
      "border-radius:var(--radius-card);padding:14px 16px;margin-bottom:14px;";
    const hue = oh.healthy ? "#3fb950" : "var(--down)";
    healthBlock.innerHTML = `
      <div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em;">Ollama probe</div>
      <div style="display:flex;gap:24px;align-items:baseline;margin-top:6px;flex-wrap:wrap;">
        <div><span style="color:${hue};font-size:18px;font-weight:600;">${oh.healthy ? "● HEALTHY" : "● UNHEALTHY"}</span></div>
        <div class="muted" style="font-size:11px;font-family:var(--mono);">probe ${oh.last_probe_latency_s ?? "—"}s · ${(oh.models_available || []).length} models loaded · ${oh.consecutive_failures} consecutive failures</div>
      </div>${oh.models_missing && oh.models_missing.length ? `<div class="bad" style="font-size:11px;margin-top:6px;">missing: ${oh.models_missing.join(", ")}</div>` : ""}
      ${oh.error ? `<div class="bad" style="font-size:11px;margin-top:6px;">${esc(String(oh.error))}</div>` : ""}
    `;
    body.appendChild(healthBlock);
  }

  // 2. Circuit-breakers grid
  if (!breakers.length) {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.style.cssText = "padding: 12px; font-size: 12px;";
    empty.textContent = "— no breakers yet — they spawn on the first shark LLM call —";
    body.appendChild(empty);
    return;
  }

  const grid = document.createElement("div");
  grid.className = "cb-grid";
  // Sort: Ollama first, then Anthropic; fast then deep within each
  const sorted = breakers.slice().sort((a, b) => {
    const aa = (a.name || "").toLowerCase();
    const bb = (b.name || "").toLowerCase();
    return aa.localeCompare(bb);
  });
  for (const b of sorted) {
    const card = document.createElement("div");
    card.className = "cb-card";
    card.dataset.state = b.state || "closed";
    const inSec = b.in_state_seconds || 0;
    const inStr = inSec < 60 ? `${inSec}s` : inSec < 3600 ? `${Math.floor(inSec/60)}m` : `${Math.floor(inSec/3600)}h`;
    card.innerHTML = `
      <div class="cb-name">${esc(b.name || "?")} · ${esc(b.tier || "?")}</div>
      <div class="cb-state">${(b.state || "closed").toUpperCase().replace("_", " ")}</div>
      <div class="cb-meta">
        in state ${esc(inStr)} · ${esc(b.failure_count || 0)} failures<br>
        p50 ${b.p50_latency_s ?? "—"}s · p95 ${b.p95_latency_s ?? "—"}s · threshold ${b.threshold_s ?? "—"}s<br>
        ${esc(b.samples_in_window || 0)} samples in last 60s
      </div>
    `;
    grid.appendChild(card);
  }
  body.appendChild(grid);
}

// ─── Combined portfolio (crypto + stocks unified view) ───────────
function fmtUsdAbs(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return "$" + Math.abs(Number(n)).toLocaleString("en-US", {
    maximumFractionDigits: 0, minimumFractionDigits: 0,
  });
}
function fmtPctSigned(n) {
  if (n === null || n === undefined) return "—";
  const v = Number(n);
  return (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(2) + "%";
}
async function refreshCombined() {
  const r = await jsonFetch("/api/ops/combined_portfolio");
  const env = r.body;
  setStatus("card-combined", env.status || "down");
  const ageEl = document.getElementById("combined-age");
  const body = document.getElementById("combined-body");
  if (!body) return;
  if (!env.data) {
    body.textContent = env.error || "—";
    if (ageEl) ageEl.textContent = env.error || "—";
    return;
  }
  const d = env.data;
  if (ageEl) {
    const breaker = d.circuit_breaker_active ? " · BREAKER TRIPPED" : "";
    ageEl.textContent = `${d.combined_open_positions} open${breaker}`;
    ageEl.style.color = d.circuit_breaker_active ? "#f85149" : "";
  }

  // 4-up KPI grid: crypto / stocks / total / drawdown
  const ddPct = d.combined_drawdown_pct;
  const thresh = d.threshold_pct;
  const ddClass = d.circuit_breaker_active ? "is-neg"
    : (ddPct > thresh * 0.7 ? "is-neg" : ddPct > 0.01 ? "" : "is-pos");
  const ddBarPct = Math.min(100, (ddPct / thresh) * 100);

  let html = `<div class="ks-grid">` +
    `<div><div class="kpi-label">Crypto equity · freqtrade</div>` +
    `<div class="kpi-value">${esc(fmtUsdAbs(d.crypto_equity))}</div>` +
    `<div class="kpi-sub">${esc(d.crypto_open_positions)} open · realised ${esc(fmtPctSigned(d.crypto_drawdown_pct))} DD</div></div>` +

    `<div><div class="kpi-label">Stocks equity · Alpaca</div>` +
    `<div class="kpi-value">${esc(fmtUsdAbs(d.stocks_equity))}</div>` +
    `<div class="kpi-sub">${esc(d.stocks_open_positions)} open · ${d.sources && d.sources.stocks_paper ? "PAPER" : "LIVE"}</div></div>` +

    `<div><div class="kpi-label">Total equity · combined</div>` +
    `<div class="kpi-value">${esc(fmtUsdAbs(d.total_equity))}</div>` +
    `<div class="kpi-sub">peak ${esc(fmtUsdAbs(d.combined_peak_equity))}</div></div>` +

    `<div><div class="kpi-label">Combined drawdown</div>` +
    `<div class="kpi-value ${ddClass}">${esc(fmtPctSigned(-ddPct))}</div>` +
    `<div class="kpi-sub">threshold ${esc(thresh.toFixed(1))}% · ${d.circuit_breaker_active ? "BREAKER ACTIVE" : "clear"}</div></div>` +
    `</div>`;

  // Drawdown bar — visualises distance to the 10% trip threshold
  html += `<div class="dd-bar"><i style="width:${ddBarPct.toFixed(1)}%;"></i></div>` +
    `<div class="muted" style="font-size:11px; display:flex; justify-content:space-between;">` +
    `<span>0%</span><span>${esc(ddPct.toFixed(2))}% of ${esc(thresh.toFixed(1))}% threshold</span><span>tripped</span></div>`;

  // Source breakdown — small print
  const s = d.sources || {};
  const snapAge = d.snapshot_age_seconds;
  const snapAgeStr = snapAge == null ? "—"
    : snapAge < 60 ? `${snapAge}s ago`
    : snapAge < 3600 ? `${Math.floor(snapAge/60)}m ago`
    : `${Math.floor(snapAge/3600)}h ago`;
  html += `<p class="muted" style="margin:14px 0 0;font-size:11px;font-family:var(--mono);">` +
    `crypto: starting=${esc(fmtUsdAbs(s.crypto_starting_equity))} realised=${esc(fmtUsdAbs(s.crypto_realised_pnl))} unrealised=${esc(fmtUsdAbs(s.crypto_unrealised_pnl))} · ` +
    `stocks snapshot ${esc(snapAgeStr)}</p>`;

  body.innerHTML = html;
}

// ─── LLM inference monitor (cost saved vs Anthropic API) ──────────
async function refreshLLMStats() {
  const r = await jsonFetch("/api/ops/llm_stats");
  const env = r.body;
  setStatus("card-llm", env.status || "down");
  const ageEl = document.getElementById("llm-age");
  const body = document.getElementById("llm-body");
  if (!body) return;
  if (!env.data) {
    body.textContent = env.error || "—";
    return;
  }
  const d = env.data;
  const shark = d.shark || {};
  const crypto = d.crypto || {};
  const provider = d.provider || "ollama";
  const isLocal = d.is_local;

  if (ageEl) {
    ageEl.textContent = `provider: ${provider} · ${shark.total_calls || 0} shark calls 24h · ${crypto.calls_24h || 0} crypto calls 24h`;
  }

  const providerBadge = isLocal
    ? `<span class="pill-local">● LOCAL · ZERO COST</span>`
    : `<span class="pill-paid">● PAID API</span>`;
  const totalSaved = Number(shark.total_api_cost_saved_usd || 0);
  const monthlyProj = totalSaved * 30;

  let html = `<div class="ks-grid">` +
    `<div><div class="kpi-label">Provider</div>` +
    `<div class="kpi-value" style="font-size:14px;">${providerBadge}</div>` +
    `<div class="kpi-sub">model: ${esc(Object.keys(shark.by_model || {})[0] || "—")}</div></div>` +

    `<div><div class="kpi-label">Calls · 24h (shark)</div>` +
    `<div class="kpi-value">${esc(shark.total_calls || 0)}</div>` +
    `<div class="kpi-sub">avg ${esc((shark.avg_latency_seconds || 0).toFixed(1))}s · ${esc(shark.by_tier?.fast || 0)} fast / ${esc(shark.by_tier?.deep || 0)} deep</div></div>` +

    `<div><div class="kpi-label">Tokens · 24h (shark)</div>` +
    `<div class="kpi-value">${esc(((shark.total_prompt_tokens||0) + (shark.total_completion_tokens||0)).toLocaleString())}</div>` +
    `<div class="kpi-sub">${esc((shark.total_prompt_tokens||0).toLocaleString())} in · ${esc((shark.total_completion_tokens||0).toLocaleString())} out</div></div>` +

    `<div><div class="kpi-label">Cost saved · 24h</div>` +
    `<div class="kpi-value is-pos">+$${esc(totalSaved.toFixed(2))}</div>` +
    `<div class="kpi-sub">~$${esc(monthlyProj.toFixed(0))}/mo at this rate</div></div>` +
    `</div>`;

  // Per-agent breakdown
  const agents = shark.by_agent || {};
  const agentRows = Object.entries(agents).sort((a, b) => (b[1].calls||0) - (a[1].calls||0));
  if (agentRows.length) {
    html += `<h4 style="margin:14px 0 6px;font-size:13px;">Per-agent breakdown</h4>`;
    html += `<table class="tape"><thead><tr><th>agent</th><th>model(s)</th><th>calls</th><th>avg latency</th><th>max latency</th><th>tokens</th></tr></thead><tbody>`;
    // Find the max latency for the bar visual
    const maxLat = Math.max(1, ...agentRows.map(([,v]) => v.max_latency || 0));
    for (const [agent, info] of agentRows) {
      const barPct = ((info.avg_latency || 0) / maxLat * 100).toFixed(0);
      html += `<tr><td>${esc(agent)}</td>` +
        `<td class="muted" style="font-family:var(--mono);font-size:11px;">${esc((info.models||[]).join(", "))}</td>` +
        `<td>${esc(info.calls)}</td>` +
        `<td><span class="lat-bar" style="width:80px;"><i style="width:${barPct}%;"></i></span> ${esc(info.avg_latency)}s</td>` +
        `<td>${esc(info.max_latency)}s</td>` +
        `<td>${esc((info.total_tokens || 0).toLocaleString())}</td></tr>`;
    }
    html += `</tbody></table>`;
  } else {
    html += `<p class="muted" style="margin:8px 0 0;font-size:12px;">— no shark calls in last 24h — fire a shark phase to populate this card —</p>`;
  }

  // Crypto sentiment row (separate — sentiment_log has different shape)
  html += `<h4 style="margin:14px 0 6px;font-size:13px;">Crypto sentiment engine · 24h</h4>`;
  html += `<table class="tape"><thead><tr><th>source</th><th>calls</th><th>note</th></tr></thead><tbody>`;
  html += `<tr><td>sentiment_log</td><td>${esc(crypto.calls_24h || 0)}</td>` +
    `<td class="muted" style="font-size:11px;">${esc(crypto.latency_note || "")}</td></tr>`;
  html += `</tbody></table>`;

  body.innerHTML = html;
}

// ─── Entry gates matrix (per-pair × per-gate green/red grid) ──────
function renderGatesSection(host, kind, rows, title) {
  // host: DOM node, kind: "crypto"|"stocks", rows: [{pair, gates: [{gate, pass, detail}], ...}]
  if (!rows || !rows.length) {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.style.cssText = "padding: 12px; font-size: 12px;";
    empty.textContent = `${title}: no pairs configured`;
    host.appendChild(empty);
    return;
  }
  const section = document.createElement("div");
  section.className = "gates-section";
  section.dataset.kind = kind;

  const head = document.createElement("div");
  head.className = "gates-section-head";
  head.textContent = title;
  section.appendChild(head);

  // Build column list from the first row's gate names so we get consistent order
  const gateNames = rows[0].gates.map(g => g.gate);

  const table = document.createElement("table");
  table.className = "gates-table";
  table.dataset.kind = kind;

  // Header
  const thead = document.createElement("thead");
  const trh = document.createElement("tr");
  const cols = ["pair", "regime"].concat(gateNames).concat(["status"]);
  for (const c of cols) {
    const th = document.createElement("th");
    if (gateNames.includes(c)) th.className = "gate-col";
    th.textContent = c.replace(/_/g, " ");
    trh.appendChild(th);
  }
  thead.appendChild(trh);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.style.cursor = "pointer";

    const tdPair = document.createElement("td");
    tdPair.className = "pair";
    tdPair.textContent = row.pair;
    tr.appendChild(tdPair);

    const tdReg = document.createElement("td");
    tdReg.className = "regime";
    tdReg.textContent = row.regime || "—";
    tr.appendChild(tdReg);

    for (const gname of gateNames) {
      const g = row.gates.find(x => x.gate === gname);
      const td = document.createElement("td");
      td.style.textAlign = "center";
      // Use the new GateBadge primitive — proper PASS/BLOCK/N/A chips.
      const passState = g?.pass === true ? "pass" : g?.pass === false ? "block" : "na";
      const label = g?.pass === true ? "✓" : g?.pass === false ? "✗" : "—";
      const badge = window.QC && window.QC.gateBadge
        ? window.QC.gateBadge(passState, label)
        : (function () {
            const sp = document.createElement("span");
            sp.className = "gate " + passState;
            sp.textContent = label;
            return sp;
          })();
      badge.title = g?.detail || "";
      td.appendChild(badge);
      tr.appendChild(td);
    }

    const tdSum = document.createElement("td");
    tdSum.className = "summary";
    if (row.n_blocking === 0) {
      tdSum.textContent = "all pass · ready";
      tdSum.style.color = "#3fb950";
    } else {
      tdSum.textContent = `${row.n_blocking}/${row.n_gates} blocking`;
      tdSum.style.color = "#f85149";
    }
    tr.appendChild(tdSum);

    // Detail row (revealed by tooltip via title attrs; also pre-rendered below the row)
    const detailTr = document.createElement("tr");
    detailTr.className = "gates-detail-row";
    const detailTd = document.createElement("td");
    detailTd.colSpan = cols.length;
    if (row.n_blocking > 0) {
      const blockers = row.gates.filter(g => g.pass === false);
      const lines = blockers.map(b => `${b.gate}: ${b.detail}`);
      const span = document.createElement("span");
      span.className = "blocker";
      span.textContent = "blocking → ";
      detailTd.appendChild(span);
      detailTd.appendChild(document.createTextNode(lines.join(" · ")));
    } else {
      detailTd.style.color = "#3fb950";
      detailTd.textContent = "all gates pass — bot will fire on the next valid candle";
    }
    detailTr.appendChild(detailTd);

    tbody.appendChild(tr);
    tbody.appendChild(detailTr);
  }
  table.appendChild(tbody);
  section.appendChild(table);
  host.appendChild(section);
}

async function refreshGates() {
  const r = await jsonFetch("/api/ops/gates");
  const env = r.body;
  setStatus("card-gates", env.status || "down");
  const ageEl = document.getElementById("gates-age");
  const body = document.getElementById("gates-body");
  if (!body) return;
  if (!env.data) { body.textContent = env.error || "—"; return; }

  const acct = env.data.account || {};
  if (ageEl) {
    const breakerStr = acct.breaker_active ? " · BREAKER" : "";
    const paperStr = acct.paper === false ? "LIVE" : "PAPER";
    ageEl.textContent = `${acct.open_count ?? 0}/${acct.max_open ?? 6} open · ${paperStr}${breakerStr}`;
  }

  // Wipe + rerender
  while (body.firstChild) body.removeChild(body.firstChild);

  renderGatesSection(body, "crypto", env.data.crypto || [],
    "Crypto · freqtrade FreqAI gates");
  renderGatesSection(body, "stocks", env.data.stocks || [],
    "Stocks · wheel CSP rules");
}

// ─── Live trades hero strip (full-width, top of page) ─────────────
async function refreshLiveTrades() {
  const r = await jsonFetch("/api/ops/live_trades");
  const env = r.body;
  const host = document.getElementById("lt-tracks");
  const counter = document.getElementById("lt-counter");
  if (!host || !env || !env.data) return;
  const trades = env.data.trades || [];
  const summary = env.data.summary || {};

  if (counter) {
    counter.textContent = `${summary.crypto_active ?? 0} crypto · ${summary.wheel_active ?? 0} wheel · ${summary.alpaca_paper === false ? "LIVE" : "PAPER"}`;
  }

  if (!trades.length) {
    host.innerHTML = `<div class="lt-empty">— no active trades right now — bot is running, gates are blocking new entries —</div>`;
    return;
  }

  const fragments = trades.map((t) => {
    const pnlPct = t.pnl_pct;
    const pnlDir = pnlPct == null ? "" : pnlPct > 0 ? "up" : pnlPct < 0 ? "down" : "";
    let detail;
    if (t.kind === "wheel") {
      const credit = t.pnl_usd != null ? `$${Number(t.pnl_usd).toFixed(0)} credit` : "";
      const subkind = t.subkind === "short_put" ? "CSP"
        : t.subkind === "short_call" ? "CC"
        : t.subkind === "long_shares" ? "shares"
        : t.subkind;
      detail = `${subkind} $${Number(t.entry || 0).toFixed(2)} · ${credit} · ${esc(t.extra || "")}`;
    } else {
      const dur = t.duration_s ? `${Math.floor(t.duration_s / 60)}m` : "—";
      const pnlStr = pnlPct == null ? "—" : `${pnlPct >= 0 ? "+" : "−"}${Math.abs(pnlPct).toFixed(2)}%`;
      const pnlUsdStr = t.pnl_usd != null ? `$${Number(t.pnl_usd).toFixed(2)}` : "";
      detail = `${esc(t.subkind)} @${esc(t.entry)} · ${pnlStr} ${pnlUsdStr} · held ${dur}`;
    }
    return `<div class="lt-pill" data-kind="${esc(t.kind)}" data-pnl="${pnlDir}">` +
      `<span class="lt-dot"></span>` +
      `<span class="lt-label">${esc(t.label)}</span>` +
      `<span class="lt-detail">${detail}</span>` +
      `</div>`;
  });
  host.innerHTML = fragments.join("");
}

// ─── Stocks regime (SPY 50/200 MA classifier) ─────────────────────
const STOCK_REGIME_LABELS = {
  trending_up:     "↗ trending up",
  trending_down:   "↘ trending down",
  mean_reverting:  "↔ mean-reverting",
  high_volatility: "⚡ high vol",
};
const STOCK_REGIME_COLORS = {
  trending_up:     "#3fb950",
  trending_down:   "#f85149",
  mean_reverting:  "#9ab8ff",
  high_volatility: "#f4b942",
};
async function refreshStockRegime() {
  const r = await jsonFetch("/api/ops/stock_regime");
  const env = r.body;
  const head = document.getElementById("hero-stock-regime");
  const meta = document.getElementById("hero-stock-meta");
  const slot = document.getElementById("hero-stock-slot");
  if (!head || !env) return;
  if (!env.data || env.status === "down") {
    head.textContent = "—";
    if (meta) meta.textContent = env.error || "no SPY data";
    return;
  }
  const d = env.data;
  const reg = d.current || "—";
  head.textContent = STOCK_REGIME_LABELS[reg] || reg;
  head.style.color = STOCK_REGIME_COLORS[reg] || "var(--text-primary)";
  if (slot) slot.dataset.regime = reg;
  if (meta) {
    const conf = d.probability != null ? `${(d.probability * 100).toFixed(0)}%` : "—";
    const r5 = d.return_5d_pct != null ? `${d.return_5d_pct >= 0 ? "+" : ""}${d.return_5d_pct}%` : "—";
    meta.textContent = `SPY $${d.spot} · 50d $${d.ma_50} · 200d $${d.ma_200} · 5d ${r5} · conf ${conf}`;
  }
}

// ─── Stocks (shark momentum + wheel income on Alpaca) ─────────────
function fmtUsdPlain(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return "$" + Number(n).toLocaleString("en-US", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
}

function fmtAge(seconds) {
  if (seconds === null || seconds === undefined) return "no data";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

async function refreshStocks() {
  const [r, mhResp] = await Promise.all([
    jsonFetch("/api/ops/stocks"),
    jsonFetch("/api/ops/market_hours"),
  ]);
  const env = r.body;
  const mh = mhResp.body && mhResp.body.data;
  setStatus("card-stocks", env.status || "down");
  const ageEl = document.getElementById("stocks-age");
  if (ageEl) {
    const reopen = mh && mh.next_open_utc
      ? new Date(mh.next_open_utc).toLocaleString("en-US", {
          weekday: "short", hour: "numeric", minute: "2-digit",
          hour12: true, timeZone: "America/New_York",
        }) + " ET"
      : null;
    let mhBadge;
    if (!mh) mhBadge = "";
    else if (mh.is_open) mhBadge = "● NYSE open";
    else if (mh.is_extended) mhBadge = "● Extended hours";
    else mhBadge = `🔒 closed · reopens ${reopen}`;
    ageEl.textContent = (env.error || "live") + (mhBadge ? "  ·  " + mhBadge : "");
  }
  const body = document.getElementById("stocks-body");
  if (!body) return;
  if (!env.data) { body.textContent = env.error || "—"; return; }

  const { alpaca = {}, wheel = {}, shark = {} } = env.data;

  // ── Top KPIs: alpaca + wheel premium ────────────────────────────
  const pnlClass = (wheel.cumulative_pnl_usd || 0) > 0 ? "ok"
    : (wheel.cumulative_pnl_usd || 0) < 0 ? "bad" : "muted";
  const paperBadge = alpaca.paper === false
    ? `<span style="background:#3b1e1f;color:#ff9a9a;font-size:10px;padding:2px 6px;border-radius:3px;margin-left:8px;font-weight:600;">LIVE</span>`
    : `<span style="background:#1a2640;color:#9ab8ff;font-size:10px;padding:2px 6px;border-radius:3px;margin-left:8px;font-weight:600;">PAPER</span>`;
  const ageNote = alpaca.age_seconds === null
    ? `<span class="muted" style="font-size:10px;">no snapshot — run \`wheel snapshot\`</span>`
    : `<span class="muted" style="font-size:10px;">snapshot ${esc(fmtAge(alpaca.age_seconds))}</span>`;

  let html = `<div style="display:grid;grid-template-columns:repeat(4, 1fr);gap:14px;">` +
    `<div><div class="muted" style="font-size:11px;">alpaca cash ${paperBadge}</div>` +
    `<div style="font-size:22px;font-variant-numeric:tabular-nums;">${esc(fmtUsdPlain(alpaca.cash))}</div></div>` +
    `<div><div class="muted" style="font-size:11px;">buying power</div>` +
    `<div style="font-size:22px;font-variant-numeric:tabular-nums;">${esc(fmtUsdPlain(alpaca.buying_power))}</div></div>` +
    `<div><div class="muted" style="font-size:11px;">portfolio value</div>` +
    `<div style="font-size:22px;font-variant-numeric:tabular-nums;">${esc(fmtUsdPlain(alpaca.portfolio_value))}</div></div>` +
    `<div><div class="muted" style="font-size:11px;">wheel premium captured</div>` +
    `<div class="${pnlClass}" style="font-size:22px;font-variant-numeric:tabular-nums;">${esc(fmtUsd(wheel.cumulative_pnl_usd))}</div></div>` +
    `</div>` +
    `<div style="margin-top:6px;text-align:right;">${ageNote}</div>`;

  // ── Wheel: open positions table ──────────────────────────────────
  html += `<h4 style="margin:14px 0 6px;font-size:13px;">Wheel positions <span class="muted" style="font-weight:400;font-size:11px;">— cash-secured puts &amp; covered calls on Alpaca</span></h4>`;
  const positions = wheel.open_positions || [];
  if (positions.length) {
    html += `<table class="tape"><thead><tr><th>kind</th><th>underlying</th><th>strike</th><th>expiry</th><th>qty</th><th>credit</th><th>contract</th></tr></thead><tbody>`;
    for (const p of positions) {
      const kindLabel = p.kind === "short_put" ? "short put"
        : p.kind === "short_call" ? "short call"
        : p.kind === "long_shares" ? "shares"
        : (p.kind || "—");
      html += `<tr><td>${esc(kindLabel)}</td><td>${esc(p.underlying)}</td>` +
        `<td>${esc(p.strike != null ? "$" + Number(p.strike).toFixed(2) : "—")}</td>` +
        `<td>${esc(p.expiry || "—")}</td>` +
        `<td>${esc(p.qty ?? "—")}</td>` +
        `<td>${esc(fmtUsdPlain(p.entry_credit))}</td>` +
        `<td class="muted" style="font-family:var(--mono);">${esc(p.contract || "—")}</td></tr>`;
    }
    html += `</tbody></table>`;
  } else {
    html += `<p class="muted" style="margin:8px 0 0;font-size:12px;">— no open wheel positions — Friday cron sells the next CSP cycle —</p>`;
  }

  // ── Shark: momentum bot strip ────────────────────────────────────
  const sharkMode = (shark.mode || "—").toLowerCase();
  const modeBadgeColor = sharkMode === "live" ? "#ff9a9a" : "#9ab8ff";
  const cbTone = shark.circuit_breaker
    ? `<span class="bad">TRIPPED</span>`
    : `<span class="ok">clear</span>`;
  const ksTone = shark.kill_switch_active
    ? `<span class="bad">ACTIVE</span>`
    : `<span class="ok">clear</span>`;
  const sharkAge = shark.age_seconds == null
    ? "—"
    : fmtAge(shark.age_seconds);
  const stats = shark.stats || {};
  const winsLosses = `${esc(stats.wins ?? 0)}W / ${esc(stats.losses ?? 0)}L`;
  const winRate = stats.win_rate != null ? Number(stats.win_rate).toFixed(0) + "%" : "—";

  html += `<h4 style="margin:14px 0 6px;font-size:13px;">Shark momentum bot <span class="muted" style="font-weight:400;font-size:11px;">— S&amp;P-beating stocks · NO options</span></h4>`;
  html += `<div style="display:grid;grid-template-columns:repeat(4, 1fr);gap:14px;">` +
    `<div><div class="muted" style="font-size:11px;">mode</div>` +
    `<div style="font-size:18px;color:${modeBadgeColor};text-transform:uppercase;letter-spacing:0.04em;">${esc(sharkMode)}</div></div>` +
    `<div><div class="muted" style="font-size:11px;">peak equity</div>` +
    `<div style="font-size:18px;font-variant-numeric:tabular-nums;">${esc(fmtUsdPlain(shark.peak_equity))}</div></div>` +
    `<div><div class="muted" style="font-size:11px;">trades this week</div>` +
    `<div style="font-size:18px;font-variant-numeric:tabular-nums;">${esc(shark.weekly_trade_count ?? 0)}<span class="muted">/3</span></div></div>` +
    `<div><div class="muted" style="font-size:11px;">circuit breaker</div>` +
    `<div style="font-size:18px;">${cbTone}</div></div>` +
    `</div>`;

  html += `<div style="display:grid;grid-template-columns:repeat(4, 1fr);gap:14px;margin-top:12px;">` +
    `<div><div class="muted" style="font-size:11px;">kill switch</div>` +
    `<div style="font-size:14px;">${ksTone}</div></div>` +
    `<div><div class="muted" style="font-size:11px;">open shark trades</div>` +
    `<div style="font-size:14px;font-variant-numeric:tabular-nums;">${esc((shark.open_trades || []).length)}<span class="muted">/6</span></div></div>` +
    `<div><div class="muted" style="font-size:11px;">total P&amp;L · realized</div>` +
    `<div style="font-size:14px;font-variant-numeric:tabular-nums;">${esc(fmtUsd(stats.total_pnl))}</div></div>` +
    `<div><div class="muted" style="font-size:11px;">wins · win-rate</div>` +
    `<div style="font-size:14px;font-variant-numeric:tabular-nums;">${esc(winsLosses)} · ${esc(winRate)}</div></div>` +
    `</div>`;

  if (shark.open_trades && shark.open_trades.length) {
    html += `<table class="tape" style="margin-top:10px;"><thead><tr><th>symbol</th><th>side</th><th>entry</th><th>qty</th><th>stop</th><th>opened</th></tr></thead><tbody>`;
    for (const t of shark.open_trades.slice(0, 6)) {
      html += `<tr><td>${esc(t.symbol || t.ticker || "—")}</td>` +
        `<td>${esc(t.side || "long")}</td>` +
        `<td>${esc(t.entry_price != null ? "$" + Number(t.entry_price).toFixed(2) : "—")}</td>` +
        `<td>${esc(t.qty ?? "—")}</td>` +
        `<td>${esc(t.stop_price != null ? "$" + Number(t.stop_price).toFixed(2) : "—")}</td>` +
        `<td class="muted" style="font-size:11px;">${esc(t.opened_at || t.entry_time || "—")}</td></tr>`;
    }
    html += `</tbody></table>`;
  }

  html += `<p class="muted" style="margin:14px 0 0;font-size:11px;">shark snapshot ${esc(sharkAge)} · ${esc(shark.generated_at || "—")}</p>`;

  body.innerHTML = html;
}

// ─── Sparklines (per-pair tiny price chart on canvas) ──────────────
function drawSparkline(canvas, closes, color = "#4cc38a", colorDown = "#ff6b6b") {
  if (!canvas || !closes || closes.length < 2) return;
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  if (canvas.width !== W * dpr) { canvas.width = W * dpr; canvas.height = H * dpr; }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const lo = Math.min(...closes), hi = Math.max(...closes);
  const range = (hi - lo) || 1;
  const x = i => (i / (closes.length - 1)) * (W - 2) + 1;
  const y = v => H - 1 - ((v - lo) / range) * (H - 2);

  // Determine direction by last vs first
  const up = closes[closes.length - 1] >= closes[0];
  const stroke = up ? color : colorDown;

  // Filled area
  ctx.beginPath();
  ctx.moveTo(x(0), y(closes[0]));
  for (let i = 1; i < closes.length; i++) ctx.lineTo(x(i), y(closes[i]));
  ctx.lineTo(x(closes.length - 1), H); ctx.lineTo(x(0), H); ctx.closePath();
  ctx.fillStyle = stroke + "33";  // 20% alpha
  ctx.fill();

  // Stroke line
  ctx.beginPath();
  ctx.moveTo(x(0), y(closes[0]));
  for (let i = 1; i < closes.length; i++) ctx.lineTo(x(i), y(closes[i]));
  ctx.strokeStyle = stroke; ctx.lineWidth = 1.4;
  ctx.stroke();
}

async function refreshSparklines() {
  const r = await jsonFetch("/api/ops/sparklines?timeframe=5m&limit=288");
  const env = r.body;
  if (env.status === "down" || !env.data) return;
  const pairs = env.data.pairs || {};
  const tl = env.data.timeline_24h || {regimes: [], sentiment: []};
  const host = document.getElementById("sparklines");
  if (!host) return;

  // Build / re-build the row of cards. Each card gets a price chunk + a
  // regime band + a sentiment mini-canvas (Improvement 3).
  const labels = Object.keys(pairs);
  if (host.dataset.built !== labels.join(",")) {
    host.dataset.built = labels.join(",");
    host.replaceChildren();
    for (const p of labels) {
      const card = document.createElement("div");
      card.style.cssText = "background:#0a0f24;border:1px solid #1f2748;border-radius:8px;padding:10px 12px;display:flex;flex-direction:column;gap:2px;min-width:0;";
      card.innerHTML =
        `<div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px;">` +
        `<span style="font-size:12px;font-weight:600;">${esc(p)}</span>` +
        `<span class="muted" style="font-size:10px;" data-spark-pct>—</span>` +
        `</div>` +
        `<div style="font-size:18px;font-variant-numeric:tabular-nums;" data-spark-price>—</div>` +
        `<canvas data-spark-canvas style="width:100%;height:36px;display:block;"></canvas>` +
        `<div data-spark-regime class="spark-regime-bar" title="regime band, last 24h"></div>` +
        `<canvas data-spark-sentiment class="spark-sentiment-canvas" title="sentiment line, last 24h"></canvas>`;
      card.dataset.pair = p;
      host.appendChild(card);
    }
  }

  for (const card of host.children) {
    const p = card.dataset.pair;
    const v = pairs[p] || {};
    const priceEl = card.querySelector("[data-spark-price]");
    const pctEl = card.querySelector("[data-spark-pct]");
    const canvas = card.querySelector("[data-spark-canvas]");
    const regimeBar = card.querySelector("[data-spark-regime]");
    const sentCanvas = card.querySelector("[data-spark-sentiment]");

    priceEl.textContent = v.current !== null && v.current !== undefined
      ? "$" + Number(v.current).toLocaleString(undefined, {maximumFractionDigits: 4})
      : "—";
    if (v.pct_24h !== null && v.pct_24h !== undefined) {
      const cls = v.pct_24h > 0 ? "ok" : v.pct_24h < 0 ? "bad" : "muted";
      pctEl.className = cls;
      pctEl.style.fontSize = "10px";
      pctEl.textContent = fmtPct(v.pct_24h) + " 24h";
    } else {
      pctEl.textContent = "—";
    }
    drawSparkline(canvas, v.closes || []);

    // Regime band — render proportional segments by duration
    drawRegimeBar(regimeBar, tl.regimes || []);

    // Sentiment mini line — green above 0, red below
    drawSentimentMini(sentCanvas, tl.sentiment || []);
  }
}

function drawRegimeBar(host, points) {
  if (!host) return;
  host.replaceChildren();
  if (!points || points.length === 0) {
    host.style.opacity = "0.3";
    return;
  }
  host.style.opacity = "1";
  const t0 = new Date(points[0].ts).getTime();
  const tN = new Date(points[points.length - 1].ts).getTime();
  const span = Math.max(1, tN - t0);

  // Group consecutive same-regime points into segments
  const segs = [];
  let cur = null;
  for (const p of points) {
    if (!cur || cur.regime !== p.regime) {
      if (cur) segs.push(cur);
      cur = {regime: p.regime, start: new Date(p.ts).getTime(), end: new Date(p.ts).getTime()};
    } else {
      cur.end = new Date(p.ts).getTime();
    }
  }
  if (cur) segs.push(cur);

  for (const s of segs) {
    const w = Math.max(1, ((s.end - s.start) / span) * 100);
    const div = document.createElement("div");
    div.className = "spark-regime-seg regime-" + (s.regime || "unknown");
    div.style.flexBasis = w + "%";
    div.title = `${s.regime}: ${new Date(s.start).toISOString().substring(11,16)}Z → ${new Date(s.end).toISOString().substring(11,16)}Z`;
    host.appendChild(div);
  }
}

function drawSentimentMini(canvas, points) {
  if (!canvas) return;
  if (!points || points.length < 2) {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    return;
  }
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight || 26;
  if (canvas.width !== W * dpr) { canvas.width = W * dpr; canvas.height = H * dpr; }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  // Score domain is [-1, +1] always (clamp). Y midline = 0.
  const mid = H / 2;
  const x = i => (i / (points.length - 1)) * (W - 2) + 1;
  const y = v => mid - (Math.max(-1, Math.min(1, v)) * (H / 2 - 2));

  // Mid baseline
  ctx.strokeStyle = "#1f2748";
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(W, mid); ctx.stroke();

  // Two-pass line: green above zero, red below. We draw segments per-pair.
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1].score, b = points[i].score;
    const avg = (a + b) / 2;
    ctx.strokeStyle = avg >= 0 ? "#4cc38a" : "#ff6b6b";
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    ctx.moveTo(x(i - 1), y(a));
    ctx.lineTo(x(i), y(b));
    ctx.stroke();
  }
}

// ─── Quick Actions (MCP-routed via /api/mcp/{tool_name}) ───────────
async function qaCallTool(tool, args = {}) {
  const r = await fetch(`/api/mcp/${encodeURIComponent(tool)}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(args || {}),
  });
  return r.json();
}

function qaShowResult(tool, env) {
  const box = document.getElementById("qa-result");
  box.style.display = "block";
  // give the browser a tick to register display:block before opening
  requestAnimationFrame(() => box.classList.add("open"));

  const ok = env && env.status === "ok";
  const data = env && env.data;
  let summary;
  switch (tool) {
    case "get_risk_status": {
      const d = data || {};
      summary =
        `<div><strong>${esc(tool)}</strong> · <span class="${ok ? "ok" : "warn"}">${esc(env.status || "?")}</span></div>` +
        `<div class="row"><span class="label">open positions</span><span class="value">${esc(d.open_positions ?? "—")}</span></div>` +
        `<div class="row"><span class="label">trade count</span><span class="value">${esc(d.trade_count ?? "—")}</span></div>` +
        `<div class="row"><span class="label">winning trades</span><span class="value">${esc(d.winning_trades ?? "—")}</span></div>` +
        `<div class="row"><span class="label">total PnL closed</span><span class="value">${esc(d.total_pnl_closed ?? "—")}</span></div>`;
      break;
    }
    case "get_current_regime": {
      const d = data || {};
      const arrow = REGIME_ARROW[d.regime] || "·";
      summary =
        `<div><strong>${esc(tool)}</strong> · <span class="ok">${esc(env.status)}</span></div>` +
        `<div style="font-size:24px;margin-top:6px;">${esc((d.regime || "—").replace("_"," "))} <span style="opacity:0.7;">${esc(arrow)}</span></div>` +
        `<div class="muted" style="font-size:12px;">probability ${fmt(d.probability, 2)} · active ${fmt(d.duration_hours, 1)}h · ts ${esc(d.ts || "—")}</div>`;
      break;
    }
    default: {
      summary =
        `<div><strong>${esc(tool)}</strong> · <span class="${ok ? "ok" : "warn"}">${esc(env.status || "?")}</span></div>` +
        `<pre>${esc(JSON.stringify(data, null, 2))}</pre>`;
      if (env && env.error) {
        summary += `<div class="bad" style="margin-top:6px;">${esc(env.error)}</div>`;
      }
    }
  }
  box.innerHTML = summary;
}

function qaConfirm(message, doubleConfirm = false) {
  if (!confirm(message)) return false;
  if (doubleConfirm && !confirm("Are you absolutely sure? Final confirmation.")) return false;
  return true;
}

async function qaButtonHandler(ev) {
  const btn = ev.currentTarget;
  const tool = btn.dataset.tool;
  const action = btn.dataset.action;
  const confirmMsg = btn.dataset.confirm;
  const doubleConfirm = btn.dataset.double === "1";

  if (confirmMsg && !qaConfirm(confirmMsg, doubleConfirm)) return;

  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="qa-spinner"></span>${original.replace(/^[^\s]+/, "").trim() || tool || action}…`;

  let env;
  try {
    if (tool) {
      // MCP-routed buttons (pause/resume/evolve/risk/regime)
      const args = {};
      if (tool === "pause_trading") args.reason = "manual_pause_via_dashboard_quick_action";
      if (tool === "resume_trading") args.confirm = true;
      env = await qaCallTool(tool, args);
      qaShowResult(tool, env || {});
    } else if (action === "readiness" || action === "readiness-ft") {
      const ft = action === "readiness-ft";
      const r = await jsonFetch(`/api/ops/readiness?fast_track=${ft}`);
      env = r.body;
      qaShowReadiness(env);
    } else if (action === "rebalance-dry") {
      const r = await jsonFetch("/api/ops/rebalance");
      env = r.body;
      qaShowRebalance(env, /*isDry=*/true);
    } else if (action === "config-overview") {
      const r = await jsonFetch("/api/ops/config");
      env = r.body;
      qaShowConfig(env);
    }
  } catch (e) {
    env = {status: "down", error: String(e), data: null};
    qaShowResult(action || tool || "?", env);
  }

  btn.disabled = false;
  btn.innerHTML = original;
}

function qaShowConfig(env) {
  const box = document.getElementById("qa-result");
  box.style.display = "block";
  requestAnimationFrame(() => box.classList.add("open"));

  if (!env || !env.data) {
    box.innerHTML = `<div><strong>Config overview</strong> · <span class="bad">${esc(env && env.status || "?")}</span></div>` +
                    `<div class="bad">${esc(env && env.error || "no data")}</div>`;
    return;
  }
  const d = env.data;
  const cfg = d.config || {};
  const trading = cfg.trading || {};
  const pairs = cfg.pairs || {};
  const cap = cfg.capital_allocation || {};
  const weights = cap.pair_weights || {};

  let html =
    `<div><strong>Config overview</strong> · <span class="ok">live</span> · <span class="muted">${esc(d.config_path)}</span></div>`;

  // Trading basics
  html += `<h4 style="margin:10px 0 4px;font-size:13px;">Trading basics</h4>`;
  html += `<table class="tape"><tbody>`;
  for (const [k, v] of Object.entries(trading)) {
    let val = v === null || v === undefined ? "—" : v;
    if (typeof val === "object") val = JSON.stringify(val);
    html += `<tr><td>${esc(k)}</td><td>${esc(val)}</td></tr>`;
  }
  html += `</tbody></table>`;

  // Pair allocation
  html += `<h4 style="margin:10px 0 4px;font-size:13px;">Pair allocation (${(pairs.whitelist || []).length} pairs)</h4>`;
  html += `<table class="tape"><thead><tr><th>pair</th><th>weight</th><th>min Sharpe</th></tr></thead><tbody>`;
  for (const p of (pairs.whitelist || [])) {
    const w = weights[p];
    const wStr = w === undefined ? "—" : `${(w * 100).toFixed(1)}%`;
    html += `<tr><td>${esc(p)}</td><td>${wStr}</td><td>${esc(cap.min_sharpe_for_trading ?? "—")}</td></tr>`;
  }
  html += `</tbody></table>`;

  // Sentiment sources
  if (cfg.sentiment_sources) {
    html += `<h4 style="margin:10px 0 4px;font-size:13px;">Sentiment sources</h4>`;
    html += `<table class="tape"><thead><tr><th>source</th><th>enabled</th><th>weight</th></tr></thead><tbody>`;
    for (const [k, v] of Object.entries(cfg.sentiment_sources)) {
      if (k.startsWith("_")) continue;
      const en = v.enabled ? "<span class=\"ok\">✓</span>" : "<span class=\"bad\">✗</span>";
      html += `<tr><td>${esc(k)}</td><td>${en}</td><td>${esc(v.weight)}</td></tr>`;
    }
    html += `</tbody></table>`;
  }

  // Env presence
  if (d.env) {
    html += `<h4 style="margin:10px 0 4px;font-size:13px;">Environment variables</h4>`;
    html += `<table class="tape"><tbody>`;
    for (const [k, v] of Object.entries(d.env)) {
      const dispVal = v === null
        ? `<span class="muted">unset</span>`
        : v === "<set>"
        ? `<span class="ok">&lt;set&gt;</span>`
        : `<span class="muted">${esc(v)}</span>`;
      html += `<tr><td>${esc(k)}</td><td>${dispVal}</td></tr>`;
    }
    html += `</tbody></table>`;
  }

  // Full JSON below for the curious
  html += `<details style="margin-top:8px;"><summary class="muted" style="cursor:pointer;font-size:11px;">Full raw JSON…</summary>` +
          `<pre style="margin:6px 0 0;font-size:11px;background:#060a1c;padding:10px;border-radius:4px;max-height:300px;overflow:auto;">${esc(JSON.stringify(d, null, 2))}</pre>` +
          `</details>`;

  box.innerHTML = html;
}

function qaShowReadiness(env) {
  const box = document.getElementById("qa-result");
  box.style.display = "block";
  requestAnimationFrame(() => box.classList.add("open"));

  if (env.status === "down" || !env.data) {
    box.innerHTML = `<div><strong>Validate readiness</strong> · <span class="bad">${esc(env.status || "?")}</span></div>` +
                    `<div class="bad">${esc(env.error || "no data")}</div>`;
    return;
  }
  const d = env.data;
  const mode = d.mode === "fast_track" ? "FAST-TRACK" : "STANDARD";
  const verdictCls = d.ready ? "ok" : "warn";
  const verdictText = d.ready ? "READY ✓" : "NOT READY";
  let html =
    `<div><strong>Go-live readiness</strong> · ${esc(mode)} · ` +
    `<span class="${verdictCls}">${esc(verdictText)}</span></div>` +
    `<div class="muted" style="font-size:11px;margin-top:4px;">` +
    `${esc(d.n_trades)} trades in window` +
    (d.thresholds.window_days ? ` (last ${esc(d.thresholds.window_days)}d)` : "") +
    `</div>`;

  if (d.checks && d.checks.length) {
    html += `<table class="tape" style="margin-top:8px;">` +
            `<thead><tr><th>check</th><th>value</th><th></th><th>threshold</th><th></th></tr></thead><tbody>`;
    for (const c of d.checks) {
      const tick = c.passed ? "<span class=\"ok\">✓</span>" : "<span class=\"bad\">✗</span>";
      const val = c.value === null ? "—" : c.value;
      html += `<tr><td>${esc(c.name)}</td><td>${esc(val)}</td>` +
              `<td>${esc(c.op)}</td><td>${esc(c.threshold)}</td><td>${tick}</td></tr>`;
    }
    html += `</tbody></table>`;
  } else if (d.diagnostics && d.diagnostics.reason) {
    html += `<div class="muted" style="margin-top:6px;">${esc(d.diagnostics.reason)}</div>`;
  }
  box.innerHTML = html;
}

async function qaApplyRebalance(window) {
  if (!confirm("Apply this rebalance? This will atomic-write config.json and " +
               "freqtrade picks up the new weights within 1h. A backup of the " +
               "current config is saved first.")) return;

  const r = await fetch("/api/ops/rebalance", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({confirm: true, window: window || 14}),
  });
  const env = await r.json().catch(() => ({}));
  qaShowRebalance(env, /*isDry=*/false);
}

function qaShowRebalance(env, isDry) {
  const box = document.getElementById("qa-result");
  box.style.display = "block";
  requestAnimationFrame(() => box.classList.add("open"));

  if (!env || (!env.data && !env.detail)) {
    box.innerHTML = `<div><strong>Rebalance</strong> · <span class="bad">failed</span></div>` +
                    `<div class="bad">${esc(env && (env.error || env.detail) || "no response")}</div>`;
    return;
  }
  if (env.detail) {
    box.innerHTML = `<div class="bad">Failed: ${esc(env.detail)}</div>`;
    return;
  }
  const d = env.data;
  const cls = d.applied ? "ok" : (d.n_changes > 0 ? "warn" : "muted");
  const verdict = d.applied ? "APPLIED" : (d.n_changes > 0 ? "DRY-RUN · proposal" : "NO CHANGE");

  let html =
    `<div><strong>Capital rebalance</strong> · <span class="${cls}">${esc(verdict)}</span> ` +
    `· window ${esc(d.window_days)}d · floor ${esc(d.min_sharpe_for_trading)}</div>`;

  if (!d.changes || d.changes.length === 0) {
    html += `<div class="muted" style="margin-top:6px;">${esc(d.note || "weights are stable — no rebalance needed")}</div>`;
  } else {
    html += `<table class="tape" style="margin-top:8px;">` +
            `<thead><tr><th>pair</th><th>sharpe (live)</th><th>from</th><th>→</th><th>to</th></tr></thead><tbody>`;
    for (const c of d.changes) {
      const arrow = c.to > c.from ? "<span class=\"ok\">↑</span>" : c.to < c.from ? "<span class=\"bad\">↓</span>" : "·";
      const sharpe = c.sharpe == null ? "—" : c.sharpe;
      html += `<tr><td>${esc(c.pair)}</td><td>${esc(sharpe)}</td>` +
              `<td>${esc((c.from * 100).toFixed(1))}%</td><td>${arrow}</td>` +
              `<td>${esc((c.to * 100).toFixed(1))}%</td></tr>`;
    }
    html += `</tbody></table>`;
  }

  if (d.backup) {
    html += `<div class="muted" style="font-size:11px;margin-top:6px;">Backup: ${esc(d.backup)}</div>`;
  }
  if (d.note) {
    html += `<div class="muted" style="font-size:11px;margin-top:4px;">${esc(d.note)}</div>`;
  }

  // If this was a dry-run with changes, offer to apply.
  if (isDry && d.n_changes > 0) {
    const apply = document.createElement("button");
    apply.className = "btn primary";
    apply.style.cssText = "margin-top:10px;";
    apply.textContent = `Apply ${d.n_changes} weight change${d.n_changes === 1 ? "" : "s"}`;
    apply.addEventListener("click", () => qaApplyRebalance(d.window_days));
    box.innerHTML = html;
    box.appendChild(apply);
    return;
  }
  box.innerHTML = html;
}

// ─── Slack daily-report preview ─────────────────────────────────────
async function refreshSlackPreview() {
  const r = await jsonFetch("/api/ops/slack_preview");
  const env = r.body;
  setStatus("card-slack-preview", env.status || "down");
  document.getElementById("slack-preview-age").textContent = env.error || "live";
  const body = document.getElementById("slack-preview-body");
  if (!env.data) { body.textContent = env.error || "—"; return; }
  const d = env.data;
  const pnlClass = d.pnl_usd > 0 ? "ok" : d.pnl_usd < 0 ? "bad" : "muted";

  const regimeRows = (d.regime_distribution || []).map(r =>
    `<div>${esc(r.regime)}: <span class="slack-mono">${esc(r.n)}</span></div>`
  ).join(" · ");

  const bestPair = d.best
    ? `${esc(d.best.pair)} <span class="slack-mono">${fmtUsd(d.best.pnl)}</span> (${esc(d.best.n)}t)`
    : "—";
  const worstPair = d.worst
    ? `${esc(d.worst.pair)} <span class="slack-mono">${fmtUsd(d.worst.pnl)}</span> (${esc(d.worst.n)}t)`
    : "—";

  body.innerHTML =
    `<div class="slack-card">` +
      `<div class="slack-author">📊 Trading Bot · daily P&amp;L preview</div>` +
      `<div class="slack-block-header">Daily P&amp;L — ${esc(d.date_utc)} (UTC)</div>` +
      `<div class="slack-fields">` +
        `<div class="slack-field"><strong>Net P&amp;L</strong><br><span class="slack-mono ${pnlClass}">${esc(fmtUsd(d.pnl_usd))} (${esc(fmtPct(d.pnl_pct))})</span></div>` +
        `<div class="slack-field"><strong>Trades</strong><br><span class="slack-mono">${esc(d.trades)} (${esc(d.wins)}W / ${esc(d.losses)}L)</span></div>` +
        `<div class="slack-field"><strong>Win rate</strong><br><span class="slack-mono">${esc(fmt(d.win_rate_pct, 1))}%</span></div>` +
        `<div class="slack-field"><strong>Sharpe (trailing)</strong><br><span class="slack-mono">${esc(fmt(d.sharpe_trailing, 2))}</span></div>` +
      `</div>` +
      `<div class="slack-divider"></div>` +
      `<div><strong>Best pair:</strong> ${bestPair}</div>` +
      `<div><strong>Worst pair:</strong> ${worstPair}</div>` +
      `<div class="slack-context">` +
        `Regime distribution (24h): ${regimeRows || "—"} · Max DD trailing: <span class="slack-mono">${esc(fmtPct((d.max_dd_trailing || 0) * 100))}</span>` +
      `</div>` +
    `</div>`;
}

// ─── Explainability — last 5 decisions for a pair ──────────────────
async function refreshExplainability() {
  const sel = document.getElementById("exp-pair-select");
  const pairValue = sel && sel.value;
  if (!pairValue) return;
  const [base, quote] = pairValue.split("/");
  const r = await jsonFetch(`/api/ops/explainability/${encodeURIComponent(base)}/${encodeURIComponent(quote)}?limit=5`);
  const env = r.body;
  setStatus("card-explainability", env.status || "down");
  document.getElementById("exp-age").textContent = env.error || "live";
  const body = document.getElementById("exp-body");
  if (!env.data || !env.data.decisions || env.data.decisions.length === 0) {
    body.textContent = env.error || "no recent decisions for this pair";
    return;
  }
  body.replaceChildren();
  for (const d of env.data.decisions) {
    body.insertAdjacentHTML("beforeend", renderExpCard(d));
  }
}

function renderExpCard(d) {
  const ts = esc(d.ts || "—");
  if (d.kind === "blocked") {
    return (
      `<div class="exp-card" data-kind="blocked">` +
        `<div class="exp-line"><span class="label">${ts}</span><span class="value warn">NO ENTRY · blocked</span></div>` +
        `<div class="exp-line"><span class="label">pair</span><span class="value">${esc(d.pair)}</span></div>` +
        `<div class="exp-line"><span class="label">constraint</span><span class="value">${esc(d.constraint)}</span></div>` +
        `<div class="exp-line"><span class="label">reason</span><span class="value">${esc(d.reason)}</span></div>` +
      `</div>`
    );
  }
  // entered
  const tft = d.tft_probs || {};
  const drl = d.drl_votes || {};
  const sentDir = d.sentiment_score == null ? "—"
                : d.sentiment_score > 0.1 ? "bullish"
                : d.sentiment_score < -0.1 ? "bearish" : "neutral";
  const closed = d.closed_at
    ? `<div class="exp-line"><span class="label">closed</span><span class="value">${esc(d.closed_at)} · pnl ${esc(fmtPct(d.pnl_pct))} · ${esc(d.exit_reason || "—")}</span></div>`
    : `<div class="exp-line"><span class="label">closed</span><span class="value muted">still open</span></div>`;
  return (
    `<div class="exp-card" data-kind="entered">` +
      `<div class="exp-line"><span class="label">${ts}</span><span class="value ok">ENTRY · ${esc(d.side || "long")}</span></div>` +
      `<div class="exp-line"><span class="label">TFT probs</span><span class="value">up ${fmt(tft.up, 2)} · flat ${fmt(tft.flat, 2)} · down ${fmt(tft.down, 2)}</span></div>` +
      `<div class="exp-line"><span class="label">DRL votes</span><span class="value">${esc(JSON.stringify(drl))}</span></div>` +
      `<div class="exp-line"><span class="label">meta confidence</span><span class="value">${fmt(d.confidence, 2)}</span></div>` +
      `<div class="exp-line"><span class="label">sentiment</span><span class="value">${fmt(d.sentiment_score, 2)} (${sentDir}) · conf ${fmt(d.sentiment_confidence, 2)}</span></div>` +
      `<div class="exp-line"><span class="label">regime</span><span class="value">${esc(d.regime || "—")}</span></div>` +
      `<div class="exp-line"><span class="label">entry / stake</span><span class="value">$${esc(d.entry_price)} · $${esc(d.stake)}</span></div>` +
      (d.reasoning ? `<div class="exp-decision">${esc(d.reasoning)}</div>` : "") +
      closed +
    `</div>`
  );
}

async function loadExpPairs() {
  // Prefill the pair dropdown from /api/pairs (existing)
  const r = await jsonFetch("/api/pairs");
  const pairs = (r.body && r.body.pairs) || ["BTC/USD", "ETH/USD", "SOL/USD"];
  const sel = document.getElementById("exp-pair-select");
  if (!sel) return;
  sel.replaceChildren();
  for (const p of pairs) {
    const opt = document.createElement("option");
    opt.value = p; opt.textContent = p;
    sel.appendChild(opt);
  }
}

// ─── MCP Tool Console ───────────────────────────────────────────────
let _mcp_tools = [];        // [{name, doc, mutating, params}, ...]
const _mcp_history = [];    // most recent first
const MCP_HISTORY_MAX = 10;

async function loadMcpTools() {
  const r = await jsonFetch("/api/ops/tools");
  if (!r.body || !r.body.data || !r.body.data.tools) return;
  _mcp_tools = r.body.data.tools;
  const datalist = document.getElementById("mcp-tool-list");
  if (!datalist) return;
  datalist.replaceChildren();
  for (const t of _mcp_tools) {
    const opt = document.createElement("option");
    opt.value = t.name;
    if (t.mutating) opt.label = "❗ " + t.doc;
    else opt.label = t.doc;
    datalist.appendChild(opt);
  }
}

function _findMcpTool(name) {
  return _mcp_tools.find(t => t.name === name) || null;
}

function showMcpToolParams(toolName) {
  const params = document.getElementById("mcp-params");
  const doc = document.getElementById("mcp-tool-doc");
  const exec = document.getElementById("mcp-execute");
  params.replaceChildren();
  doc.textContent = "";

  const t = _findMcpTool(toolName);
  if (!t) {
    exec.disabled = true;
    return;
  }
  doc.textContent = (t.mutating ? "❗ MUTATING · " : "") + t.doc;
  for (const p of t.params) {
    const wrap = document.createElement("label");
    wrap.style.cssText = "display:flex;flex-direction:column;font-size:11px;color:#7a86b8;";
    wrap.textContent = `${p.name} (${p.type})${p.required ? " *" : ""}`;
    const inp = document.createElement("input");
    inp.type = p.type === "int" || p.type === "float" ? "number" : "text";
    inp.dataset.paramName = p.name;
    inp.dataset.paramType = p.type;
    if (p.type === "bool") {
      inp.type = "checkbox";
      inp.checked = !!p.default;
      inp.style.cssText = "margin-top:4px;";
    } else {
      inp.value = p.default ?? "";
      inp.placeholder = String(p.default ?? "");
      inp.style.cssText = "margin-top:2px;padding:4px 6px;background:#060a1c;color:#e8ecf8;border:1px solid #2c386a;border-radius:4px;font:inherit;";
    }
    wrap.appendChild(inp);
    params.appendChild(wrap);
  }
  exec.disabled = false;
}

function _collectMcpArgs() {
  const out = {};
  document.querySelectorAll("#mcp-params input[data-param-name]").forEach(el => {
    const name = el.dataset.paramName;
    const type = el.dataset.paramType;
    if (type === "bool") {
      out[name] = el.checked;
    } else {
      const raw = el.value.trim();
      if (raw === "") return;  // omit so default applies
      out[name] = raw;
    }
  });
  return out;
}

async function executeMcp() {
  const name = document.getElementById("mcp-tool-input").value.trim();
  const t = _findMcpTool(name);
  if (!t) return;
  const args = _collectMcpArgs();
  const exec = document.getElementById("mcp-execute");
  const out = document.getElementById("mcp-output");

  exec.disabled = true; exec.textContent = "Executing…";
  out.style.display = "block";
  out.textContent = "(running)";

  let env;
  try {
    env = await qaCallTool(name, args);
  } catch (e) {
    env = {status: "down", error: String(e)};
  }
  out.textContent = JSON.stringify(env, null, 2);

  pushMcpHistory(name, args, env);

  exec.disabled = false; exec.textContent = "Execute";
}

function pushMcpHistory(tool, args, env) {
  _mcp_history.unshift({
    ts: new Date().toISOString().substring(11, 19) + "Z",
    tool, args, env,
  });
  while (_mcp_history.length > MCP_HISTORY_MAX) _mcp_history.pop();
  const wrap = document.getElementById("mcp-history-wrap");
  const list = document.getElementById("mcp-history");
  wrap.style.display = "block";
  list.replaceChildren();
  for (const h of _mcp_history) {
    const row = document.createElement("div");
    row.className = "mcp-history-row";
    const status = h.env && h.env.status === "ok" ? "ok" : "bad";
    row.innerHTML =
      `<span class="${status}">${esc(h.env.status || "?")}</span> ` +
      `<strong>${esc(h.tool)}</strong>` +
      `<span class="muted">${esc(JSON.stringify(h.args))}</span>` +
      `<span class="muted">@ ${esc(h.ts)}</span>`;
    row.addEventListener("click", () => {
      document.getElementById("mcp-output").style.display = "block";
      document.getElementById("mcp-output").textContent = JSON.stringify(h.env, null, 2);
    });
    list.appendChild(row);
  }
}

// ─── Regime parameters editor ──────────────────────────────────────
let _rg_pristine = null;   // { regime_gating, schema, config_path }
let _rg_dirty = false;

function rgRowsHtml(rg, schema) {
  const regs = schema.regimes;
  const dlo = schema.delta_range[0], dhi = schema.delta_range[1];
  const rows = [];

  // Per-regime entry/exit deltas as a small grid
  rows.push(`<h4 style="margin:6px 0;font-size:13px;">Per-regime entry / exit deltas <span class="muted" style="font-size:11px;">(allowed [${dlo}, ${dhi}], blank = null = hard-block)</span></h4>`);
  let grid = `<div style="display:grid;grid-template-columns:120px repeat(${regs.length}, 1fr);gap:6px;align-items:center;font-size:12px;">`;
  grid += `<div></div>`;
  for (const r of regs) grid += `<div class="muted" style="text-align:center;">${esc(r)}</div>`;
  for (const which of ["entry_delta", "exit_delta"]) {
    grid += `<div class="muted">${esc(which)}</div>`;
    for (const r of regs) {
      const v = (rg[which] || {})[r];
      const val = v === null || v === undefined ? "" : v;
      grid += `<input data-rg-deep="${esc(which)}" data-rg-key="${esc(r)}" type="number" step="0.01" value="${esc(val)}" style="width:100%;padding:4px 6px;background:#060a1c;color:#e8ecf8;border:1px solid #2c386a;border-radius:4px;font:inherit;font-variant-numeric:tabular-nums;" />`;
    }
  }
  grid += `</div>`;
  rows.push(grid);

  // Scalar params (one input per row)
  rows.push(`<h4 style="margin:14px 0 6px;font-size:13px;">Scalar parameters</h4>`);
  let scal = `<div style="display:grid;grid-template-columns:1fr 160px 200px;gap:6px;align-items:center;font-size:12px;">`;
  scal += `<div class="muted">key</div><div class="muted" style="text-align:center;">value</div><div class="muted" style="text-align:center;">allowed range</div>`;
  for (const [k, [lo, hi]] of Object.entries(schema.scalar_ranges)) {
    const v = rg[k];
    scal += `<div>${esc(k)}</div>`;
    scal += `<input data-rg-key="${esc(k)}" type="number" step="0.01" value="${esc(v ?? "")}" style="width:100%;padding:4px 6px;background:#060a1c;color:#e8ecf8;border:1px solid #2c386a;border-radius:4px;font:inherit;font-variant-numeric:tabular-nums;" />`;
    scal += `<div class="muted" style="text-align:center;">[${lo}, ${hi}]</div>`;
  }
  scal += `</div>`;
  rows.push(scal);

  return rows.join("");
}

async function refreshRegimeConfig() {
  const r = await jsonFetch("/api/ops/regime_config");
  const env = r.body;
  setStatus("card-regime-params", env.status || "down");
  document.getElementById("regime-params-age").textContent = env.error || env.data?.config_path || "live";
  const body = document.getElementById("regime-params-body");
  if (!env.data) { body.textContent = env.error || "—"; return; }
  _rg_pristine = JSON.parse(JSON.stringify(env.data));
  body.innerHTML = rgRowsHtml(env.data.regime_gating, env.data.schema);
  _rg_dirty = false;
  document.getElementById("btn-rg-apply").disabled = true;

  body.querySelectorAll("input[data-rg-key]").forEach(el => {
    el.addEventListener("input", () => {
      _rg_dirty = true;
      document.getElementById("btn-rg-apply").disabled = false;
    });
  });
}

function rgCollect() {
  // Build a regime_gating object from the inputs.
  const out = {};
  document.querySelectorAll("#regime-params-body input[data-rg-key]").forEach(el => {
    const key = el.dataset.rgKey;
    const deep = el.dataset.rgDeep;
    const raw = el.value.trim();
    const val = raw === "" ? null : Number(raw);
    if (deep) {
      out[deep] = out[deep] || {};
      out[deep][key] = val;
    } else {
      // Scalars don't accept null — skip if blank
      if (val !== null) out[key] = val;
    }
  });
  return out;
}

async function applyRegimeConfig() {
  const btn = document.getElementById("btn-rg-apply");
  btn.disabled = true; btn.textContent = "Applying…";
  const payload = { regime_gating: rgCollect() };
  const r = await fetch("/api/ops/regime_config", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  const env = await r.json().catch(() => ({}));
  if (r.ok && env.data) {
    const lines = (env.data.changes || []).join("\n  ");
    alert(`Applied ${env.data.changes?.length || 0} change(s):\n  ${lines}\n\nBackup: ${env.data.backup}\nFreqtrade reload: ${env.data.freqtrade_reload}\n\n${env.data.note || ""}`);
    refreshRegimeConfig();
  } else {
    alert(`Failed: ${JSON.stringify(env, null, 2)}`);
  }
  btn.textContent = "Apply changes…";
}

document.addEventListener("DOMContentLoaded", () => {
  // Quick Actions buttons (replace the old btn-pause / btn-resume / resume-modal flow)
  document.querySelectorAll(".qa-btn").forEach(b => b.addEventListener("click", qaButtonHandler));

  // Regime params editor
  const rgApply = document.getElementById("btn-rg-apply");
  const rgRevert = document.getElementById("btn-rg-revert");
  if (rgApply) rgApply.addEventListener("click", applyRegimeConfig);
  if (rgRevert) rgRevert.addEventListener("click", () => refreshRegimeConfig());

  // Explainability pair selector
  const expSel = document.getElementById("exp-pair-select");
  if (expSel) expSel.addEventListener("change", refreshExplainability);

  // MCP Console
  const mcpInput = document.getElementById("mcp-tool-input");
  const mcpExec  = document.getElementById("mcp-execute");
  if (mcpInput) mcpInput.addEventListener("input", () => showMcpToolParams(mcpInput.value.trim()));
  if (mcpExec)  mcpExec.addEventListener("click", executeMcp);

  // First-paint refreshes
  refreshLiveTrades();
  refreshGates();
  refreshCombined();
  refreshLLMStats();
  refreshCircuitBreakers();
  refreshStocksML();
  refreshRegime().then(refreshSentiment);
  refreshStockRegime();
  refreshServices(); refreshTraining(); refreshMcp(); refreshTrades();
  refreshStocks();
  refreshRegimeConfig(); refreshSparklines();
  refreshSlackPreview();
  loadExpPairs().then(refreshExplainability);
  loadMcpTools();

  // ─── Refresh controller — one master tick, dropdown + button govern it ──
  const REFRESH_LS_KEY = "ops.refresh_interval_ms";
  const ALL_REFRESHERS = [
    refreshLiveTrades, refreshGates, refreshCombined, refreshLLMStats,
    refreshCircuitBreakers, refreshStocksML,
    () => refreshRegime().then(refreshSentiment),
    refreshStockRegime,
    refreshServices, refreshTraining, refreshMcp, refreshTrades,
    refreshStocks, refreshSparklines, refreshSlackPreview, refreshExplainability,
  ];
  let _masterTimer = null;
  function _refreshAll() {
    for (const fn of ALL_REFRESHERS) {
      try { Promise.resolve(fn()).catch(() => {}); } catch (_) { /* noop */ }
    }
  }
  function _setRefreshInterval(ms) {
    if (_masterTimer) { clearInterval(_masterTimer); _masterTimer = null; }
    if (ms > 0) _masterTimer = setInterval(_refreshAll, ms);
  }
  // Wire the dropdown + force-refresh button
  const refreshSel = document.getElementById("refresh-interval-select");
  const refreshBtn = document.getElementById("refresh-now-btn");
  // Restore saved choice if any
  if (refreshSel) {
    const saved = localStorage.getItem(REFRESH_LS_KEY);
    if (saved !== null && [...refreshSel.options].some(o => o.value === saved)) {
      refreshSel.value = saved;
    }
    refreshSel.addEventListener("change", () => {
      const ms = parseInt(refreshSel.value, 10) || 0;
      localStorage.setItem(REFRESH_LS_KEY, String(ms));
      _setRefreshInterval(ms);
    });
    _setRefreshInterval(parseInt(refreshSel.value, 10) || 0);
  } else {
    // No dropdown — fall back to default 10s
    _setRefreshInterval(10000);
  }
  if (refreshBtn) {
    refreshBtn.addEventListener("click", (e) => {
      e.preventDefault();
      refreshBtn.disabled = true;
      _refreshAll();
      setTimeout(() => { refreshBtn.disabled = false; }, 300);
    });
  }
});
