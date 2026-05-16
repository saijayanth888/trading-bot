// Direction B — "Telemetry"
// Datadog × Grafana × Sentry. Tighter rows, more components on the fold,
// slim sparklines everywhere, dense status grid. The same data as Operator
// but optimized for "show me everything at once without scrolling away".

const QT = window.Q;
const { Sparkline, StatusDot, FreshnessChip, Kbd, DrawdownRibbon, KillSwitch,
  V4TickTable, SharkDebate, AgentFlow, HermesHeartbeat, Card, fmtUSD, fmtPct } = window;

// ─── TopBar (compact) ────────────────────────────────────────────
function TopBarT() {
  return (
    <header style={{ background: "var(--q-bg-0)", padding: "10px 24px", display: "flex",
      alignItems: "center", gap: 18, boxShadow: "inset 0 -1px 0 var(--q-line)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
        <div style={{ width: 22, height: 22, borderRadius: 5,
          background: "linear-gradient(135deg, var(--q-accent), var(--q-accent-2))",
          boxShadow: "0 0 10px rgba(132,148,255,0.45)",
          display: "flex", alignItems: "center", justifyContent: "center" }}>
          <svg width="11" height="11" viewBox="0 0 14 14" fill="none" stroke="#fff" strokeWidth="2">
            <path d="M2 11 L7 3 L12 11 Z M2 11 H12"/>
          </svg>
        </div>
        <span style={{ fontFamily: "var(--q-font-ui)", fontWeight: 700, fontSize: 13,
          letterSpacing: "-0.02em", color: "var(--q-t-1)" }}>quanta</span>
        <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
          color: "var(--q-t-3)", letterSpacing: "0.08em" }}>v2.6·paper</span>
      </div>

      {/* compact pill chain */}
      <div style={{ display: "flex", alignItems: "center", gap: 0, padding: "3px",
        background: "var(--q-bg-1)", borderRadius: 6, boxShadow: "inset 0 0 0 1px var(--q-line)" }}>
        <PillBtn active>OVERVIEW</PillBtn>
        <PillBtn>V4</PillBtn>
        <PillBtn>WHEEL</PillBtn>
        <PillBtn>SHARK</PillBtn>
        <PillBtn>RISK</PillBtn>
        <PillBtn>HERMES</PillBtn>
        <PillBtn>FORGE</PillBtn>
      </div>

      {/* status banner — compact 1-line */}
      <div style={{ flex: 1, display: "flex", alignItems: "center", gap: 10, padding: "6px 12px",
        background: "var(--q-warn-soft)", borderRadius: 6,
        boxShadow: "inset 0 0 0 1px var(--q-warn-bord)" }}>
        <StatusDot level="warn" pulse size={6}/>
        <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
          color: "var(--q-warn)", fontWeight: 600, letterSpacing: "0.06em", textTransform: "uppercase" }}>
          1 CAP BREACH · 24H
        </span>
        <span style={{ color: "var(--q-t-2)", fontFamily: "var(--q-font-mono)",
          fontSize: "var(--q-fs-pico)" }}>BTC/USD · stake $66k vs cap $1.9k · 34× · realized −$1,057.32 · FRI 23:14 ET</span>
        <span style={{ flex: 1 }}/>
        <button style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
          color: "var(--q-warn)", letterSpacing: "0.08em", textTransform: "uppercase", fontWeight: 600 }}>
          AUDIT →
        </button>
      </div>

      {/* env */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, fontFamily: "var(--q-font-mono)",
        fontSize: "var(--q-fs-pico)", color: "var(--q-t-3)", letterSpacing: "0.06em" }}>
        <span style={{ color: "var(--q-pl-pos)" }}>WS · 160ms</span>
        <span>·</span><span>ET <span style={{ color: "var(--q-t-1)" }}>{QT.now.et}</span></span>
        <span>·</span><span>{QT.now.date}</span>
      </div>

      <KillSwitch compact/>
    </header>
  );
}

function PillBtn({ active, children }) {
  return (
    <button style={{ padding: "4px 10px", borderRadius: 4, fontFamily: "var(--q-font-mono)",
      fontSize: "var(--q-fs-pico)", letterSpacing: "0.10em", fontWeight: 600,
      color: active ? "var(--q-t-1)" : "var(--q-t-3)",
      background: active ? "var(--q-bg-3)" : "transparent" }}>{children}</button>
  );
}

// ─── 6-col KPI strip (replaces big monumental ribbon) ────────────
function KPIStrip() {
  const cells = [
    { label: "COMBINED EQUITY", value: `$${QT.equity.combined.toLocaleString("en-US", { minimumFractionDigits: 2 })}`,
      sub: `peak $${QT.equity.peak.toLocaleString("en-US", { minimumFractionDigits: 0 })}`, spark: QT.equityArc, sparkColor: "var(--q-accent)" },
    { label: "DAY P&L", value: fmtUSD(QT.equity.dayPL, { sign: true }),
      sub: fmtPct(QT.equity.dayPLpct) + " · SAT · CLOSED", spark: QT.intradayPL, sparkColor: "var(--q-t-3)", sparkDashed: true },
    { label: "DRAWDOWN", value: fmtPct(QT.equity.drawdown), valueColor: "var(--q-pl-neg)",
      ribbon: true },
    { label: "CRYPTO · V4", value: "trending_down", valueColor: "var(--q-pl-neg)", valueFs: 16,
      sub: "conf 96% · 0 open · 12s tick", chip: { mode: "live", age: "12s" } },
    { label: "STOCKS", value: "consolidating", valueColor: "var(--q-t-2)", valueFs: 16,
      sub: "conf 71% · 1 open · NYSE closed", chip: { mode: "closed", age: "17h" } },
    { label: "RISK GATES", value: "3 / 4 OK", valueColor: "var(--q-warn)", valueFs: 16,
      sub: "1 breach · cap · auto-fixed", spark: null,
      pips: [
        { c: "var(--q-crit)" }, { c: "var(--q-ok)" }, { c: "var(--q-ok)" }, { c: "var(--q-ok)" },
      ] },
  ];
  return (
    <section style={{ padding: "16px 24px", display: "grid", gridTemplateColumns: "1.4fr 1fr 1.3fr 1fr 1fr 1fr",
      gap: 0, borderBottom: "1px solid var(--q-line-subtle)" }}>
      {cells.map((c, i) => (
        <div key={i} style={{ padding: "8px 22px",
          borderRight: i < cells.length - 1 ? "1px solid var(--q-line-subtle)" : "none" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <span className="q-eyebrow">{c.label}</span>
            {c.chip && <FreshnessChip age={c.chip.age} mode={c.chip.mode}/>}
          </div>
          <div style={{ fontFamily: "var(--q-font-mono)", fontSize: c.valueFs || 26, fontWeight: 600,
            color: c.valueColor || "var(--q-t-1)", marginTop: 6, letterSpacing: "-0.02em", lineHeight: 1.1 }}>
            {c.value}
          </div>
          <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
            color: "var(--q-t-3)", marginTop: 4, letterSpacing: "0.04em" }}>{c.sub}</div>
          {c.spark && (
            <div style={{ marginTop: 6 }}>
              <Sparkline data={c.spark} width={170} height={22} color={c.sparkColor}
                fill={c.sparkDashed ? undefined : "rgba(132,148,255,0.08)"}/>
            </div>
          )}
          {c.ribbon && (
            <div style={{ marginTop: 8 }}>
              <DrawdownRibbon current={QT.equity.drawdown} pause={QT.equity.pause} kill={QT.equity.kill}
                width={210} height={20} compact/>
              <div style={{ display: "flex", justifyContent: "space-between", marginTop: 4,
                fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)", color: "var(--q-t-4)" }}>
                <span>0%</span><span style={{ color: "var(--q-warn)" }}>−3</span><span style={{ color: "var(--q-crit)" }}>−10</span>
              </div>
            </div>
          )}
          {c.pips && (
            <div style={{ marginTop: 8, display: "flex", gap: 4 }}>
              {c.pips.map((p, j) => (
                <div key={j} style={{ flex: 1, height: 6, borderRadius: 2, background: p.c, opacity: 0.85 }}/>
              ))}
            </div>
          )}
        </div>
      ))}
    </section>
  );
}

// ─── Agent Flow + Hermes (top row of dense grid) ─────────────────
function FlowRow() {
  return (
    <section style={{ padding: "16px 24px", display: "grid",
      gridTemplateColumns: "1.8fr 1fr", gap: 16 }}>
      <Card eyebrow="AGENT FLOW · LIVE PIPELINE" title="signal → gate → exec" padding={16}
        right={
          <div style={{ display: "flex", gap: 10, fontFamily: "var(--q-font-mono)",
            fontSize: "var(--q-fs-pico)", color: "var(--q-t-3)" }}>
            <span><StatusDot level="ok" size={5}/> 4 ACTIVE</span>
            <span><StatusDot level="warn" size={5}/> 1 GATE BREACH</span>
            <span><StatusDot level="stale" size={5}/> 1 IDLE</span>
          </div>
        }>
        <div style={{ padding: "4px 0" }}>
          <AgentFlow width={1020} height={240}/>
        </div>
      </Card>
      <Card eyebrow="HERMES · NEXT FIRE" title="Conductor" padding={16}>
        <HermesHeartbeat next={QT.hermes.nextFires} recent={QT.hermes.recentRuns} compact/>
      </Card>
    </section>
  );
}

// ─── V4 + Wheel + Shark (3-col agent grid) ───────────────────────
function AgentGrid() {
  return (
    <section style={{ padding: "0 24px 16px", display: "grid",
      gridTemplateColumns: "1.5fr 0.8fr 1fr", gap: 16 }}>
      {/* V4 — dense tick table */}
      <Card eyebrow="V4 · CRYPTO · 12 PAIRS · TICK 5MIN"
        title={<span>V4 <span style={{ color: "var(--q-t-3)", fontWeight: 400 }}>· {QT.v4.decisionsToday} decisions in last hour</span></span>}
        right={<FreshnessChip age="12s" mode="live"/>} padding={0}>
        <div style={{ padding: "0 6px 8px" }}>
          <V4TickTable pairs={QT.v4.pairs} dense/>
        </div>
        <div style={{ padding: "10px 16px", borderTop: "1px solid var(--q-line-subtle)",
          background: "var(--q-bg-1)", display: "flex", justifyContent: "space-between",
          fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)", color: "var(--q-t-3)" }}>
          <span>bb <span style={{ color: "var(--q-accent)" }}>{QT.v4.bbVotes}</span></span>
          <span>tf <span style={{ color: "var(--q-accent)" }}>{QT.v4.tfVotes}</span></span>
          <span>aligned <span style={{ color: "var(--q-pl-pos)" }}>{QT.v4.alignedVotes}</span></span>
          <span>blocked <span style={{ color: "var(--q-warn)" }}>{QT.v4.regimeBlocked}</span></span>
          <span>last fill <span style={{ color: "var(--q-t-1)" }}>{QT.v4.lastFill.sym}</span> <span style={{ color: "var(--q-pl-neg)" }}>{QT.v4.lastFill.side}</span> <span style={{ color: "var(--q-pl-pos)" }}>{fmtUSD(QT.v4.lastFill.pl, { sign: true })}</span> · 22h</span>
        </div>
      </Card>

      {/* Wheel — compact */}
      <Card eyebrow="WHEEL · STOCKS · OPTIONS" title="1 open · NVDA" padding={16}
        right={<FreshnessChip age="17h" mode="closed"/>}>
        <div style={{ padding: 14, borderRadius: 8, background: "var(--q-bg-1)",
          boxShadow: "inset 0 0 0 1px var(--q-line-subtle)" }}>
          <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
            <span style={{ fontFamily: "var(--q-font-mono)", fontSize: 22, fontWeight: 600,
              color: "var(--q-t-1)", letterSpacing: "-0.02em" }}>NVDA</span>
            <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
              color: "var(--q-pl-neg)", letterSpacing: "0.06em" }}>short_put · K=220</span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginTop: 10,
            fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)" }}>
            <div><span style={{ color: "var(--q-t-3)" }}>credit </span><span style={{ color: "var(--q-t-1)", fontWeight: 600 }}>$616.00</span></div>
            <div><span style={{ color: "var(--q-t-3)" }}>P&L </span><span style={{ color: "var(--q-pl-pos)", fontWeight: 600 }}>+$118.42</span></div>
            <div><span style={{ color: "var(--q-t-3)" }}>exp </span><span style={{ color: "var(--q-t-1)" }}>5/23</span></div>
            <div><span style={{ color: "var(--q-t-3)" }}>dte </span><span style={{ color: "var(--q-warn)" }}>5d</span></div>
          </div>
        </div>
        <div className="q-eyebrow" style={{ marginTop: 14, marginBottom: 6 }}>UNIVERSE · 15</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
          {QT.wheel.universe.map((s) => (
            <span key={s} style={{ padding: "2px 7px", borderRadius: 4,
              background: s === "NVDA" ? "var(--q-pl-pos-soft)" : "transparent",
              color: s === "NVDA" ? "var(--q-pl-pos)" : "var(--q-t-2)",
              fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
              fontWeight: 500, boxShadow: "inset 0 0 0 1px var(--q-line-subtle)" }}>{s}</span>
          ))}
        </div>
        <div className="q-eyebrow" style={{ marginTop: 14, marginBottom: 4 }}>NEXT</div>
        <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)", color: "var(--q-t-1)" }}>
          roll · <span style={{ color: "var(--q-accent)" }}>{QT.wheel.nextRoll}</span>
        </div>
      </Card>

      {/* Shark — debate */}
      <Card eyebrow="SHARK · STOCKS · LLM DEBATE"
        title={<span><span style={{ color: "var(--q-warn)" }}>WAITING</span> · MON 08:30</span>}
        right={<span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
          color: "var(--q-t-3)", letterSpacing: "0.06em" }}>qwen2.5:72b · local</span>} padding={16}>
        <div style={{ padding: "10px 12px", borderRadius: 8, background: "var(--q-pl-pos-soft)",
          boxShadow: "inset 0 0 0 1px rgba(94,193,138,0.30)", marginBottom: 12,
          display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ fontFamily: "var(--q-font-mono)", fontSize: 18, fontWeight: 600,
            color: "var(--q-pl-pos)" }}>PLTR</span>
          <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
            color: "var(--q-pl-pos)", letterSpacing: "0.08em" }}>LONG · FRI</span>
          <span style={{ flex: 1 }}/>
          <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-h2)",
            color: "var(--q-pl-pos)", fontWeight: 600 }}>+$84.18</span>
        </div>
        <div style={{ maxHeight: 230, overflow: "hidden" }}>
          <SharkDebate debate={QT.shark.debate}/>
        </div>
      </Card>
    </section>
  );
}

// ─── Risk gates + Recent runs + ModelForge (bottom row) ──────────
function BottomRow() {
  const gates = [
    { label: "SINGLE-NAME CAP", value: "3.42×", sub: "BTC/USD · FRI 23:14 ET", status: "breach", limit: "10% NAV" },
    { label: "DAILY LOSS",      value: "0.00%", sub: "no fills today",          status: "ok",     limit: "−3.0%" },
    { label: "WEEKLY TRADES",   value: "18/50", sub: "36% used",                 status: "ok",     limit: "50 max" },
    { label: "DRAWDOWN",        value: "−1.53%",sub: "8.47pp from kill",          status: "ok",     limit: "−10%" },
    { label: "WEEKLY DD",       value: "−1.53%",sub: "3.47pp headroom",           status: "ok",     limit: "−5%" },
  ];
  return (
    <section style={{ padding: "0 24px 16px", display: "grid",
      gridTemplateColumns: "1.4fr 1.1fr 0.9fr", gap: 16 }}>
      {/* Risk gates */}
      <Card eyebrow="RISK GOVERNOR · 5 GATES" title="What's stopping you" padding={16}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 8 }}>
          {gates.map((g) => (
            <div key={g.label} style={{ padding: 12, borderRadius: 6,
              background: g.status === "breach" ? "var(--q-crit-soft)" : "var(--q-bg-1)",
              boxShadow: `inset 0 0 0 1px ${g.status === "breach" ? "var(--q-crit-bord)" : "var(--q-line-subtle)"}` }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <StatusDot level={g.status === "breach" ? "crit" : "ok"} pulse={g.status === "breach"} size={5}/>
                <span className="q-eyebrow" style={{ color: g.status === "breach" ? "var(--q-crit)" : "var(--q-t-3)",
                  fontSize: 8, lineHeight: 1.2 }}>{g.label}</span>
              </div>
              <div style={{ fontFamily: "var(--q-font-mono)", fontSize: 17,
                color: g.status === "breach" ? "var(--q-crit)" : "var(--q-t-1)",
                fontWeight: 600, marginTop: 6, letterSpacing: "-0.01em" }}>{g.value}</div>
              <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
                color: "var(--q-t-3)", marginTop: 4 }}>limit {g.limit}</div>
              <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
                color: g.status === "breach" ? "var(--q-crit)" : "var(--q-t-2)",
                marginTop: 6, lineHeight: 1.4 }}>{g.sub}</div>
            </div>
          ))}
        </div>
      </Card>

      {/* Recent runs */}
      <Card eyebrow="HERMES · RECENT RUNS · LAST 30 MIN" title="Heartbeat trace" padding={16}>
        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          {QT.hermes.recentRuns.map(([who, status, when, desc], i) => (
            <div key={i} style={{ display: "grid", gridTemplateColumns: "12px 64px 1fr auto", gap: 10,
              fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)", alignItems: "center",
              padding: "4px 0", borderBottom: i < 6 ? "1px dashed var(--q-line-subtle)" : "none" }}>
              <StatusDot level={status === "ok" ? "ok" : status === "skip" ? "stale" : "warn"} size={5}/>
              <span style={{ color: "var(--q-t-3)" }}>{when}</span>
              <span style={{ color: "var(--q-t-1)" }}>{who}</span>
              <span style={{ color: "var(--q-t-3)" }}>{desc}</span>
            </div>
          ))}
        </div>
      </Card>

      {/* ModelForge + Integrations chip-cloud */}
      <Card eyebrow="MODELFORGE · TRAINING LOOP" title="adapter-v423" padding={16}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 12px",
          background: "var(--q-pl-pos-soft)", borderRadius: 6,
          boxShadow: "inset 0 0 0 1px rgba(94,193,138,0.30)" }}>
          <StatusDot level="ok" pulse size={6}/>
          <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)",
            color: "var(--q-t-1)", fontWeight: 600 }}>{QT.modelforge.champion}</span>
          <span style={{ flex: 1 }}/>
          <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
            color: "var(--q-pl-pos)", letterSpacing: "0.06em" }}>CHAMPION</span>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginTop: 12,
          fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)" }}>
          <div><span style={{ color: "var(--q-t-3)" }}>promoted </span><span style={{ color: "var(--q-t-1)" }}>{QT.modelforge.promotedAge}</span></div>
          <div><span style={{ color: "var(--q-t-3)" }}>parity </span><span style={{ color: "var(--q-pl-pos)" }}>{(QT.modelforge.parityPass*100).toFixed(1)}%</span></div>
          <div><span style={{ color: "var(--q-t-3)" }}>queue </span><span style={{ color: "var(--q-t-1)" }}>{QT.modelforge.queue}</span></div>
          <div><span style={{ color: "var(--q-t-3)" }}>API </span><span style={{ color: "var(--q-t-1)" }}>:8000</span></div>
        </div>
        <div className="q-eyebrow" style={{ marginTop: 14, marginBottom: 6 }}>EVAL QUEUE</div>
        {QT.modelforge.pendingEval.map((n) => (
          <div key={n} style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0",
            fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)" }}>
            <StatusDot level="stale" size={4}/>
            <span style={{ color: "var(--q-t-2)" }}>{n}</span>
            <span style={{ flex: 1 }}/>
            <span style={{ color: "var(--q-t-3)" }}>WAITING</span>
          </div>
        ))}
        <div className="q-eyebrow" style={{ marginTop: 12, marginBottom: 6 }}>INTEGRATIONS</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
          {["alpaca","polygon","ollama","anthropic","forge","core"].map((s) => (
            <span key={s} style={{ display: "inline-flex", alignItems: "center", gap: 4,
              padding: "2px 8px", borderRadius: 4, fontFamily: "var(--q-font-mono)",
              fontSize: "var(--q-fs-pico)", color: "var(--q-t-2)",
              boxShadow: "inset 0 0 0 1px var(--q-line-subtle)" }}>
              <StatusDot level={s === "anthropic" ? "stale" : "ok"} size={4} pulse={s !== "anthropic"}/>
              {s}
            </span>
          ))}
        </div>
      </Card>
    </section>
  );
}

// ─── Footer (compact telemetry bar) ──────────────────────────────
function FooterT() {
  return (
    <footer style={{ padding: "10px 24px", display: "flex", alignItems: "center",
      gap: 18, color: "var(--q-t-3)", fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
      letterSpacing: "0.06em", borderTop: "1px solid var(--q-line-subtle)",
      background: "var(--q-bg-0)" }}>
      <span><StatusDot level="ok" size={5} pulse/> CORE · uptime 4d 11h</span>
      <span>·</span>
      <span><StatusDot level="ok" size={5}/> WS · 160ms · 99.4%</span>
      <span>·</span>
      <span>HEAP <span style={{ color: "var(--q-t-1)" }}>412 MB</span> / 1024</span>
      <span>·</span>
      <span>CPU <span style={{ color: "var(--q-t-1)" }}>0.12</span></span>
      <span>·</span>
      <span>LOG <span style={{ color: "var(--q-t-1)" }}>1,842 ev/min</span></span>
      <span style={{ flex: 1 }}/>
      <span>OPERATOR · <span style={{ color: "var(--q-t-1)" }}>quant@quanta</span> · root</span>
      <span>·</span>
      <span>v2.6.0 · a4f81e2</span>
      <span>·</span>
      <Kbd>?</Kbd><span>shortcuts</span>
      <Kbd>⌘</Kbd>+<Kbd>K</Kbd>
    </footer>
  );
}

// ─── Compose ─────────────────────────────────────────────────────
function Telemetry() {
  return (
    <div className="q-root" style={{ width: 1600 }}>
      <TopBarT/>
      <KPIStrip/>
      <FlowRow/>
      <AgentGrid/>
      <BottomRow/>
      <FooterT/>
    </div>
  );
}

window.Telemetry = Telemetry;
