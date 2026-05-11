/* ════════════════════════════════════════════════════════════════════
   Quanta dashboard — pure helpers from the prototype's data.jsx.
   Exposed on window.QU.
   ════════════════════════════════════════════════════════════════════ */
(function (global) {
  "use strict";

  function fmtUSD(v, frac = 2) {
    if (v == null || !Number.isFinite(v)) return "—";
    return v.toLocaleString("en-US", { minimumFractionDigits: frac, maximumFractionDigits: frac });
  }
  function fmtSigned(v, frac = 2) {
    if (v == null || !Number.isFinite(v)) return "—";
    const s = v >= 0 ? "+" : "";
    return s + v.toLocaleString("en-US", { minimumFractionDigits: frac, maximumFractionDigits: frac });
  }
  function fmtPct(v, signed = false, frac = 2) {
    if (v == null || !Number.isFinite(v)) return "—";
    const txt = v.toFixed(frac) + "%";
    return signed ? ((v >= 0 ? "+" : "") + txt) : txt;
  }
  function fmtAgo(secs) {
    if (secs == null || !Number.isFinite(secs)) return "—";
    const a = Math.abs(secs);
    if (a < 60)    return Math.round(a) + "s";
    if (a < 3600)  return Math.round(a / 60) + "m";
    if (a < 86400) return Math.round(a / 3600) + "h";
    return Math.round(a / 86400) + "d";
  }
  function fmtClock(d) {
    d = d || new Date();
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    return `${hh}:${mm}:${ss}`;
  }
  function fmtClockET(d) {
    d = d || new Date();
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true,
      timeZoneName: "short",
    }).formatToParts(d).reduce((m, p) => (m[p.type] = p.value, m), {});
    return `${parts.hour}:${parts.minute}:${parts.second} ${parts.dayPeriod || ""} ${parts.timeZoneName}`.trim();
  }

  // genSeries / genCandles — kept for any test fixtures; production reads
  // /api/candles/{base}/{quote} for real data.
  function genSeries(n, start, vol, drift = 0) {
    const out = []; let x = start;
    for (let i = 0; i < n; i++) {
      const r = (Math.sin(i * 0.42 + start) + Math.cos(i * 0.13) + (Math.random() - 0.5) * 1.6) * vol;
      x = Math.max(0.001, x * (1 + r * 0.01 + drift));
      out.push(x);
    }
    return out;
  }

  global.QU = { fmtUSD, fmtSigned, fmtPct, fmtAgo, fmtClock, fmtClockET, genSeries };
})(window);
