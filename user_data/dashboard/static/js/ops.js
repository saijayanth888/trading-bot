/* Ops tab — fetches the six /api/ops/* endpoints on staggered intervals
 * and renders them. No framework; vanilla DOM. Fail-soft: any panel that
 * times out / errors goes amber/red without crashing the page.
 *
 * Defense-in-depth: every variable interpolated into HTML goes through
 * esc() first. Data ultimately comes from our own DB, but a malicious
 * pair name / regime label / log line shouldn't be able to inject script.
 */

const REFRESH_MS = {
  services:    5000,
  training:   10000,
  regime:     30000,
  sentiment:  30000,
  mcp:        15000,
  trades:      5000,
  sparklines: 60000,  // 5m candles only change every 5 min; refresh once a min
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
  const host = document.getElementById("sparklines");
  if (!host) return;

  // Build / re-build the row of cards
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
        `<canvas data-spark-canvas style="width:100%;height:36px;display:block;"></canvas>`;
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
  }
}

// ─── Pause / Resume ─────────────────────────────────────────────────
async function pauseClicked() {
  const btn = document.getElementById("btn-pause");
  btn.disabled = true; btn.textContent = "Pausing…";
  const r = await fetch("/api/ops/pause", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({reason: "ops-tab manual pause"}),
  });
  const body = await r.json().catch(() => ({}));
  alert(r.ok ? "Paused. Freqtrade reports: " + JSON.stringify(body.data) : "Failed: " + JSON.stringify(body));
  btn.disabled = false; btn.textContent = "⏸ Pause Trading";
}

function openResumeModal() {
  const m = document.getElementById("resume-modal");
  m.classList.add("open");
  fetch("/api/ops/trades_risk").then(r => r.json()).then(env => {
    const d = env.data || {};
    document.getElementById("resume-context").textContent =
      `Current: open ${d.open_count}/${d.max_open}, DD 30d ${fmtPct(d.drawdown_pct_30d)}, breaker ${d.circuit_breaker && d.circuit_breaker.active ? "ACTIVE" : "clear"}.`;
  });
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
  document.getElementById("btn-pause").addEventListener("click", pauseClicked);
  document.getElementById("btn-resume").addEventListener("click", openResumeModal);
  document.getElementById("btn-rg-apply").addEventListener("click", applyRegimeConfig);
  document.getElementById("btn-rg-revert").addEventListener("click", () => refreshRegimeConfig());
  document.getElementById("resume-cancel").addEventListener("click", () => {
    document.getElementById("resume-modal").classList.remove("open");
    document.getElementById("resume-input").value = "";
    document.getElementById("resume-confirm").disabled = true;
  });
  document.getElementById("resume-input").addEventListener("input", (e) => {
    document.getElementById("resume-confirm").disabled = e.target.value !== "RESUME";
  });
  document.getElementById("resume-confirm").addEventListener("click", async () => {
    const r = await fetch("/api/ops/resume", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({confirm: true, reason: "ops-tab manual resume"}),
    });
    const body = await r.json().catch(() => ({}));
    alert(r.ok ? "Resumed. " + JSON.stringify(body.data) : "Failed: " + JSON.stringify(body));
    document.getElementById("resume-modal").classList.remove("open");
    document.getElementById("resume-input").value = "";
    document.getElementById("resume-confirm").disabled = true;
  });

  refreshRegime().then(refreshSentiment);
  refreshServices(); refreshTraining(); refreshMcp(); refreshTrades();
  refreshRegimeConfig(); refreshSparklines();

  setInterval(() => { refreshRegime().then(refreshSentiment); }, REFRESH_MS.regime);
  setInterval(refreshServices, REFRESH_MS.services);
  setInterval(refreshTraining, REFRESH_MS.training);
  setInterval(refreshMcp,      REFRESH_MS.mcp);
  setInterval(refreshTrades,   REFRESH_MS.trades);
  setInterval(refreshSparklines, REFRESH_MS.sparklines);
});
