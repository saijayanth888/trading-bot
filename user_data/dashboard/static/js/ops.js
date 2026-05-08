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
async function refreshTraining() {
  const r = await jsonFetch("/api/ops/training");
  const env = r.body;
  setStatus("card-training", env.status || "down");
  document.getElementById("training-age").textContent = env.error ? "amber" : "live";
  const body = document.getElementById("training-body");
  if (!env.data) { body.textContent = env.error || "—"; return; }
  const d = env.data;
  body.replaceChildren();

  if (d.tft && d.tft.epoch !== undefined) {
    const pct = d.tft.max_epoch ? (d.tft.epoch / d.tft.max_epoch * 100) : 0;
    body.insertAdjacentHTML("beforeend",
      `<div class="row"><span class="label">TFT epoch</span><span class="value">${esc(d.tft.epoch)}/${esc(d.tft.max_epoch || "?")}</span></div>` +
      `<div class="progress"><div style="width:${esc(pct.toFixed(1))}%"></div></div>` +
      `<div class="row"><span class="label muted">val_sharpe / loss</span><span class="value">${fmt(d.tft.val_sharpe, 2)} / ${fmt(d.tft.loss, 2)}</span></div>`
    );
  } else {
    body.insertAdjacentHTML("beforeend",
      `<div class="row"><span class="label">TFT</span><span class="muted">no recent epoch line</span></div>`);
  }

  const drlText = (d.drl && d.drl.status) ? d.drl.status : "—";
  body.insertAdjacentHTML("beforeend",
    `<div class="row"><span class="label">DRL</span><span class="value muted">${esc(drlText)}</span></div>`);

  if (d.ept && d.ept.generation !== undefined) {
    body.insertAdjacentHTML("beforeend",
      `<div class="row"><span class="label">EPT gen</span><span class="value">${esc(d.ept.generation)} (champ ${esc(d.ept.champion_id || "?")})</span></div>`);
  } else {
    body.insertAdjacentHTML("beforeend",
      `<div class="row"><span class="label">EPT</span><span class="muted">${esc((d.ept && d.ept.note) || "no generation yet")}</span></div>`);
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
  body.innerHTML =
    `<div class="row"><span class="label">endpoint</span><span class="value muted">${esc(d.endpoint)}</span></div>` +
    `<div class="row"><span class="label">transport</span><span class="value">${esc(d.transport)}</span></div>` +
    `<div class="row"><span class="label">probe</span><span class="value">${esc(d.probe.code)} ${okBad}</span></div>` +
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

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("btn-pause").addEventListener("click", pauseClicked);
  document.getElementById("btn-resume").addEventListener("click", openResumeModal);
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

  setInterval(() => { refreshRegime().then(refreshSentiment); }, REFRESH_MS.regime);
  setInterval(refreshServices, REFRESH_MS.services);
  setInterval(refreshTraining, REFRESH_MS.training);
  setInterval(refreshMcp,      REFRESH_MS.mcp);
  setInterval(refreshTrades,   REFRESH_MS.trades);
});
