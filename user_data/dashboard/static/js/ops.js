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
  el.textContent = d.toISOString().substring(11, 19) + "Z";
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
  document.getElementById("hero-regime").innerHTML =
    "REGIME: <strong>" + esc((d.current || "—").replace("_", " ")) + "</strong> " +
    "<span class=\"arrow\">" + esc(arrow) + "</span>";
  const dur = d.duration_hours ? `${Number(d.duration_hours).toFixed(1)}h` : "—";
  const prob = d.probability ? Number(d.probability).toFixed(2) : "—";
  document.getElementById("hero-meta").textContent =
    `prob ${prob} · active ${dur}` + (d.age_s ? ` · row age ${d.age_s}s` : "");
}

async function refreshSentiment() {
  const r = await jsonFetch("/api/ops/sentiment");
  const env = r.body;
  if (env.status === "down" || !env.data) return;
  const d = env.data;
  const direction = d.score > 0.1 ? "bullish" : d.score < -0.1 ? "bearish" : "neutral";
  const agree = d.agreement ? "✓ agree" : "✗ disagree";
  // append as plain text — direction/agree are constants so no esc needed,
  // but d.score / d.confidence / d.n_headlines are numbers so safe by construction.
  const meta = document.getElementById("hero-meta");
  meta.textContent = meta.textContent +
    ` · sentiment ${fmt(d.score, 2)} (${direction}, conf ${fmt(d.confidence, 2)}, ${agree}, ${d.n_headlines} hl)`;
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
      const t_str = t.exit_time ? new Date(t.exit_time).toISOString().substring(11,16) + "Z" : "—";
      html += `<tr><td>${esc(t.pair)}</td><td>${esc(t.side)}</td><td class="muted">${esc(t.regime_at_entry || "—")}</td><td>${esc(fmtPct(t.pnl_pct))}</td><td class="muted">${esc(t_str)}</td></tr>`;
    }
    html += `</tbody></table>`;
  }

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
    }
  } catch (e) {
    env = {status: "down", error: String(e), data: null};
    qaShowResult(action || tool || "?", env);
  }

  btn.disabled = false;
  btn.innerHTML = original;
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
  refreshRegime().then(refreshSentiment);
  refreshServices(); refreshTraining(); refreshMcp(); refreshTrades();
  refreshRegimeConfig(); refreshSparklines();
  refreshSlackPreview();
  loadExpPairs().then(refreshExplainability);
  loadMcpTools();

  setInterval(() => { refreshRegime().then(refreshSentiment); }, REFRESH_MS.regime);
  setInterval(refreshServices, REFRESH_MS.services);
  setInterval(refreshTraining, REFRESH_MS.training);
  setInterval(refreshMcp,      REFRESH_MS.mcp);
  setInterval(refreshTrades,   REFRESH_MS.trades);
  setInterval(refreshSparklines,    REFRESH_MS.sparklines);
  setInterval(refreshSlackPreview,  REFRESH_MS.slack_preview);
  setInterval(refreshExplainability, REFRESH_MS.explainability);
});
