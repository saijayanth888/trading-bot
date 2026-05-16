// Direction A — "Operator"
// Stripe Dashboard × Linear × Vercel. Generous whitespace, monumental numbers,
// decisive hierarchy via type weight + scale. Optimized for the under-1s scan.

// shared primitives (Sparkline, StatusDot, FreshnessChip, Kbd, DrawdownRibbon,
// KillSwitch, V4TickTable, SharkDebate, AgentFlow, HermesHeartbeat, Card,
// fmtUSD, fmtPct) live on window as top-level globals from shared.jsx — no
// need to destructure. Same for Q (the shared mock state).
const Q = window.Q;

// ─── TopBar ──────────────────────────────────────────────────────
function TopBar() {
  const bannerStyle = {
    ok:   { dot: "var(--q-ok)",   bg: "var(--q-ok-soft)",   bord: "var(--q-ok-bord)",   text: "var(--q-ok)" },
    warn: { dot: "var(--q-warn)", bg: "var(--q-warn-soft)", bord: "var(--q-warn-bord)", text: "var(--q-warn)" },
    crit: { dot: "var(--q-crit)", bg: "var(--q-crit-soft)", bord: "var(--q-crit-bord)", text: "var(--q-crit)" },
  }[Q.banner.level];
  return (
    <header style={{ background: "var(--q-bg-0)", padding: "16px 40px", display: "flex",
      alignItems: "center", gap: 32, boxShadow: "inset 0 -1px 0 var(--q-line)" }}>
      {/* brand */}
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <div style={{ width: 30, height: 30, borderRadius: 7,
          background: "linear-gradient(135deg, var(--q-accent), var(--q-accent-2))",
          boxShadow: "0 0 14px rgba(132,148,255,0.45)",
          display: "flex", alignItems: "center", justifyContent: "center" }}>
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="#fff" strokeWidth="1.8">
            <path d="M2 11 L7 3 L12 11 Z M2 11 H12"/>
          </svg>
        </div>
        <span style={{ fontFamily: "var(--q-font-ui)", fontWeight: 700, fontSize: 16,
          letterSpacing: "-0.02em", color: "var(--q-t-1)" }}>quanta</span>
        <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
          color: "var(--q-t-3)", letterSpacing: "0.08em" }}>v2.6 · paper · dry-run</span>
      </div>

      {/* status banner — 1 line */}
      <div style={{ flex: 1, display: "flex", alignItems: "center", gap: 12, padding: "8px 14px",
        background: bannerStyle.bg, borderRadius: 8, boxShadow: `inset 0 0 0 1px ${bannerStyle.bord}` }}>
        <StatusDot level={Q.banner.level} pulse size={8}/>
        <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-micro)",
          color: bannerStyle.text, fontWeight: 600, letterSpacing: "0.04em", textTransform: "uppercase" }}>
          {Q.banner.headline}
        </span>
        <span style={{ flex: 1, color: "var(--q-t-2)", fontFamily: "var(--q-font-mono)",
          fontSize: "var(--q-fs-small)" }}>· {Q.banner.sub}</span>
        <button style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
          color: bannerStyle.text, letterSpacing: "0.10em", textTransform: "uppercase", fontWeight: 600 }}>
          REVIEW →
        </button>
      </div>

      {/* clock */}
      <div style={{ textAlign: "right" }}>
        <div style={{ fontFamily: "var(--q-font-mono)", fontSize: 15, fontWeight: 600,
          color: "var(--q-t-1)", letterSpacing: "0.04em" }}>{Q.now.et}</div>
        <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
          color: "var(--q-t-3)", letterSpacing: "0.08em" }}>{Q.now.date}</div>
      </div>

      <KillSwitch />
    </header>
  );
}

// ─── Equity Ribbon ───────────────────────────────────────────────
function EquityRibbon() {
  const dayPos = Q.equity.dayPL > 0, dayNeg = Q.equity.dayPL < 0;
  return (
    <section style={{ padding: "40px 40px 32px", display: "grid",
      gridTemplateColumns: "1.2fr 1fr 1.3fr", gap: 56, alignItems: "flex-end" }}>
      {/* combined equity */}
      <div>
        <div className="q-eyebrow" style={{ marginBottom: 14 }}>
          COMBINED EQUITY · USD ·
          <FreshnessChip age={Q.sides.crypto.lastTick} mode="live"/>
        </div>
        <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-mono-xl)",
          fontWeight: 500, color: "var(--q-t-1)", letterSpacing: "-0.04em", lineHeight: 1,
          fontVariantNumeric: "tabular-nums" }}>
          ${Q.equity.combined.toLocaleString("en-US", { minimumFractionDigits: 2 })}
        </div>
        <div style={{ marginTop: 14, display: "flex", gap: 20, fontFamily: "var(--q-font-mono)",
          fontSize: "var(--q-fs-small)", color: "var(--q-t-2)" }}>
          <span>crypto <span style={{ color: "var(--q-t-1)" }}>${Q.equity.cryptoEquity.toLocaleString("en-US", { minimumFractionDigits: 2 })}</span></span>
          <span style={{ color: "var(--q-t-4)" }}>·</span>
          <span>stocks <span style={{ color: "var(--q-t-1)" }}>${Q.equity.stocksEquity.toLocaleString("en-US", { minimumFractionDigits: 2 })}</span></span>
          <span style={{ color: "var(--q-t-4)" }}>·</span>
          <span>peak <span style={{ color: "var(--q-t-2)" }}>${Q.equity.peak.toLocaleString("en-US", { minimumFractionDigits: 2 })}</span></span>
        </div>
      </div>

      {/* day P&L */}
      <div>
        <div className="q-eyebrow" style={{ marginBottom: 14 }}>
          DAY P&amp;L · SAT · MKT CLOSED
          <FreshnessChip age={Q.sides.stocks.lastTick} mode="closed"/>
        </div>
        <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-mono-xl)",
          fontWeight: 500, letterSpacing: "-0.04em", lineHeight: 1,
          color: dayPos ? "var(--q-pl-pos)" : dayNeg ? "var(--q-pl-neg)" : "var(--q-t-2)" }}>
          {fmtUSD(Q.equity.dayPL, { sign: true })}
        </div>
        <div style={{ marginTop: 14, display: "flex", gap: 20, fontFamily: "var(--q-font-mono)",
          fontSize: "var(--q-fs-small)", color: "var(--q-t-2)" }}>
          <span>{fmtPct(Q.equity.dayPLpct)}</span>
          <span style={{ color: "var(--q-t-4)" }}>·</span>
          <span>crypto <span style={{ color: "var(--q-t-2)" }}>{fmtUSD(Q.equity.cryptoPL, { sign: true })}</span></span>
          <span style={{ color: "var(--q-t-4)" }}>·</span>
          <span>stocks <span style={{ color: "var(--q-t-2)" }}>{fmtUSD(Q.equity.stocksPL, { sign: true })}</span></span>
        </div>
      </div>

      {/* drawdown */}
      <div>
        <div className="q-eyebrow" style={{ marginBottom: 14 }}>
          DRAWDOWN FROM PEAK · KILL @ {fmtPct(Q.equity.kill)}
        </div>
        <div style={{ display: "flex", alignItems: "flex-end", gap: 28, marginBottom: 18 }}>
          <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-mono-lg)",
            fontWeight: 500, color: "var(--q-pl-neg)", letterSpacing: "-0.03em", lineHeight: 1,
            fontVariantNumeric: "tabular-nums" }}>
            {fmtPct(Q.equity.drawdown)}
          </div>
          <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)",
            color: "var(--q-t-3)", paddingBottom: 2 }}>
            {fmtUSD(Q.equity.combined - Q.equity.peak, { sign: true })} from peak
          </div>
        </div>
        <DrawdownRibbon current={Q.equity.drawdown} pause={Q.equity.pause} kill={Q.equity.kill} width={460}/>
      </div>
    </section>
  );
}

// ─── Regime / Side Chips ─────────────────────────────────────────
function RegimeStrip() {
  const Chip = ({ side, regime, conf, ws, lastTick, open }) => {
    const wsMode = ws === "live" ? "live" : ws === "closed" ? "closed" : "stale";
    const regimeColor = regime.includes("down") ? "var(--q-pl-neg)"
                      : regime.includes("up")   ? "var(--q-pl-pos)"
                      : "var(--q-t-2)";
    return (
      <div style={{ flex: 1, padding: "18px 22px", background: "var(--q-bg-2)",
        borderRadius: 12, boxShadow: "var(--q-elev-flat)", display: "flex", alignItems: "center", gap: 24 }}>
        <div style={{ width: 6, height: 44, borderRadius: 3, background: regimeColor, opacity: 0.6 }}/>
        <div style={{ flex: 1 }}>
          <div className="q-eyebrow">{side} · REGIME</div>
          <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginTop: 4 }}>
            <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-mono-md)",
              color: regimeColor, fontWeight: 600, letterSpacing: "-0.01em" }}>{regime}</span>
            <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)",
              color: "var(--q-t-3)" }}>conf {(conf * 100).toFixed(0)}%</span>
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <FreshnessChip age={lastTick} mode={wsMode}/>
          <div style={{ marginTop: 6, fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)",
            color: "var(--q-t-2)" }}><span style={{ color: "var(--q-t-1)" }}>{open}</span> open</div>
        </div>
      </div>
    );
  };
  return (
    <section style={{ padding: "0 40px 32px", display: "flex", gap: 18 }}>
      <Chip side="CRYPTO · V4" regime={Q.sides.crypto.regime} conf={Q.sides.crypto.regimeConf}
        ws={Q.sides.crypto.ws} lastTick={Q.sides.crypto.lastTick} open={Q.sides.crypto.openPos}/>
      <Chip side="STOCKS · WHEEL+SHARK" regime={Q.sides.stocks.regime} conf={Q.sides.stocks.regimeConf}
        ws={Q.sides.stocks.ws} lastTick={Q.sides.stocks.lastTick} open={Q.sides.stocks.openPos}/>
    </section>
  );
}

// ─── Cap Breach incident ─────────────────────────────────────────
function CapBreachCard() {
  return (
    <section style={{ margin: "0 40px 40px", padding: "22px 28px", borderRadius: 12,
      background: "var(--q-crit-soft)", boxShadow: "inset 0 0 0 1px var(--q-crit-bord)",
      display: "grid", gridTemplateColumns: "auto 1fr auto", gap: 28, alignItems: "center" }}>
      <div style={{ width: 4, height: 60, borderRadius: 2, background: "var(--q-crit)" }}/>
      <div>
        <div className="q-eyebrow" style={{ color: "var(--q-crit)" }}>SINGLE-NAME CAP BREACH · LAST 24H</div>
        <div style={{ fontFamily: "var(--q-font-ui)", fontSize: 19, fontWeight: 600, color: "var(--q-t-1)",
          marginTop: 6, letterSpacing: "-0.01em" }}>
          BTC/USD position sized 34× over per-asset cap before risk-governor at-entry check landed
        </div>
        <div style={{ display: "flex", gap: 24, marginTop: 10, fontFamily: "var(--q-font-mono)",
          fontSize: "var(--q-fs-small)", color: "var(--q-t-2)" }}>
          <span>stake <span style={{ color: "var(--q-t-1)" }}>$66,041.20</span></span>
          <span>cap <span style={{ color: "var(--q-t-1)" }}>$1,941.80</span></span>
          <span>realized <span style={{ color: "var(--q-pl-neg)" }}>−$1,057.32</span></span>
          <span>fired <span style={{ color: "var(--q-t-1)" }}>FRI 23:14 ET</span></span>
          <span>fix <span style={{ color: "var(--q-pl-pos)" }}>shipped FRI 23:48 · #B12</span></span>
        </div>
      </div>
      <div style={{ display: "flex", gap: 10 }}>
        <button style={{ padding: "9px 14px", borderRadius: 6, fontFamily: "var(--q-font-mono)",
          fontSize: "var(--q-fs-pico)", letterSpacing: "0.10em", textTransform: "uppercase",
          color: "var(--q-t-1)", boxShadow: "inset 0 0 0 1px var(--q-line-strong)", fontWeight: 600 }}>
          OPEN AUDIT
        </button>
        <button style={{ padding: "9px 14px", borderRadius: 6, fontFamily: "var(--q-font-mono)",
          fontSize: "var(--q-fs-pico)", letterSpacing: "0.10em", textTransform: "uppercase",
          color: "var(--q-t-3)", fontWeight: 600 }}>
          ACK
        </button>
      </div>
    </section>
  );
}

// ─── Agent Flow Card ─────────────────────────────────────────────
function AgentFlowCard() {
  return (
    <section style={{ margin: "0 40px 40px" }}>
      <Card
        eyebrow="AGENT FLOW · 5 AGENTS · 1 BREACH ON THE GATE"
        title="The pipeline, breathing"
        right={
          <div style={{ display: "flex", gap: 16, fontFamily: "var(--q-font-mono)",
            fontSize: "var(--q-fs-pico)", color: "var(--q-t-3)", letterSpacing: "0.06em" }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <StatusDot level="ok" size={5}/> SIGNAL
            </span>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <StatusDot level="warn" size={5}/> GATE · 1 BREACH
            </span>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <StatusDot level="stale" size={5}/> EXECUTOR · MKT CLOSED
            </span>
          </div>
        }
        padding={28}
      >
        <div style={{ display: "flex", justifyContent: "center", padding: "10px 0 4px" }}>
          <AgentFlow width={1400} height={300}/>
        </div>
        <div style={{ marginTop: 14, padding: "12px 16px", background: "var(--q-bg-1)", borderRadius: 8,
          boxShadow: "inset 0 0 0 1px var(--q-line-subtle)", display: "grid",
          gridTemplateColumns: "auto 1fr auto", gap: 16, alignItems: "center" }}>
          <span className="q-eyebrow" style={{ color: "var(--q-accent)" }}>NOW</span>
          <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)", color: "var(--q-t-2)" }}>
            <span style={{ color: "var(--q-t-1)" }}>hermes</span> just fired <span style={{ color: "var(--q-accent)" }}>v4.crypto.tick</span> · 12 decisions returned · 0 fills · regime gate blocked 12/27 in last hour · risk.governor holding 1 cap breach <span style={{ color: "var(--q-warn)" }}>(BTC/USD, 34×)</span> · executor sleeping until MON 09:30 ET
          </span>
          <Kbd>SPACE</Kbd>
        </div>
      </Card>
    </section>
  );
}

// ─── Agent Strips ────────────────────────────────────────────────
function V4Card() {
  return (
    <Card
      eyebrow={`V4 · CRYPTO · MEAN-REVERSION + TREND-FOLLOW · TICK 5MIN`}
      title="12 pairs · last fill XRP/USD SELL 22h ago"
      right={<FreshnessChip age={Q.sides.crypto.lastTick} mode="live"/>}
      padding={0}
      style={{ overflow: "hidden" }}
    >
      <div style={{ display: "grid", gridTemplateColumns: "1fr 280px" }}>
        <div style={{ padding: "0 8px 8px" }}>
          <V4TickTable pairs={Q.v4.pairs}/>
        </div>
        <aside style={{ padding: "22px 24px", borderLeft: "1px solid var(--q-line-subtle)",
          background: "var(--q-bg-1)" }}>
          <div className="q-eyebrow" style={{ marginBottom: 10 }}>LAST HOUR · {Q.v4.decisionsToday} TICKS</div>
          <Stat label="LONG votes"  value={Q.v4.bbVotes + " · MR"}      color="var(--q-pl-pos)"/>
          <Stat label="SHORT votes" value={"3 · MR"}                     color="var(--q-pl-neg)"/>
          <Stat label="TF aligned"  value={Q.v4.alignedVotes + " / " + Q.v4.tfVotes} color="var(--q-accent)"/>
          <Stat label="regime gate" value={Q.v4.regimeBlocked + " blocked"} color="var(--q-warn)"/>
          <div style={{ height: 16 }}/>
          <div className="q-eyebrow" style={{ marginBottom: 10 }}>LAST FILL</div>
          <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-h2)",
            color: "var(--q-t-1)", fontWeight: 600 }}>{Q.v4.lastFill.sym}</div>
          <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)",
            color: "var(--q-t-2)", marginTop: 4 }}>
            <span style={{ color: "var(--q-pl-neg)" }}>{Q.v4.lastFill.side}</span> {Q.v4.lastFill.qty} @ {Q.v4.lastFill.fillPx}
          </div>
          <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)",
            color: "var(--q-pl-pos)", marginTop: 4 }}>{fmtUSD(Q.v4.lastFill.pl, { sign: true })} realized</div>
          <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
            color: "var(--q-t-3)", marginTop: 6, letterSpacing: "0.06em" }}>{Q.v4.lastFill.age} ago</div>
        </aside>
      </div>
    </Card>
  );
}

function Stat({ label, value, color }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline",
      padding: "5px 0", fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)" }}>
      <span style={{ color: "var(--q-t-3)" }}>{label}</span>
      <span style={{ color: color || "var(--q-t-1)", fontWeight: 600 }}>{value}</span>
    </div>
  );
}

function WheelCard() {
  const p = Q.wheel.open[0];
  return (
    <Card
      eyebrow="WHEEL · STOCKS · SHORT_PUT + COVERED_CALL"
      title={`1 open · ${Q.wheel.universe.length} universe · next roll ${Q.wheel.nextRoll}`}
      right={<FreshnessChip age={Q.wheel.lastSnap} mode="closed"/>}
    >
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 24 }}>
        <div style={{ padding: 18, borderRadius: 10, background: "var(--q-bg-1)",
          boxShadow: "inset 0 0 0 1px var(--q-line-subtle)" }}>
          <div className="q-eyebrow" style={{ marginBottom: 10 }}>OPEN · 1 CONTRACT</div>
          <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
            <span style={{ fontFamily: "var(--q-font-mono)", fontSize: 28, fontWeight: 600,
              color: "var(--q-t-1)", letterSpacing: "-0.02em" }}>NVDA</span>
            <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)",
              color: "var(--q-pl-neg)", fontWeight: 600 }}>short_put</span>
            <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)",
              color: "var(--q-t-2)" }}>K={p[2]}</span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginTop: 14 }}>
            <div>
              <div className="q-eyebrow">CREDIT</div>
              <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-h1)",
                color: "var(--q-t-1)", fontWeight: 600, marginTop: 2 }}>${p[4].toFixed(2)}</div>
            </div>
            <div>
              <div className="q-eyebrow">UNREAL P&amp;L</div>
              <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-h1)",
                color: "var(--q-pl-pos)", fontWeight: 600, marginTop: 2 }}>{fmtUSD(p[6], { sign: true })}</div>
            </div>
            <div>
              <div className="q-eyebrow">EXPIRES</div>
              <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-h2)",
                color: "var(--q-t-1)", fontWeight: 600, marginTop: 2 }}>{p[5]}</div>
            </div>
            <div>
              <div className="q-eyebrow">DTE</div>
              <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-h2)",
                color: "var(--q-warn)", fontWeight: 600, marginTop: 2 }}>{p[7]}d</div>
            </div>
          </div>
        </div>

        <div>
          <div className="q-eyebrow" style={{ marginBottom: 10 }}>UNIVERSE · 15 NAMES</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {Q.wheel.universe.map((s) => (
              <span key={s} style={{ padding: "5px 10px", borderRadius: 5,
                background: s === "NVDA" ? "var(--q-pl-pos-soft)" : "var(--q-bg-1)",
                color: s === "NVDA" ? "var(--q-pl-pos)" : "var(--q-t-2)",
                fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)",
                fontWeight: 500, boxShadow: "inset 0 0 0 1px var(--q-line-subtle)" }}>{s}</span>
            ))}
          </div>
          <div className="q-eyebrow" style={{ marginTop: 18, marginBottom: 10 }}>NEXT ACTION</div>
          <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-body)",
            color: "var(--q-t-1)" }}>
            roll · <span style={{ color: "var(--q-accent)" }}>{Q.wheel.nextRoll}</span>
            <div style={{ color: "var(--q-t-3)", fontSize: "var(--q-fs-pico)", marginTop: 2, letterSpacing: "0.06em" }}>
              SCAN PUT STRIKES Δ0.20–0.30, DTE 7–14, OI &gt; 1000
            </div>
          </div>
        </div>
      </div>
    </Card>
  );
}

function SharkCard() {
  return (
    <Card
      eyebrow="SHARK · STOCKS · LLM DEBATE · 5 PHASES/DAY"
      title="Waiting on MON 08:30 · pre-market scan"
      right={
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <FreshnessChip age="WAIT" mode="closed"/>
        </div>
      }
    >
      <div style={{ display: "grid", gridTemplateColumns: "1.6fr 1fr", gap: 28 }}>
        <div>
          <div className="q-eyebrow" style={{ marginBottom: 12 }}>
            LAST DEBATE · FRI 08:31 · {Q.shark.debate.length} TURNS · 4 ELIDED → 4 SHOWN
          </div>
          <SharkDebate debate={Q.shark.debate}/>
        </div>
        <div>
          <div className="q-eyebrow" style={{ marginBottom: 10 }}>CONFIRMED PICK · FRI</div>
          <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
            <span style={{ fontFamily: "var(--q-font-mono)", fontSize: 36, fontWeight: 600,
              color: "var(--q-pl-pos)", letterSpacing: "-0.02em" }}>{Q.shark.lastPick.sym}</span>
            <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)",
              color: "var(--q-pl-pos)", fontWeight: 600 }}>{Q.shark.lastPick.side}</span>
          </div>
          <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-h2)",
            color: "var(--q-pl-pos)", fontWeight: 600, marginTop: 4 }}>
            {fmtUSD(Q.shark.lastPick.pl, { sign: true })} <span style={{ color: "var(--q-t-3)", fontSize: 12 }}>realized · closed_eod</span>
          </div>
          <div style={{ height: 18 }}/>
          <Stat label="phase"   value={"WAITING"} color="var(--q-warn)"/>
          <Stat label="next"    value={"MON 08:30"} color="var(--q-accent)"/>
          <Stat label="model"   value={"qwen2.5:72b"} color="var(--q-t-1)"/>
          <Stat label="fallback" value={"sonnet-4-6"} color="var(--q-t-2)"/>
          <Stat label="14d picks" value={"4 / 11"} color="var(--q-t-1)"/>
          <div style={{ height: 14 }}/>
          <div style={{ padding: "10px 12px", background: "var(--q-bg-1)", borderRadius: 6,
            boxShadow: "inset 0 0 0 1px var(--q-line-subtle)",
            fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
            color: "var(--q-t-3)", letterSpacing: "0.04em", lineHeight: 1.55 }}>
            // GROUNDED IN GRAPH · 7 SAFEGUARDS<br/>
            // tft_up gate · ivc gate · catalyst windowed · risk pre-cap
          </div>
        </div>
      </div>
    </Card>
  );
}

function HermesCard() {
  return (
    <Card
      eyebrow="HERMES · CRON · 34 JOBS · CONDUCTOR"
      title="The heartbeat"
    >
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1.4fr", gap: 32 }}>
        <HermesHeartbeat next={Q.hermes.nextFires} recent={Q.hermes.recentRuns}/>
        <div>
          <div className="q-eyebrow" style={{ marginBottom: 10 }}>WEEKLY HEATMAP · 168 SLOTS · GREEN=FIRED</div>
          <HeatmapWeek/>
        </div>
      </div>
    </Card>
  );
}

function HeatmapWeek() {
  // 7 rows × 24 cols, pseudo-random density for visual
  const rows = ["MON","TUE","WED","THU","FRI","SAT","SUN"];
  const rand = (r, c) => {
    const v = Math.sin(r * 31 + c * 7) * 0.5 + 0.5;
    return v;
  };
  return (
    <div>
      <div style={{ display: "grid", gridTemplateColumns: "30px repeat(24, 1fr)", gap: 3,
        fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)", color: "var(--q-t-4)" }}>
        <span/>
        {Array.from({ length: 24 }).map((_, c) => (
          <span key={c} style={{ textAlign: "center", paddingBottom: 3 }}>{c % 4 === 0 ? c : ""}</span>
        ))}
        {rows.map((r, ri) => (
          <React.Fragment key={r}>
            <span style={{ alignSelf: "center" }}>{r}</span>
            {Array.from({ length: 24 }).map((_, c) => {
              const v = rand(ri, c);
              const isWeekend = ri >= 5;
              const fired = v > 0.32;
              const intensity = fired ? Math.min(1, (v - 0.32) / 0.5) : 0;
              const bg = isWeekend
                ? `rgba(86,180,233,${intensity * 0.55})`  // weekend = crypto only (blue)
                : `rgba(94,193,138,${intensity * 0.7})`;  // weekday = all (green)
              return (
                <div key={c} style={{ aspectRatio: "1/1.2", borderRadius: 2,
                  background: fired ? bg : "var(--q-bg-1)",
                  boxShadow: "inset 0 0 0 1px var(--q-line-subtle)" }}/>
              );
            })}
          </React.Fragment>
        ))}
      </div>
      <div style={{ display: "flex", gap: 18, marginTop: 12, fontFamily: "var(--q-font-mono)",
        fontSize: "var(--q-fs-pico)", color: "var(--q-t-3)", letterSpacing: "0.06em" }}>
        <span><span style={{ display: "inline-block", width: 10, height: 10, background: "rgba(94,193,138,0.7)", borderRadius: 2, verticalAlign: "-2px", marginRight: 6 }}/>WEEKDAY · all agents</span>
        <span><span style={{ display: "inline-block", width: 10, height: 10, background: "rgba(86,180,233,0.55)", borderRadius: 2, verticalAlign: "-2px", marginRight: 6 }}/>WEEKEND · crypto only</span>
        <span style={{ marginLeft: "auto" }}>now ↘ <span style={{ color: "var(--q-accent)" }}>SAT 13:29</span></span>
      </div>
    </div>
  );
}

// ─── Risk Gates ──────────────────────────────────────────────────
function RiskGates() {
  const gates = [
    { label: "SINGLE-NAME CAP",   limit: "10% NAV",       value: "3.42× peak 24h", status: "breach", note: "BTC/USD · FRI 23:14 ET · fix shipped" },
    { label: "DAILY LOSS HALT",   limit: "−3.0%",          value: "0.00%",           status: "ok",     note: "no trades · markets closed" },
    { label: "WEEKLY TRADES",     limit: "50 max",         value: "18 / 50",        status: "ok",     note: "36% budget used" },
    { label: "DRAWDOWN CIRCUIT",  limit: "−10% kill · −3% pause", value: "−1.53%", status: "ok",     note: "8.47pp from kill" },
  ];
  const cs = { ok: "var(--q-ok)", breach: "var(--q-crit)", warn: "var(--q-warn)" };
  return (
    <section style={{ margin: "0 40px 40px" }}>
      <Card eyebrow="RISK GOVERNOR · 4 GATES · AT-ENTRY + PER-HEARTBEAT" title="What's stopping you from blowing up">
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 18 }}>
          {gates.map((g) => (
            <div key={g.label} style={{ padding: 18, borderRadius: 10,
              background: g.status === "breach" ? "var(--q-crit-soft)" : "var(--q-bg-1)",
              boxShadow: `inset 0 0 0 1px ${g.status === "breach" ? "var(--q-crit-bord)" : "var(--q-line-subtle)"}` }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <StatusDot level={g.status === "breach" ? "crit" : "ok"} pulse={g.status === "breach"} size={6}/>
                <span className="q-eyebrow" style={{ color: cs[g.status] }}>{g.label}</span>
              </div>
              <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-h1)",
                color: g.status === "breach" ? "var(--q-crit)" : "var(--q-t-1)",
                fontWeight: 600, marginTop: 8, letterSpacing: "-0.01em" }}>{g.value}</div>
              <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
                color: "var(--q-t-3)", marginTop: 4, letterSpacing: "0.06em" }}>limit · {g.limit}</div>
              <div style={{ marginTop: 10, fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
                color: g.status === "breach" ? "var(--q-crit)" : "var(--q-t-2)",
                lineHeight: 1.5 }}>{g.note}</div>
            </div>
          ))}
        </div>
      </Card>
    </section>
  );
}

// ─── Integrations + ModelForge mini ──────────────────────────────
function IntegrationsRow() {
  const items = [
    { label: "Alpaca Paper",     state: "ok",     detail: "WS · 160ms · synced 17h" },
    { label: "Polygon WS",       state: "ok",     detail: "live · 12s lag · 12 pairs" },
    { label: "Ollama · qwen2.5", state: "ok",     detail: "local · 28GB · idle" },
    { label: "Anthropic fallback",state:"off",    detail: "armed · sonnet-4-6 · unused" },
    { label: "ModelForge API",   state: "ok",     detail: "95 endpoints · :8000" },
    { label: "Quanta Core",      state: "ok",     detail: "container · uptime 4d 11h" },
  ];
  const css = { ok: "var(--q-ok)", warn: "var(--q-warn)", crit: "var(--q-crit)", off: "var(--q-stale)" };
  return (
    <section style={{ margin: "0 40px 40px" }}>
      <Card eyebrow="INTEGRATIONS · 6 SOURCES" title="What's connected">
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 16 }}>
          {items.map((it) => (
            <div key={it.label} style={{ padding: "14px 18px", borderRadius: 8,
              background: "var(--q-bg-1)", boxShadow: "inset 0 0 0 1px var(--q-line-subtle)",
              display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <StatusDot level={it.state === "off" ? "stale" : it.state} pulse={it.state === "ok"} size={6}/>
                  <span style={{ fontFamily: "var(--q-font-ui)", fontWeight: 500,
                    fontSize: "var(--q-fs-body)", color: "var(--q-t-1)" }}>{it.label}</span>
                </div>
                <div style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
                  color: "var(--q-t-3)", marginTop: 4, letterSpacing: "0.04em" }}>{it.detail}</div>
              </div>
              <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
                color: css[it.state], letterSpacing: "0.10em", textTransform: "uppercase", fontWeight: 600 }}>
                {it.state.toUpperCase()}
              </span>
            </div>
          ))}
        </div>
      </Card>
    </section>
  );
}

function ModelForgeStrip() {
  return (
    <section style={{ margin: "0 40px 40px", padding: "16px 22px", background: "var(--q-bg-2)",
      borderRadius: 10, boxShadow: "var(--q-elev-flat)", display: "flex", alignItems: "center", gap: 20 }}>
      <StatusDot level="ok" size={6}/>
      <span className="q-eyebrow">MODELFORGE</span>
      <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-body)", color: "var(--q-t-1)" }}>
        champion <span style={{ fontWeight: 600 }}>{Q.modelforge.champion}</span>
      </span>
      <span style={{ color: "var(--q-t-4)" }}>·</span>
      <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)", color: "var(--q-t-2)" }}>
        promoted <span style={{ color: "var(--q-t-1)" }}>{Q.modelforge.promotedAge}</span> ago
      </span>
      <span style={{ color: "var(--q-t-4)" }}>·</span>
      <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)", color: "var(--q-t-2)" }}>
        parity <span style={{ color: "var(--q-pl-pos)" }}>{(Q.modelforge.parityPass * 100).toFixed(1)}%</span>
      </span>
      <span style={{ color: "var(--q-t-4)" }}>·</span>
      <span style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-small)", color: "var(--q-t-2)" }}>
        {Q.modelforge.queue} in eval queue
      </span>
      <span style={{ flex: 1 }}/>
      <button style={{ fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
        color: "var(--q-accent)", letterSpacing: "0.10em", textTransform: "uppercase", fontWeight: 600 }}>
        OPEN FORGE →
      </button>
    </section>
  );
}

// ─── Footer ──────────────────────────────────────────────────────
function Footer() {
  return (
    <footer style={{ padding: "20px 40px 28px", display: "flex", alignItems: "center",
      gap: 18, color: "var(--q-t-3)", fontFamily: "var(--q-font-mono)", fontSize: "var(--q-fs-pico)",
      letterSpacing: "0.08em", borderTop: "1px solid var(--q-line-subtle)" }}>
      <span>OPERATOR · quant@quanta · root</span>
      <span>·</span>
      <span>localhost:8081 · LOCAL</span>
      <span>·</span>
      <span>BUILD v2.6.0 · COMMIT a4f81e2</span>
      <span style={{ flex: 1 }}/>
      <span><Kbd>?</Kbd> shortcuts</span>
      <span><Kbd>g</Kbd> then <Kbd>r</Kbd> risk</span>
      <span><Kbd>g</Kbd> then <Kbd>s</Kbd> shark</span>
      <span><Kbd>⌘</Kbd>+<Kbd>K</Kbd> command bar</span>
    </footer>
  );
}

// ─── Compose ─────────────────────────────────────────────────────
function Operator() {
  return (
    <div className="q-root" style={{ width: 1600, minHeight: 100, position: "relative" }}>
      <TopBar/>
      <EquityRibbon/>
      <RegimeStrip/>
      <CapBreachCard/>
      <AgentFlowCard/>
      <section style={{ padding: "0 40px 40px", display: "grid", gap: 24 }}>
        <V4Card/>
        <WheelCard/>
        <SharkCard/>
        <HermesCard/>
      </section>
      <RiskGates/>
      <IntegrationsRow/>
      <ModelForgeStrip/>
      <Footer/>
    </div>
  );
}

window.Operator = Operator;
