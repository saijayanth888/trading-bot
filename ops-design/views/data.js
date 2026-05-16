// Shared operator state for both mocks. Captures the snapshot from the brief:
// Saturday 13:29 ET, NYSE closed, combined equity $118,292.03, peak $120,128.54,
// DD -1.53%, crypto regime trending_down conf 96%, 1 open NVDA short-put 220,
// day P&L $0 (markets closed). Plus historical context for sparklines and feeds.

window.Q = {
  now: { et: "13:29:14 ET", date: "SAT · MAY 16 2026", nyseOpen: false },
  banner: {
    level: "warn",                              // ok | warn | crit
    headline: "1 single-name cap breach in last 24h",
    sub: "BTC/USD stake $66.0k vs cap $1.94k · 34× violation · trade closed −$1,057.32",
  },
  equity: {
    combined: 118292.03,
    peak: 120128.54,
    drawdown: -1.53,                            // %, negative
    pause: -3.0,
    kill:  -10.0,
    dayPL:        0.00,
    dayPLpct:     0.00,
    cryptoPL:     0.00,
    stocksPL:     0.00,
    cryptoEquity: 18934.18,
    stocksEquity: 99357.85,
  },
  sides: {
    crypto:  { regime: "trending_down", regimeConf: 0.96, ws: "live",   lastTick: "12s", openPos: 0 },
    stocks:  { regime: "consolidating", regimeConf: 0.71, ws: "closed", lastTick: "17h", openPos: 1 },
  },
  v4: {
    decisionsToday: 27,
    bbVotes: 12, tfVotes: 5, alignedVotes: 5, regimeBlocked: 12,
    pairs: [
      // sym       last      d24h    bb     tf     regime  decision  conf   fresh
      ["BTC/USD",  68420.50, +0.42,  "MR",  "—",   "blk",  "FLAT",   0.61,  "11s"],
      ["ETH/USD",   2384.10, -0.18,  "MR",  "TF",  "ok",   "LONG",   0.78,  "12s"],
      ["SOL/USD",    138.42, +1.84,  "—",   "TF",  "ok",   "LONG",   0.71,  "13s"],
      ["ADA/USD",      0.41, -0.06,  "MR",  "—",   "blk",  "FLAT",   0.55,  "9s"],
      ["XRP/USD",      0.52, -0.31,  "MR",  "—",   "ok",   "SHORT",  "0.82","12s"],
      ["DOGE/USD",     0.17, +0.95,  "—",   "TF",  "blk",  "FLAT",   0.52,  "11s"],
      ["LINK/USD",    13.84, -0.42,  "MR",  "—",   "blk",  "FLAT",   0.66,  "12s"],
      ["BCH/USD",    412.71, +0.04,  "MR",  "—",   "blk",  "FLAT",   0.58,  "13s"],
      ["LTC/USD",     78.04, -0.22,  "—",   "—",   "ok",   "FLAT",   0.51,  "10s"],
      ["ATOM/USD",     6.84, +1.18,  "MR",  "TF",  "ok",   "LONG",   0.73,  "12s"],
      ["AVAX/USD",    22.41, -0.84,  "MR",  "—",   "blk",  "FLAT",   0.62,  "14s"],
      ["DOT/USD",      4.42, +0.31,  "—",   "—",   "ok",   "FLAT",   0.49,  "11s"],
    ],
    lastFill: { sym: "XRP/USD", side: "SELL", qty: 540, fillPx: 0.5238, pl: +18.42, age: "22h" },
  },
  wheel: {
    open: [
      // sym  contract       strike  qty  credit   expiry      pl       dte
      ["NVDA","short_put",   220,   1,   616.00,  "2026-05-23", +118.42, 5],
    ],
    universe: ["NVDA","GOOGL","AAPL","SOFI","SPY","PLTR","QQQ","IWM","MARA","HOOD","TSLA","AMD","MSTR","F","COIN"],
    nextRoll: "MON · 10:35 ET",
    lastSnap: "17h",
  },
  shark: {
    phase: "WAITING · pre_market_scan @ MON 08:30 ET",
    lastPick: { sym: "PLTR", side: "LONG", confirmed: "FRI 2026-05-15 09:30 ET", pl: +84.18, status: "closed_eod" },
    model: "qwen2.5:72b-instruct · local",
    fallback: "claude-sonnet-4-6",
    debate: [
      { role: "bull", t: "08:31:04", txt: "PLTR — Q1 commercial bookings +83% YoY; AIP enterprise pipeline accelerating into earnings window. Setup: pullback to 21EMA on rising RSI." },
      { role: "bear", t: "08:31:18", txt: "Crowded long, IV crush after print, options skew negative. Government-segment lumpy revenue — guidance miss risk asymmetric." },
      { role: "bull", t: "08:31:31", txt: "Position sized 1.5% — within cap. Stop −2.4% beneath 21EMA. R:R 3.1× at first target. Catalyst is dated, not crowded by 4DTE." },
      { role: "arbiter", t: "08:31:47", txt: "Confirmed PLTR LONG · size 1.5% · stop 24.18 · target 26.40. Rationale: catalyst-window setup with defined R:R, bear concerns priced via stop. 4 prior debate turns elided." },
    ],
    confirmedCount: 4,
    rejectedCount: 11,
  },
  hermes: {
    jobs: 34,
    nextFires: [
      // who                           when         desc
      ["v4.crypto.tick",                "00:14",     "12 pairs · BB+TF vote · regime gate"],
      ["wheel.snapshot",                "00:14",     "options chain refresh"],
      ["risk.governor.heartbeat",       "00:44",     "single-name cap check · DD check"],
      ["v4.regime.recompute",           "04:14",     "HMM 7d window"],
      ["wheel.eod_pnl_snapshot",        "MON 16:00", "skipped · NYSE closed"],
      ["shark.pre_market_scan",         "MON 08:30", "qwen2.5:72b debate · 5 candidates"],
      ["modelforge.weekly_promote",     "SUN 02:00", "champion v423 → v424 if parity-pass"],
    ],
    recentRuns: [
      ["v4.crypto.tick",         "ok",    "00:13:14", "12 decisions · 0 fills"],
      ["v4.crypto.tick",         "ok",    "00:08:14", "12 decisions · 0 fills"],
      ["v4.crypto.tick",         "ok",    "00:03:14", "12 decisions · 0 fills"],
      ["risk.governor.heartbeat","ok",    "00:00:44", "cap·dd·weekly all green"],
      ["v4.crypto.tick",         "ok",    "23:58:14", "12 decisions · 0 fills"],
      ["wheel.snapshot",         "skip",  "23:58:14", "NYSE closed · marks frozen"],
      ["v4.crypto.tick",         "ok",    "23:53:14", "12 decisions · 1 fill XRP/USD"],
    ],
  },
  modelforge: {
    champion: "adapter-v423",
    promotedAge: "4d 12h",
    queue: 2,
    pendingEval: ["adapter-v424-bb_low_vol", "adapter-v425-tf_alt_hmm"],
    parityPass: 0.973,
    api: "95 endpoints · /admin/forge",
  },
  risk: {
    singleNameCap: { limit: 0.10, peak24h: 3.42, status: "breach", breachAt: "FRI 23:14 ET" },
    dailyLoss:     { limit: -3.0, today: 0.00,   status: "ok" },
    weeklyTrades:  { limit: 50,   used:  18,     status: "ok" },
    drawdown:      { limit: -10.0, current: -1.53, status: "ok" },
    weeklyDD:      { limit: -5.0, current: -1.53, status: "ok" },
  },
  // 12-month equity arc, for sparklines / drawdown ribbon context
  equityArc: [
    100000, 101240, 102810, 105200, 107420, 108910, 109840, 112030, 115280,
    117420, 116910, 118040, 119320, 120128, 119780, 119240, 118920, 118650,
    118420, 118292,
  ],
  intradayPL: [ 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0 ], // Saturday flat
};
