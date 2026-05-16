// Shared primitives for both Operator and Telemetry directions.
// Exposes components on window so the per-direction Babel files can use them.
// Animations: V4 tick rows flash on change every few seconds (rotating through
// rows); Shark debate streams in a fresh line periodically; Hermes pulses with
// its next-fire countdown; Agent Flow has packets traveling along edges.

const { useState, useEffect, useRef, useMemo } = React;

// ─── Sparkline ───────────────────────────────────────────────────
function Sparkline({ data, width = 120, height = 28, color, fill, strokeWidth = 1.4 }) {
  if (!data || !data.length) return null;
  const min = Math.min(...data), max = Math.max(...data);
  const span = max - min || 1;
  const stepX = width / (data.length - 1 || 1);
  const pts = data.map((v, i) => [i * stepX, height - ((v - min) / span) * (height - 2) - 1]);
  const path = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
  const area = `${path} L${width} ${height} L0 ${height} Z`;
  return (
    <svg width={width} height={height} style={{ display: "block", overflow: "visible" }}>
      {fill && <path d={area} fill={fill} />}
      <path d={path} fill="none" stroke={color} strokeWidth={strokeWidth} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

// ─── StatusDot (Wong palette) ────────────────────────────────────
function StatusDot({ level = "ok", pulse = false, size = 8 }) {
  const colors = { ok: "var(--q-ok)", warn: "var(--q-warn)", crit: "var(--q-crit)", stale: "var(--q-stale)", off: "var(--q-t-4)" };
  return (
    <span
      className={pulse ? "q-pulse" : ""}
      style={{ display: "inline-block", width: size, height: size, borderRadius: "50%", background: colors[level], flexShrink: 0,
        boxShadow: pulse && level !== "stale" ? `0 0 8px ${colors[level]}` : "none" }}
    />
  );
}

// ─── FreshnessChip ───────────────────────────────────────────────
// Distinguishes "stale because closed" (calm grey) from "stale because broken"
// (vermillion). Live data shows the live age + pulsing blue dot.
function FreshnessChip({ age, mode = "live" }) {
  // mode: live | closed | stale | broken
  const conf = {
    live:   { dot: "ok",    text: "LIVE",   color: "var(--q-ok)",    bg: "var(--q-ok-soft)",    bord: "var(--q-ok-bord)" },
    closed: { dot: "stale", text: "CLOSED", color: "var(--q-stale)", bg: "var(--q-stale-soft)", bord: "var(--q-line)" },
    stale:  { dot: "warn",  text: "STALE",  color: "var(--q-warn)",  bg: "var(--q-warn-soft)",  bord: "var(--q-warn-bord)" },
    broken: { dot: "crit",  text: "BROKEN", color: "var(--q-crit)",  bg: "var(--q-crit-soft)",  bord: "var(--q-crit-bord)" },
  }[mode];
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "2px 7px",
      background: conf.bg, color: conf.color, borderRadius: 999, fontSize: "var(--q-fs-pico)",
      fontFamily: "var(--q-font-mono)", letterSpacing: "0.08em", fontWeight: 500,
      boxShadow: `inset 0 0 0 1px ${conf.bord}` }}>
      <StatusDot level={conf.dot} pulse={mode === "live"} size={5} />
      {conf.text}<span style={{ opacity: 0.6, marginLeft: 2 }}>{age}</span>
    </span>
  );
}

// ─── Kbd ─────────────────────────────────────────────────────────
function Kbd({ children }) {
  return (
    <span style={{
      fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)", fontWeight: 500,
      padding: "1px 5px", borderRadius: 4, color: "var(--q-t-2)",
      background: "var(--q-bg-3)", boxShadow: "inset 0 0 0 1px var(--q-line)",
    }}>{children}</span>
  );
}

// ─── Money + Percent formatters ──────────────────────────────────
const fmtUSD = (v, { sign = false, dec = 2 } = {}) => {
  const s = v < 0 ? "−" : sign ? "+" : "";
  return s + "$" + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec });
};
const fmtPct = (v, dec = 2) => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(dec) + "%";

// ─── DrawdownRibbon ──────────────────────────────────────────────
// Shows current DD relative to pause (-3%) and kill (-10%) thresholds.
// Vermillion zone at the far end. Marker shows where we are.
function DrawdownRibbon({ current, pause, kill, width = 480, height = 38, compact = false }) {
  const x = (pct) => (Math.min(0, Math.max(kill, pct)) / kill) * width;
  const cx = x(current);
  const px = x(pause);
  const okEnd = px;
  const warnEnd = (px + width) / 2;
  return (
    <div style={{ width }}>
      <div style={{ position: "relative", height, width }}>
        {/* track */}
        <div style={{ position: "absolute", inset: 0, borderRadius: 6, overflow: "hidden",
          background: "linear-gradient(90deg, var(--q-ok-soft) 0%, var(--q-ok-soft) " + (okEnd/width*100) + "%, var(--q-warn-soft) " + (okEnd/width*100) + "%, var(--q-warn-soft) " + (warnEnd/width*100) + "%, var(--q-crit-soft) " + (warnEnd/width*100) + "%, var(--q-crit-soft) 100%)",
          boxShadow: "inset 0 0 0 1px var(--q-line)" }} />
        {/* threshold lines */}
        <div style={{ position: "absolute", left: px, top: 0, bottom: 0, width: 1, background: "var(--q-warn-bord)" }} />
        <div style={{ position: "absolute", right: 0, top: 0, bottom: 0, width: 1, background: "var(--q-crit-bord)" }} />
        {/* marker */}
        <div style={{ position: "absolute", left: cx - 1, top: -4, bottom: -4, width: 2,
          background: current > pause ? "var(--q-ok)" : current > kill/2 ? "var(--q-warn)" : "var(--q-crit)",
          boxShadow: "0 0 8px currentColor" }} />
        <div style={{ position: "absolute", left: cx - 28, top: -22, fontFamily: "var(--q-font-mono)",
          fontSize: "var(--q-fs-micro)", fontWeight: 600, color: "var(--q-t-1)", whiteSpace: "nowrap" }}>
          {fmtPct(current)}
        </div>
      </div>
      {!compact && (
        <div style={{ display: "flex", justifyContent: "space-between", marginTop: 6, fontFamily: "var(--q-font-mono)",
          fontSize: "var(--q-fs-pico)", color: "var(--q-t-3)", letterSpacing: "0.06em" }}>
          <span>0%</span>
          <span style={{ color: "var(--q-warn)" }}>PAUSE {fmtPct(pause)}</span>
          <span style={{ color: "var(--q-crit)" }}>KILL {fmtPct(kill)}</span>
        </div>
      )}
    </div>
  );
}

// ─── KillSwitch ──────────────────────────────────────────────────
// Type-to-confirm. Default state shows a vermillion bordered button; clicking
// reveals a text input that requires "KILL" to enable the commit.
function KillSwitch({ compact = false }) {
  const [armed, setArmed] = useState(false);
  const [val, setVal] = useState("");
  const ok = val === "KILL";
  if (compact) {
    return (
      <button
        onClick={() => setArmed(true)}
        style={{ padding: "6px 12px", borderRadius: 6, color: "var(--q-crit)",
          background: "var(--q-crit-soft)", boxShadow: "inset 0 0 0 1px var(--q-crit-bord)",
          fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-nano)", fontWeight: 600,
          letterSpacing: "0.10em", textTransform: "uppercase", display: "inline-flex", gap: 6, alignItems: "center" }}>
        <span style={{ width: 6, height: 6, borderRadius: 3, background: "var(--q-crit)", boxShadow: "0 0 6px var(--q-crit)" }}/>
        KILL ALL
      </button>
    );
  }
  if (!armed) return (
    <button onClick={() => setArmed(true)}
      style={{ padding: "9px 18px", borderRadius: 8, color: "var(--q-crit)",
        background: "var(--q-crit-soft)", boxShadow: "inset 0 0 0 1px var(--q-crit-bord)",
        fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-micro)", fontWeight: 600,
        letterSpacing: "0.14em", textTransform: "uppercase", display: "inline-flex", gap: 8, alignItems: "center" }}>
      <span style={{ width: 7, height: 7, borderRadius: 4, background: "var(--q-crit)", boxShadow: "0 0 8px var(--q-crit)" }}/>
      KILL ALL · FLATTEN
    </button>
  );
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "5px 5px 5px 12px",
      borderRadius: 8, background: "var(--q-crit-soft)", boxShadow: "inset 0 0 0 1px var(--q-crit-bord)" }}>
      <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
        color: "var(--q-crit)", letterSpacing: "0.12em", textTransform: "uppercase", fontWeight: 600 }}>type KILL</span>
      <input value={val} autoFocus onChange={(e) => setVal(e.target.value.toUpperCase())}
        style={{ width: 80, padding: "5px 8px", borderRadius: 5, background: "var(--q-bg-0)",
          color: "var(--q-t-1)", border: 0, boxShadow: "inset 0 0 0 1px var(--q-line-strong)",
          fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-micro)", fontWeight: 600, letterSpacing: "0.12em",
          outline: "none" }}/>
      <button disabled={!ok} onClick={() => { setArmed(false); setVal(""); }}
        style={{ padding: "5px 10px", borderRadius: 5, background: ok ? "var(--q-crit)" : "var(--q-bg-3)",
          color: ok ? "#fff" : "var(--q-t-3)", fontFamily: "var(--q-font-mono)",
          fontSize: "var(--q-fs-pico)", fontWeight: 700, letterSpacing: "0.12em",
          textTransform: "uppercase", cursor: ok ? "pointer" : "default" }}>FIRE</button>
      <button onClick={() => { setArmed(false); setVal(""); }}
        style={{ padding: "5px 6px", color: "var(--q-t-3)", fontSize: 14 }}>×</button>
    </div>
  );
}

// ─── V4 TickTable — flash-on-change ──────────────────────────────
// Cycles through rows; each cycle a random row briefly flashes (up or down)
// to suggest a fresh decision arriving from the 5min tick.
function V4TickTable({ pairs, dense = false }) {
  const [flashRow, setFlashRow] = useState({ idx: -1, dir: 0 });
  useEffect(() => {
    let id;
    const tick = () => {
      const idx = Math.floor(Math.random() * pairs.length);
      const dir = Math.random() > 0.5 ? 1 : -1;
      setFlashRow({ idx, dir });
      id = setTimeout(tick, 1400 + Math.random() * 1800);
    };
    id = setTimeout(tick, 800);
    return () => clearTimeout(id);
  }, [pairs.length]);

  const decisionColor = (d) => d === "LONG" ? "var(--q-pl-pos)" : d === "SHORT" ? "var(--q-pl-neg)" : "var(--q-t-3)";
  const cellPad = dense ? "5px 10px" : "8px 12px";
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: "var(--q-font-mono)",
      fontSize: "var(--q-fs-small)", color: "var(--q-t-1)" }}>
      <thead>
        <tr style={{ color: "var(--q-t-3)", fontWeight: 500, letterSpacing: "0.08em",
          fontSize: "var(--q-fs-pico)", textTransform: "uppercase" }}>
          <th style={{ textAlign: "left",  padding: cellPad, fontWeight: 500 }}>pair</th>
          <th style={{ textAlign: "right", padding: cellPad, fontWeight: 500 }}>last</th>
          <th style={{ textAlign: "right", padding: cellPad, fontWeight: 500 }}>24h</th>
          <th style={{ textAlign: "center",padding: cellPad, fontWeight: 500 }}>bb</th>
          <th style={{ textAlign: "center",padding: cellPad, fontWeight: 500 }}>tf</th>
          <th style={{ textAlign: "center",padding: cellPad, fontWeight: 500 }}>regime</th>
          <th style={{ textAlign: "left",  padding: cellPad, fontWeight: 500 }}>decision</th>
          <th style={{ textAlign: "right", padding: cellPad, fontWeight: 500 }}>conf</th>
          <th style={{ textAlign: "right", padding: cellPad, fontWeight: 500 }}>fresh</th>
        </tr>
      </thead>
      <tbody>
        {pairs.map(([sym, px, d24, bb, tf, regime, dec, conf, fresh], i) => {
          const flash = flashRow.idx === i;
          const flashBg = flash ? (flashRow.dir > 0 ? "rgba(94,193,138,0.10)" : "rgba(226,107,107,0.10)") : "transparent";
          return (
            <tr key={sym} style={{ borderTop: "1px solid var(--q-line-subtle)",
              background: flashBg, transition: "background 600ms ease-out" }}>
              <td style={{ padding: cellPad, color: "var(--q-t-1)", fontWeight: 500 }}>{sym}</td>
              <td style={{ padding: cellPad, textAlign: "right", color: "var(--q-t-1)" }}>{px.toLocaleString("en-US", { minimumFractionDigits: px < 1 ? 4 : 2 })}</td>
              <td style={{ padding: cellPad, textAlign: "right",
                color: d24 > 0 ? "var(--q-pl-pos)" : d24 < 0 ? "var(--q-pl-neg)" : "var(--q-t-3)" }}>
                {d24 > 0 ? "+" : ""}{d24.toFixed(2)}%
              </td>
              <td style={{ padding: cellPad, textAlign: "center", color: bb === "MR" ? "var(--q-accent)" : "var(--q-t-4)" }}>{bb}</td>
              <td style={{ padding: cellPad, textAlign: "center", color: tf === "TF" ? "var(--q-accent)" : "var(--q-t-4)" }}>{tf}</td>
              <td style={{ padding: cellPad, textAlign: "center" }}>
                <span style={{ display: "inline-block", width: 5, height: 5, borderRadius: 3,
                  background: regime === "ok" ? "var(--q-ok)" : "var(--q-warn)" }}/>
              </td>
              <td style={{ padding: cellPad, color: decisionColor(dec), fontWeight: 600 }}>{dec}</td>
              <td style={{ padding: cellPad, textAlign: "right", color: "var(--q-t-2)" }}>{Number(conf).toFixed(2)}</td>
              <td style={{ padding: cellPad, textAlign: "right", color: "var(--q-t-3)" }}>{fresh}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

// ─── SharkDebate — streaming transcript ──────────────────────────
// Shows last 3 turns. Newest line types in over ~1.6s; clicking expands to
// show all 4 turns. Roles: bull (green), bear (red), arbiter (indigo).
function SharkDebate({ debate, expanded: forceExpanded, onToggle }) {
  const [streamIdx, setStreamIdx] = useState(0);
  const [tickKey, setTickKey] = useState(0);
  const [expanded, setExpanded] = useState(!!forceExpanded);
  const isExp = forceExpanded ?? expanded;
  useEffect(() => {
    let id;
    const tick = () => { setTickKey((k) => k + 1); id = setTimeout(tick, 6000); };
    id = setTimeout(tick, 5000);
    return () => clearTimeout(id);
  }, []);
  const visible = isExp ? debate : debate.slice(-3);
  const newestIdx = visible.length - 1;

  const role = {
    bull:    { color: "var(--q-pl-pos)", label: "BULL",    glyph: "▲" },
    bear:    { color: "var(--q-pl-neg)", label: "BEAR",    glyph: "▼" },
    arbiter: { color: "var(--q-accent)", label: "ARBITER", glyph: "◆" },
  };
  return (
    <div>
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {visible.map((turn, i) => (
          <div key={i + "-" + (i === newestIdx ? tickKey : 0)} style={{
            display: "grid", gridTemplateColumns: "auto 1fr", gap: 12, alignItems: "start",
            opacity: i === newestIdx ? 1 : 0.78,
          }}>
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", paddingTop: 2 }}>
              <span style={{ color: role[turn.role].color, fontSize: 11, lineHeight: 1 }}>{role[turn.role].glyph}</span>
              <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
                color: role[turn.role].color, letterSpacing: "0.08em", marginTop: 4, fontWeight: 600 }}>{role[turn.role].label}</span>
            </div>
            <div>
              <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
                color: "var(--q-t-3)", marginBottom: 3, letterSpacing: "0.06em" }}>
                {turn.t}
              </div>
              <div style={{ fontFamily: "var(--q-font-ui)", fontSize: "var(--q-fs-small)",
                color: "var(--q-t-1)", lineHeight: 1.55,
                animation: i === newestIdx ? "q-stream-in 1.6s ease-out both" : "none" }}>
                {turn.txt}
                {i === newestIdx && (
                  <span style={{ display: "inline-block", width: 7, height: 12, marginLeft: 4,
                    verticalAlign: "-2px", background: role[turn.role].color, animation: "q-blink 1s steps(2) infinite" }}/>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>
      <style>{`
        @keyframes q-stream-in { from { clip-path: inset(0 100% 0 0) } to { clip-path: inset(0 0 0 0) } }
        @keyframes q-blink { 50% { opacity: 0 } }
      `}</style>
      {!forceExpanded && (
        <button onClick={() => setExpanded((e) => !e)}
          style={{ marginTop: 12, fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
            color: "var(--q-t-3)", letterSpacing: "0.10em", textTransform: "uppercase" }}>
          {expanded ? "collapse" : `expand · ${debate.length - 3} earlier`}
        </button>
      )}
    </div>
  );
}

// ─── AgentFlow — animated pipeline diagram ───────────────────────
// Hermes (top, conductor) → V4 / Wheel / Shark (signal layer)
//   → Risk Governor (gate) → Quanta Core (executor) → broker
//                  ↑ ModelForge (training loop, dashed back into Shark)
// Packets travel along the edges; nodes pulse when "active".
function AgentFlow({ height = 320, width = 1540 }) {
  // node positions normalized to [0,1] then scaled
  const N = {
    hermes:  { x: 0.50, y: 0.10, label: "HERMES",       sub: "cron · 34 jobs",         tone: "accent" },
    v4:      { x: 0.18, y: 0.38, label: "V4",           sub: "MR + TF · 12 pairs",      tone: "ok" },
    wheel:   { x: 0.36, y: 0.38, label: "WHEEL",        sub: "short_put · cov_call",    tone: "ok" },
    shark:   { x: 0.54, y: 0.38, label: "SHARK",        sub: "bull · bear · arbiter",   tone: "ok" },
    forge:   { x: 0.78, y: 0.38, label: "MODELFORGE",   sub: "adapter v423",            tone: "off" },
    risk:    { x: 0.36, y: 0.68, label: "RISK GOVERNOR",sub: "cap · DD · weekly",       tone: "warn" }, // breach
    core:    { x: 0.62, y: 0.68, label: "QUANTA CORE",  sub: "exec · paper",            tone: "ok" },
    broker:  { x: 0.86, y: 0.83, label: "ALPACA · CCXT",sub: "paper · dry-run",         tone: "stale" },
  };
  const px = (k) => N[k].x * width;
  const py = (k) => N[k].y * height;
  // edges: each is from -> to, with optional dashed flag (training loop)
  const E = [
    ["hermes", "v4"], ["hermes", "wheel"], ["hermes", "shark"], ["hermes", "forge"],
    ["v4", "risk"], ["wheel", "risk"], ["shark", "risk"],
    ["risk", "core"], ["core", "broker"],
    ["forge", "shark", true],   // training loop
  ];

  const tone = {
    ok:     { fill: "var(--q-ok-soft)",     ring: "var(--q-ok-bord)",     dot: "var(--q-ok)",     text: "var(--q-ok)" },
    warn:   { fill: "var(--q-warn-soft)",   ring: "var(--q-warn-bord)",   dot: "var(--q-warn)",   text: "var(--q-warn)" },
    crit:   { fill: "var(--q-crit-soft)",   ring: "var(--q-crit-bord)",   dot: "var(--q-crit)",   text: "var(--q-crit)" },
    accent: { fill: "var(--q-accent-soft)", ring: "var(--q-accent-bord)", dot: "var(--q-accent)", text: "var(--q-accent)" },
    off:    { fill: "transparent",          ring: "var(--q-line)",        dot: "var(--q-t-4)",    text: "var(--q-t-3)" },
    stale:  { fill: "transparent",          ring: "var(--q-line)",        dot: "var(--q-stale)",  text: "var(--q-t-3)" },
  };

  return (
    <div style={{ width, height, position: "relative" }}>
      <svg width={width} height={height} style={{ position: "absolute", inset: 0, overflow: "visible" }}>
        <defs>
          <marker id="qf-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="rgba(255,255,255,0.18)" />
          </marker>
        </defs>
        {/* edges */}
        {E.map(([a, b, dashed], i) => {
          const x1 = px(a), y1 = py(a) + 22, x2 = px(b), y2 = py(b) - 22;
          // simple cubic bezier for nicer flow
          const dy = (y2 - y1) * 0.45;
          const d = `M ${x1} ${y1} C ${x1} ${y1 + dy}, ${x2} ${y2 - dy}, ${x2} ${y2}`;
          return (
            <g key={i}>
              <path d={d} fill="none" stroke="var(--q-line-strong)" strokeWidth="1"
                strokeDasharray={dashed ? "3 4" : undefined} markerEnd="url(#qf-arrow)" opacity={dashed ? 0.5 : 1}/>
              {/* animated packet */}
              {!dashed && (
                <circle r="2.5" fill={tone[N[a].tone].dot} style={{ filter: `drop-shadow(0 0 4px ${tone[N[a].tone].dot})` }}>
                  <animateMotion dur={(3 + (i % 3) * 0.6) + "s"} repeatCount="indefinite" begin={(i * 0.4) + "s"} path={d}/>
                </circle>
              )}
            </g>
          );
        })}
      </svg>
      {/* nodes */}
      {Object.entries(N).map(([k, n]) => {
        const t = tone[n.tone];
        const active = n.tone === "ok" || n.tone === "accent" || n.tone === "warn";
        return (
          <div key={k} style={{ position: "absolute", left: px(k) - 78, top: py(k) - 22, width: 156, textAlign: "center" }}>
            <div style={{ display: "inline-flex", alignItems: "center", gap: 8, padding: "9px 14px",
              background: "var(--q-bg-2)", borderRadius: 8, boxShadow: `inset 0 0 0 1px ${t.ring}, ${active ? `0 0 24px ${t.fill}` : "none"}`,
              fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)", letterSpacing: "0.10em",
              fontWeight: 600, color: t.text, textTransform: "uppercase" }}>
              <span style={{ width: 6, height: 6, borderRadius: 3, background: t.dot,
                boxShadow: active && n.tone !== "off" ? `0 0 6px ${t.dot}` : "none",
                animation: active ? "q-pulse-dot 2s ease-in-out infinite" : "none" }}/>
              {n.label}
            </div>
            <div style={{ marginTop: 5, fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
              color: "var(--q-t-3)", letterSpacing: "0.04em" }}>{n.sub}</div>
          </div>
        );
      })}
    </div>
  );
}

// ─── HermesHeartbeat — next-fire countdown + recent runs ─────────
function HermesHeartbeat({ next, recent, compact = false }) {
  // animate a 5-min ring (representing v4 tick cadence)
  const [pct, setPct] = useState(0.42);
  useEffect(() => {
    let raf, t0;
    const loop = (t) => {
      if (!t0) t0 = t;
      const p = ((t - t0) / 1000) % 60; // demo: full sweep every 60s
      setPct(p / 60);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, []);
  const R = 22, C = 2 * Math.PI * R;
  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 14 }}>
        <svg width="56" height="56" viewBox="0 0 56 56">
          <circle cx="28" cy="28" r={R} fill="none" stroke="var(--q-line)" strokeWidth="2"/>
          <circle cx="28" cy="28" r={R} fill="none" stroke="var(--q-accent)" strokeWidth="2"
            strokeDasharray={C} strokeDashoffset={C * (1 - pct)} strokeLinecap="round"
            transform="rotate(-90 28 28)" style={{ filter: "drop-shadow(0 0 4px var(--q-accent))" }}/>
          <text x="28" y="31" textAnchor="middle" fontFamily="var(--q-font-mono)" fontSize="9"
            fill="var(--q-t-2)" letterSpacing="0.08em">{Math.round((1-pct)*60)}s</text>
        </svg>
        <div>
          <div className="q-eyebrow">NEXT FIRE</div>
          <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-h2)", color: "var(--q-t-1)", fontWeight: 600 }}>
            {next[0][0]}
          </div>
          <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-micro)", color: "var(--q-t-2)" }}>
            in <span style={{ color: "var(--q-accent)" }}>{next[0][1]}</span> · {next[0][2]}
          </div>
        </div>
      </div>
      <div className="q-eyebrow" style={{ marginBottom: 8 }}>UPCOMING · NEXT 24H</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {next.slice(1, compact ? 4 : 6).map(([who, when, desc], i) => (
          <div key={i} style={{ display: "grid", gridTemplateColumns: "70px 1fr auto", gap: 12,
            fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)", alignItems: "baseline",
            padding: "5px 0", borderTop: "1px dashed var(--q-line-subtle)" }}>
            <span style={{ color: "var(--q-t-3)" }}>{when}</span>
            <span style={{ color: "var(--q-t-1)" }}>{who}</span>
            <span style={{ color: "var(--q-t-3)", fontSize: "var(--q-fs-pico)" }}>{desc}</span>
          </div>
        ))}
      </div>
      {!compact && (
        <>
          <div className="q-eyebrow" style={{ marginTop: 14, marginBottom: 8 }}>RECENT RUNS · LAST 30 MIN</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {recent.slice(0, 5).map(([who, status, when, desc], i) => (
              <div key={i} style={{ display: "grid", gridTemplateColumns: "12px 60px 1fr auto", gap: 10,
                fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)", alignItems: "center" }}>
                <StatusDot level={status === "ok" ? "ok" : status === "skip" ? "stale" : "warn"} size={5}/>
                <span style={{ color: "var(--q-t-3)" }}>{when}</span>
                <span style={{ color: "var(--q-t-2)" }}>{who}</span>
                <span style={{ color: "var(--q-t-3)", fontSize: "var(--q-fs-pico)" }}>{desc}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

// ─── Card primitive ──────────────────────────────────────────────
function Card({ title, eyebrow, right, children, padding = 18, style = {} }) {
  return (
    <section style={{
      background: "var(--q-bg-2)", borderRadius: 12, boxShadow: "var(--q-elev-flat)",
      padding, ...style,
    }}>
      {(title || eyebrow || right) && (
        <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between",
          marginBottom: 14, gap: 12 }}>
          <div>
            {eyebrow && <div className="q-eyebrow" style={{ marginBottom: 4 }}>{eyebrow}</div>}
            {title && <div className="q-h2">{title}</div>}
          </div>
          {right && <div style={{ flexShrink: 0 }}>{right}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

Object.assign(window, {
  Sparkline, StatusDot, FreshnessChip, Kbd,
  DrawdownRibbon, KillSwitch,
  V4TickTable, SharkDebate, AgentFlow, HermesHeartbeat,
  Card,
  fmtUSD, fmtPct,
});
