/* docs.js — operator reference (glossary, gates, breakers, regimes, architecture).
   Renders inside the same SPA shell as /ops + / via React 18 createElement
   against QC.* components from qc_react.js. Pure content — no live data
   fetches — so cache TTL is effectively forever (cache-bust via ?v=…).
*/
(function () {
  const React = window.React;
  const ReactDOM = window.ReactDOM;
  const h = React.createElement;
  const F = React.Fragment;
  // qc_react.js exports directly onto window (Object.assign(window, {…}))
  const Card    = window.Card;
  const Topbar  = window.Topbar;
  const Sidebar = window.Sidebar;
  const QC = { Card, Topbar, Sidebar };

  // ── content blocks ───────────────────────────────────────────────────────
  // Each section uses a Card with a stable data-num so the operator can
  // reference cards verbally ("see Docs card 03 on regime gates").

  function Def({ term, children }) {
    return h("div", { style: { marginBottom: "var(--s-3)", lineHeight: 1.55 } },
      h("strong", { style: { color: "var(--fg-1)" } }, term),
      h("span", { className: "dim", style: { marginLeft: 8 } }, "— "),
      h("span", null, children));
  }

  function K({ children }) {
    // monospace inline keyword
    return h("code", { style: { fontFamily: "var(--mono)", fontSize: "0.92em",
      padding: "1px 5px", borderRadius: 3, background: "var(--bg-inset)",
      color: "var(--fg-1)", whiteSpace: "nowrap" } }, children);
  }

  function P({ children }) {
    return h("p", { style: { lineHeight: 1.6, marginBottom: "var(--s-3)" } }, children);
  }

  function L({ items }) {
    return h("ul", { style: { lineHeight: 1.6, paddingLeft: 20, marginBottom: "var(--s-3)" } },
      items.map((it, i) => h("li", { key: i, style: { marginBottom: "var(--s-1)" } }, it)));
  }

  function H2({ children, id }) {
    return h("h2", { id, style: {
      fontSize: "var(--t-md)", fontWeight: 600,
      textTransform: "uppercase", letterSpacing: ".06em",
      margin: "var(--s-4) 0 var(--s-2)", color: "var(--fg-1)" } }, children);
  }

  // ── docs sections ────────────────────────────────────────────────────────

  function OverviewCard() {
    return h(QC.Card, { num: "01", title: "Overview · what this bot does",
      sub: "paper-trading multi-asset (crypto + stocks) — V4 quanta_core + wheel runner" },
      h(P, null, "Post-cutover (2026-05-13) + post-cleanup (2026-05-14): the live trading engine is ",
        h(K, null, "quanta_core"), " (V4) running ",
        h(K, null, "MeanRevBB + TrendFollow"), " strategies on 12 crypto pairs every 5 minutes. ",
        "Stocks continue on the ", h(K, null, "wheel runner"), " (CSP / covered-call cycle, Alpaca paper) ",
        "with a separate ", h(K, null, "Shark"), " LLM analyst layer for pre-market screening. ",
        "Legacy ", h(K, null, "freqtrade"), " was decommissioned over Phases 4-7 (folder deleted, ",
        h(K, null, "freqaimodels"), " imports purged, V3 dashboard gates swapped for V4 strategy gates). ",
        "Total starting equity is $119,000 (crypto $19k + stocks $100k). ",
        h(K, null, "Paper mode"), " — orders go through a paper-fill simulator, not real exchanges."),
      h(P, null, "Trade entries are gated by a 6-condition V4 pipeline (see ",
        h("a", { href: "#gates" }, "Entry gates"), ") — down from 11 pre-cutover. ",
        "The dashboard always shows engine status + NYSE session state side-by-side so ",
        "the 24/7 crypto engine pill never gets misread as \"stocks are trading\"."),
      h(L, { items: [
        h("span", null, h(K, null, "Pause"), " freezes new entries; positions stay open."),
        h("span", null, h(K, null, "Kill / hold ARM 1.5s"), " — hardened pause for panic moments."),
        h("span", null, h(K, null, "Resume"), " re-enables order placement (confirm dialog)."),
      ]}),
      h(P, null, "Top of every dashboard page shows the operator's watch-numbers: ",
        h(K, null, "EQUITY"), " (combined account), ",
        h(K, null, "day-pct"), " (move since UTC midnight), ",
        h(K, null, "MODE pill"), " (paper / live), ",
        h(K, null, "ENGINE pill"), " (quanta_core OK/DOWN), and on the Stocks venue tab a ",
        h(K, null, "NYSE pill"), " (OPEN / EXT-HRS / CLOSED).")
    );
  }

  function RegimesCard() {
    return h(QC.Card, { num: "02", title: "Market regimes (bull / bear / etc.)",
      sub: "5 regime states · HMM classifier · re-evaluated hourly" },
      h(P, null, "The bot classifies the market into one of five regimes using a Hidden Markov Model trained on price+volume features. ",
        "The currently active regime drives the entry-gate decision (see ", h("a", { href: "#gates" }, "regime gate"), ")."),
      h(Def, { term: "trending_up (\"bull\")" },
        "Price has a positive drift with low-to-moderate volatility. Entries are easiest — the regime gate passes by default and the ",
        h(K, null, "entry_delta"), " adjustment LOWERS the entry-confidence threshold (currently −0.08), encouraging more trades."),
      h(Def, { term: "trending_down (\"bear\")" },
        "Price has a negative drift. By default this is a HARD BLOCK — the regime gate refuses entries no matter how confident the predictor is. ",
        "Operators can override this via ", h(K, null, "regime_gating.trending_down_min_confidence"), " in the config: if the HMM is highly confident the regime is trending_down AND that threshold isn't met, the block can be relaxed."),
      h(Def, { term: "mean_reverting" },
        "Price oscillates around a moving mean without clear direction. The strategy switches to mean-reversion entries — buys oversold dips, sells overbought rallies. ",
        h(K, null, "mean_rev_take_profit"), " (default 1.2%) determines the per-trade target."),
      h(Def, { term: "high_volatility" },
        "Wide intraday ranges with no consistent direction. Entries are allowed but the position size is reduced by ",
        h(K, null, "high_vol_stake_factor"), " (default 0.7 = 70% of normal size), AND the predictor must clear ",
        h(K, null, "high_vol_min_confidence"), " (default 0.65)."),
      h(Def, { term: "unknown" },
        "The HMM is uncertain. Treated like a soft block — entries allowed only on very high confidence from other signals."),
      h(P, null, "Regime confidence is reported as ", h(K, null, "0.00–1.00"), " on the dashboard. ",
        "Card 02 on /ops shows the current regime + confidence + transition history (last 24h).")
    );
  }

  function GatesCard() {
    return h(QC.Card, { num: "03", title: "Entry gates · V4 strategy conditions", anchor: "gates",
      sub: "6-gate matrix per crypto pair · ANY failing gate blocks the entry" },
      h(P, null, "Post-cutover (2026-05-14), the crypto entry-gate matrix is derived from what ",
        h(K, null, "MeanRevBB"), " + ", h(K, null, "TrendFollow"), " actually evaluate on every bar — ",
        "not the old 11-column FreqAI / TFT pipeline. The dashboard's ", h(K, null, "EntryGatesLive"),
        " card and the top-of-page ", h(K, null, "BlockerBanner"), " both read the V4 set when ",
        h(K, null, "LIVE_ENGINE_MODE=live"), ". The pre-cutover V3 gates remain available under ",
        h(K, null, "row.v3_gates"), " for legacy callers but are not surfaced in the UI."),

      h(H2, null, "Crypto · V4 gates (6)"),
      h(Def, { term: h("span", null, h(K, null, "capital_allocation"), " · cap") },
        "The pair has a non-zero allocation in the capital weights (set by rebalance). If 0, the pair is benched."),
      h(Def, { term: h("span", null, h(K, null, "regime"), " · rgm") },
        "The HMM regime allows at least one strategy to enter. ",
        h(K, null, "trending_up"), " unlocks both strategies; ",
        h(K, null, "mean_reverting"), " unlocks MeanRevBB; ",
        h(K, null, "trending_down"), " / ", h(K, null, "high_volatility"), " block both ",
        "(universe goes to cash by design)."),
      h(Def, { term: h("span", null, h(K, null, "mr_dip"), " · mr·dip") },
        "MeanRevBB entry condition: ", h(K, null, "close < lower_bb"),
        " (20-period Bollinger lower band, 2σ). Detail row shows the literal: ",
        h(K, null, "close $79,732 ≥ lower_bb $79,383"), " — meaning still 0.4% above the band, ",
        "waiting for a deeper dip."),
      h(Def, { term: h("span", null, h(K, null, "tf_break"), " · tf·brk") },
        "TrendFollow entry condition #1: ", h(K, null, "close > short_ma"),
        " (8-period SMA). The momentum-break check that confirms price is above its short-term mean."),
      h(Def, { term: h("span", null, h(K, null, "tf_aligned"), " · tf·ma") },
        "TrendFollow entry condition #2: ", h(K, null, "short_ma > long_ma"),
        " (8 > 21 SMA alignment). Both ", h(K, null, "tf_break"), " AND ", h(K, null, "tf_aligned"),
        " must pass for TrendFollow to fire."),
      h(Def, { term: h("span", null, h(K, null, "account_capacity"), " · open") },
        "Account-wide: ", h(K, null, "open_count < max_open"), " (default 6 V4-paper positions) ",
        "AND ", h(K, null, "run_state.paused = false"), " (operator kill-switch clear)."),

      h(H2, null, "Stocks · Wheel gates (8)"),
      h(L, { items: [
        h("span", null, h(K, null, "kill_switch"), " — global Shark KILL.flag clear"),
        h("span", null, h(K, null, "ticker_kill_flag"), " — per-ticker 90-day kill window not active"),
        h("span", null, h(K, null, "spy_regime"), " — SPY HMM regime not trending_down"),
        h("span", null, h(K, null, "no_existing_csp"), " — no open short put for this underlying"),
        h("span", null, h(K, null, "no_assignment"), " — no assigned long shares for this underlying"),
        h("span", null, h(K, null, "buying_power"), " — Alpaca buying power covers the CSP"),
        h("span", null, h(K, null, "snapshot_fresh"), " — wheel account snapshot < 10 min old (NYSE hours only)"),
        h("span", null, h(K, null, "schedule"), " — current ET time inside the wheel-fire window"),
      ]}),

      h(H2, null, "What each banner row tells you"),
      h(P, null, "The top-of-/ops ", h(K, null, "BlockerBanner"), " summarises the matrix in one line: ",
        h(K, null, "🚦 14/27 pairs blocked · 12/27 on mr_dip · 11/27 on tf_aligned · 5/27 on tf_break · newest blocker: mr_dip"),
        ". \"14/27\" is crypto (12) + stocks (15) combined; \"12/27 on mr_dip\" means 12 of those pairs ",
        "are sitting above the BB lower band waiting for a touch. Click the banner to expand the per-pair ",
        "breakdown. Pre-cleanup, this banner showed ",
        h(K, null, "12/27 on model_freshness"), " because the dead FreqAI ",
        h(K, null, "pair_dictionary.json"), " read always failed — that path is gone now.")
    );
  }

  function BreakersCard() {
    return h(QC.Card, { num: "04", title: "Circuit breakers · two categories", anchor: "breakers",
      sub: "portfolio breaker (unified_risk) · service breakers (LLM / MCP)" },
      h(P, null, "Two independent safety systems wrap the bot."),

      h(H2, { id: "portfolio-breaker" }, "Portfolio breaker (unified_risk)"),
      h(P, null, "A combined-asset drawdown circuit that pauses ALL new entries when any of three conditions trip:"),
      h(Def, { term: h("span", null, h(K, null, "combined drawdown")) },
        "The combined (crypto+stocks) account is more than 10% below peak. Operator can override via ", h(K, null, "UNIFIED_DD_THRESHOLD_PCT"), " env."),
      h(Def, { term: h("span", null, h(K, null, "stocks data stale")) },
        "The stocks snapshot (cash, buying-power, portfolio value) is older than 600 seconds AND the NYSE market is open. ",
        "Fail-safe — if we can't see stocks state during market hours, we can't trust the combined DD calculation. Refresh via ",
        h(K, null, "bash ~/.hermes/scripts/wheel_snapshot.sh"), " or by waiting for the next cron tick."),
      h(Def, { term: h("span", null, h(K, null, "stocks data untrusted")) },
        "Same as above but worse — snapshot is older than 2 hours. The combined DD calculation switches to ",
        h(K, null, "crypto_dd"), " only (ignoring stocks entirely), and the breaker hard-trips regardless of market hours."),
      h(P, null, "While the portfolio breaker is TRIPPED, ",
        h(K, null, "approve_entry()"), " in the risk_governor refuses every order with the reason ",
        h(K, null, "combined breaker tripped"), ". Resume requires either the underlying condition to clear OR an operator manual reset via ",
        h(K, null, "resume_after_manual_review()"), "."),

      h(H2, { id: "service-breaker" }, "Service breakers (LLM / MCP)"),
      h(P, null, "Per-service circuit breakers that flip ", h(K, null, "open"), " (block calls) after ", h(K, null, "failure_threshold"),
        " consecutive failures. Each breaker has a ", h(K, null, "cooldown_remaining_s"), " timer — when it hits zero, the breaker moves to ",
        h(K, null, "half_open"), " and lets one probe through; success closes it, failure re-opens."),
      h(P, null, "Currently registered service breakers: Anthropic API (fallback), Ollama local LLM, MCP server (trading-bot). ",
        "Auto-failover means: if the primary LLM provider (Ollama) is open, cron jobs auto-route to the fallback (Anthropic) until cooldown."),
    );
  }

  function StrategyStackCard() {
    return h(QC.Card, { num: "05", title: "Strategy stack · V4 deterministic + observability signals", anchor: "strategy",
      sub: "MeanRevBB + TrendFollow drive crypto · classifier_log + meta_signal_log = observability" },
      h(P, null, "Post-cutover the V4 trading decision is ", h("strong", null, "deterministic"),
        " — two rule-based strategies (MeanRevBB, TrendFollow) evaluate Bollinger + SMA conditions ",
        "on every cycle and emit ", h(K, null, "BUY"), " or ", h(K, null, "FLAT"),
        " per pair. No black-box ensemble drives a live trade. The TFT / sentiment / regime signals ",
        "still run continuously, but as ", h("em", null, "observability surfaces"),
        " visible on the Model View card, not as gates."),

      h(H2, null, "Live trading signals (gate trade decisions)"),
      h(Def, { term: "MeanRevBB (mean_rev_bb)" },
        "Bollinger-band mean-reversion. 20-period BB at 2σ. Enters LONG when ",
        h(K, null, "close < lower_bb"), " AND regime ∈ {trending_up, mean_reverting}. ",
        "Exits at ", h(K, null, "close > middle_bb"), ". Permissive regimes pre-coded in ",
        h(K, null, "src/quanta_core/strategy/mean_rev_bb.py"), "."),
      h(Def, { term: "TrendFollow (trend_follow)" },
        "8/21 SMA momentum follower. Enters LONG only when regime is ", h(K, null, "trending_up"),
        " AND ", h(K, null, "close > short_ma > long_ma"), ". Exits on ",
        h(K, null, "close < short_ma"), " or any regime degrade."),
      h(Def, { term: "Strategy ownership rule" },
        "Each strategy sees only the positions it opened (via ", h(K, null, "fetch_positions(strategy=)"),
        "). Eliminates the cross-strategy 5-min stomping bug — MeanRevBB can't be exited by TrendFollow."),

      h(H2, null, "Observability signals (live but not gating)"),
      h(Def, { term: "HMM regime" },
        "4-state Hidden Markov Model on BTC 1h × 30d features ",
        h(K, null, "[log_return, realized_vol_30d, volume_ratio, rsi_14]"), ". ",
        "Re-fitted hourly inside the quanta-core cycle; writes to ", h(K, null, "regime_log"),
        ". Drives the ", h("a", { href: "#gates" }, "regime gate"), "."),
      h(Def, { term: "Momentum classifier (classifier_log)" },
        "Heuristic momentum scorer that replaced the FreqAI TFT post-cutover. Writes ",
        h(K, null, "p_up / p_flat / p_down / confidence"), " per cycle to ",
        h(K, null, "public.classifier_log"), ". Drives the Model View card's probability bar ",
        "but does NOT gate trades in V4."),
      h(Def, { term: "Meta-signal (meta_signal_log)" },
        "Weighted directional consensus across mean_rev_bb + trend_follow + regime + sentiment. ",
        "Writes ", h(K, null, "signal ∈ {-1, 0, +1}"), " + ", h(K, null, "confidence"), " to ",
        h(K, null, "public.meta_signal_log"), ". Surfaced as the META-AGENT LONG/HOLD/SHORT pill."),
      h(Def, { term: "Sentiment pipeline" },
        "6 sources (Reddit, news RSS, Fear & Greed, CoinGecko, HackerNews, StockTwits) ",
        "scored by hermes3:8b + hermes3:70b on local Ollama; trust-majority emits a directional row to ",
        h(K, null, "sentiment_log"), " every 15 min."),
      h(Def, { term: "On-chain (free pipeline)" },
        "blockchain.info, mempool.space, glassnode-public — whale netflow (>1 BTC tx count / 1h), ",
        "MVRV, exchange-to-cold-wallet ratio. Cards 03 (Market context) consumes these."),
      h(Def, { term: "Stocks · Shark TFT" },
        "Independent stock-side TFT model (champion ", h(K, null, "stock_tft_v1"),
        ") + LLM analyst pipeline. Drives the Shark Briefing card and feeds the Wheel runner's ",
        "candidate filter.")
    );
  }

  function CryptoVsStocksCard() {
    return h(QC.Card, { num: "06", title: "Crypto vs Stocks · two independent engines", anchor: "assets",
      sub: "quanta_core (crypto, 12 pairs, 24/7) · wheel + shark (stocks, NYSE-gated)" },
      h(H2, null, "Crypto"),
      h(L, { items: [
        h("span", null, "Pairs (12): BTC ETH SOL ADA XRP DOGE AVAX LINK DOT ATOM LTC BCH — all /USD on Coinbase"),
        h("span", null, "Engine: ", h(K, null, "quanta_core"), " (V4) → ",
          h(K, null, "MeanRevBB + TrendFollow"), " via ", h(K, null, "scripts/run_v4_shadow.py"),
          ". Coinbase public REST candles + paper-fill simulator. 24/7."),
        h("span", null, "Cadence: ", h(K, null, "SHADOW_CYCLE_SEC=300"), " (every 5 min)"),
        h("span", null, "Regime: 4-state HMM on BTC features → applied to all 12 pairs"),
        h("span", null, "Gates (6): ", h(K, null, "cap"), " · ", h(K, null, "rgm"), " · ",
          h(K, null, "mr·dip"), " · ", h(K, null, "tf·brk"), " · ", h(K, null, "tf·ma"), " · ",
          h(K, null, "open")),
        h("span", null, "Ledger: ", h(K, null, "quanta_schema.{proposals, fills, decisions, run_state}"),
          " + mirror to ", h(K, null, "public.trade_journal")),
      ]}),
      h(H2, null, "Stocks"),
      h(L, { items: [
        h("span", null, "Wheel basket: SOFI · NVDA · PLTR · MARA · COIN · MSTR (+ extras via ",
          h(K, null, "WHEEL_SYMBOLS"), ")"),
        h("span", null, "Dashboard basket: SPY · IWM · QQQ · AAPL · TSLA · NVDA · META · MSFT · GOOGL · AMZN · MARA · F · PLTR · AMD · MSTR (15 symbols)"),
        h("span", null, "Engine: ", h(K, null, "stocks/wheel/runner.py"),
          " (CSP → assignment → CC → called-away cycle) + ", h(K, null, "stocks/shark/"),
          " LLM analyst phases (pre-market / open / midday / EOD / daily_summary)"),
        h("span", null, "Broker: Alpaca paper · snapshots refreshed every 1 min by ",
          h(K, null, "wheel_snapshot"), " cron during NYSE hours"),
        h("span", null, "Regime: separate SPY-based HMM (only refreshed during regular session)"),
        h("span", null, "Gates (8): ", h(K, null, "kill"), " · ", h(K, null, "tkf"), " · ",
          h(K, null, "spy"), " · ", h(K, null, "ncsp"), " · ", h(K, null, "nasg"), " · ",
          h(K, null, "bpow"), " · ", h(K, null, "snap"), " · ", h(K, null, "sch")),
        h("span", null, "NYSE-aware: dashboard's ", h(K, null, "NYSE OPEN/EXT/CLOSED"),
          " pill on the Stocks venue tab tells the operator when phases are idle. ",
          h(K, null, "/api/ops/stocks"), " skips its staleness check when NYSE is closed."),
      ]}),
      h(P, null, "Crypto and stocks share ONE risk governor (the portfolio breaker above) but have ",
        "independent regime gates. A crypto bear market does NOT auto-pause stocks; that's a ",
        "configuration choice, not a hardcoded rule. They are scheduled separately too — the ",
        "quanta-core container cycles 24/7, while stocks phases run only on Hermes crons that ",
        "fire during NYSE regular hours (or daily at 17:30 ET for the summary).")
    );
  }

  function RiskMgmtCard() {
    return h(QC.Card, { num: "07", title: "Risk management vocabulary",
      sub: "drawdown · equity · peak · paper vs live · pause vs kill" },
      h(Def, { term: "Equity" },
        "The current total account value. Combined = crypto + stocks. Shown in the topbar as $XXX,XXX.XX."),
      h(Def, { term: "Peak equity" },
        "The highest equity the account has touched since reset. Monotonically increases. Persisted to ", h(K, null, "anchors.json"), " so it survives restarts."),
      h(Def, { term: "Drawdown" },
        "How far below peak the account is, as a percentage. ", h(K, null, "(peak - current) / peak"),
        ". Always non-negative. The 10% threshold triggers the portfolio breaker."),
      h(Def, { term: "Daily P&L" },
        "Sum of realised + unrealised P&L since UTC midnight. Crypto P&L is derived from ",
        h(K, null, "quanta_schema.fills"), " (V4 paper ledger); stocks doesn't expose a per-day P&L endpoint yet, so the dashboard derives it from ",
        h(K, null, "stocks_equity - stocks_peak_equity"), "."),
      h(Def, { term: "Paper mode (dry-run)" },
        "Orders are simulated against the broker's quote engine but never settled. P&L is real (against live prices); cash is fake."),
      h(Def, { term: "Pause" },
        "Stops new entries. Existing positions stay open and continue to update P&L. Reversible with one click (RESUME)."),
      h(Def, { term: "Kill switch" },
        "Same as Pause functionally, but requires a 1.5-second hold on ARM to confirm. Designed to prevent accidental clicks during a panic moment."),
      h(Def, { term: "Stop loss vs Trail stop" },
        "Stop loss = fixed % below entry price. Trail stop = follows the high-water mark of the trade up, never down. ",
        h(K, null, "trending_up_trail_trigger"), " (2.5%) activates the trail; ", h(K, null, "trail_distance"), " (-2%) sets the trail gap."),
    );
  }

  function ArchitectureCard() {
    return h(QC.Card, { num: "08", title: "Architecture · 5-layer stack", anchor: "arch",
      sub: "quanta_core (V4) ⇄ postgres ⇄ dashboard ⇄ MCP ⇄ Hermes gateway" },
      h(P, null, "Each layer is a separate Docker container (or systemd unit) with its own port. ",
        "All bound to ", h(K, null, "127.0.0.1"), " — nothing exposed to the network."),
      h(Def, { term: "quanta-core · V4 trading engine" },
        "Post-cutover (2026-05-13) strategy execution. Pulls Coinbase REST candles every 5 min, ",
        "runs MeanRevBB + TrendFollow with strategy ownership, writes proposals/fills to ",
        h(K, null, "quanta_schema"),
        " with a paper-fill simulator. Hourly inside the same loop: HMM regime → ", h(K, null, "regime_log"),
        ". Container ", h(K, null, "quanta-core"), "; entry point ", h(K, null, "scripts/run_v4_shadow.py"),
        ". Image ", h(K, null, "trading-bot-quanta-core"), "; ",
        h(K, null, "LIVE_ENGINE_MODE=live"), " in the operator .env. ",
        "Pre-cutover ", h(K, null, "freqtrade"), " was retired in Phase 4 (folder deleted, ",
        h(K, null, "freqaimodels"), " package removed)."),
      h(Def, { term: "postgres · 127.0.0.1:5434" },
        "TimescaleDB. Long-term storage for two schemas: ", h(K, null, "public.*"),
        " (trade_journal, regime_log, sentiment_log, classifier_log, meta_signal_log, equity_snapshots, anchors) ",
        "and ", h(K, null, "quanta_schema.*"),
        " (V4 ledger — run_state, proposals, orders, fills, decisions, quanta_schema_version). ",
        "Container ", h(K, null, "tradebot-postgres"), "."),
      h(Def, { term: "dashboard · 127.0.0.1:8081" },
        "FastAPI app at ", h(K, null, "user_data/dashboard/"), ". Three SPAs share one shell: ",
        h(K, null, "/"), " (per-pair drill), ", h(K, null, "/ops"), " (operations console), ",
        h(K, null, "/docs"), " (this page). ",
        "Surfaces 35+ ", h(K, null, "/api/ops/*"), " routes (read-only) + ", h(K, null, "/api/v4/*"),
        " (V4 trade tape) + mutating ", h(K, null, "/api/ops/{pause,resume}"),
        " gated by ", h(K, null, "HERMES_MCP_KEY"), ". Engine-aware via ",
        h(K, null, "LIVE_ENGINE_MODE"), " env. Container ", h(K, null, "dashboard"), "."),
      h(Def, { term: "MCP server · hermes-mcp.service" },
        "Read-only Model Context Protocol surface exposing 27 tools to Hermes / Claude ",
        "(get_open_trades, get_risk_status, pause_trading, etc.). Runs as a host systemd unit."),
      h(Def, { term: "Hermes gateway · user-systemd · 31 cron jobs" },
        "User-systemd unit that runs the cron scheduler + Telegram/Slack adapters. Jobs include: ",
        h(K, null, "sentiment_refresh"), " (15-min), ",
        h(K, null, "wheel_snapshot"), " (1-min during NYSE hours), ",
        h(K, null, "wheel_sell_csps"), " / ", h(K, null, "wheel_sell_calls"), " / ",
        h(K, null, "wheel_profit_take"), ", ",
        h(K, null, "shark_pre_market"), " · ", h(K, null, "shark_market_open"), " · ",
        h(K, null, "shark_midday"), " · ", h(K, null, "shark_daily_summary"), ", ",
        h(K, null, "shark_briefing_alerts"), ", ",
        h(K, null, "nightly_reflector"), " (qwen3:30b), ",
        h(K, null, "modelforge_ingest / curate"), ". See ",
        h(K, null, "docs/HERMES_GATEWAY_RUNBOOK.md"), "."),
      h(Def, { term: "model-forge × 4 · LoRA training pipeline" },
        "Side-stack at ", h(K, null, "mf-api"), " (port 8000) + ", h(K, null, "mf-frontend"),
        " (LoRA UI) + ", h(K, null, "mf-postgres"), " + ", h(K, null, "mf-redis"),
        ". Runs the weekly LoRA training window (Sunday 14:00-18:00 ET) and serves ",
        "the champion adapter registry the Weekly Training card reads from."),
    );
  }

  function OperationsCard() {
    return h(QC.Card, { num: "09", title: "Operator actions · what each button does",
      sub: "/ops Quick actions card · all confirmed before firing" },
      h(Def, { term: "PAUSE TRADING" },
        "POST ", h(K, null, "/api/ops/pause"), " — sets ", h(K, null, "mode=paused"),
        " in the active engine. Existing positions stay open; no new entries fire. Reversible."),
      h(Def, { term: "RESUME" },
        "POST ", h(K, null, "/api/ops/resume"), " — requires confirm() dialog. Re-enables order placement. ",
        "If the portfolio breaker is also tripped, you'll need to clear that condition first (e.g., refresh stocks snapshot)."),
      h(Def, { term: "TRIGGER EVOLUTION" },
        "Kicks off the EPT (Evolutionary Parameter Tuner) cycle — runs ",
        h(K, null, "scripts/run_ept_generation.py"),
        " which mutates the regime_config (entry_delta, exit_delta, scalars) and backtests N variants. ",
        "PID-locked — only one cycle can run at a time."),
      h(Def, { term: "REBALANCE WEIGHTS" },
        "Recomputes per-pair capital allocation using rolling Sharpe ratio. ",
        "Saves to ", h(K, null, "capital_weights.json"), " which the V4 engine hot-reloads on the next cycle."),
      h(Def, { term: "DAILY SLACK BRIEF" },
        "Renders the same payload the 00:00 UTC nightly cron will Slack — operator can preview it before bed."),
      h(Def, { term: "KILL switch (hold ARM 1.5s)" },
        "Hardened version of Pause. Designed for panic moments — the hold gesture prevents accidental clicks. ",
        "Functionally identical to Pause once it fires.")
    );
  }

  function GlossaryCard() {
    return h(QC.Card, { num: "10", title: "Glossary · A–Z",
      sub: "every term used elsewhere in the UI" },
      h("div", { style: { columnCount: 2, columnGap: "var(--s-5)" } },
        h(Def, { term: "Anchors" },
          "Persisted peak-equity values written to ", h(K, null, "anchors.json"), " so peak survives restarts."),
        h(Def, { term: "ARM" },
          "First step of the Kill switch. Holding for 1.5s confirms and pauses the bot."),
        h(Def, { term: "Bear" },
          "Casual term for ", h(K, null, "trending_down"), " regime — price has negative drift."),
        h(Def, { term: "Bull" },
          "Casual term for ", h(K, null, "trending_up"), " regime — price has positive drift."),
        h(Def, { term: "Breaker" },
          "A circuit-breaker that blocks new entries when a safety condition trips."),
        h(Def, { term: "Candle" },
          "OHLCV bar over a fixed timeframe (1m, 5m, 1h, etc.). Open / High / Low / Close / Volume."),
        h(Def, { term: "CSP" },
          "Cash-Secured Put. Wheel-strategy entry: sell put → if assigned, hold shares."),
        h(Def, { term: "Covered Call (CC)" },
          "Wheel-strategy follow-up to assignment: sell call against held shares to collect premium."),
        h(Def, { term: "Drawdown (DD)" },
          "Loss from peak as a percentage. Combined DD = (peak − current) / peak."),
        h(Def, { term: "Dry-run" },
          "Same as paper mode. Orders simulated, P&L computed against real prices."),
        h(Def, { term: "DRL" },
          "Deep Reinforcement Learning ensemble (PPO/A2C/DDPG). One of the 5 prediction sources."),
        h(Def, { term: "EPT" },
          "Evolutionary Parameter Tuner. Mutates regime_config and backtests variants to find better entry/exit deltas."),
        h(Def, { term: "Equity" },
          "Total account value (cash + open positions). Crypto + stocks combined."),
        h(Def, { term: "Fail-safe" },
          "A defensive default that engages when data is missing or stale (e.g., stocks_data_stale → trip portfolio breaker)."),
        h(Def, { term: "Fear & Greed" },
          "Aggregate market-sentiment index 0–100. Folded into the sentiment pipeline."),
        h(Def, { term: "FreqAI (legacy)" },
          "Freqtrade's built-in ML predictor (gradient boosting). Retired with the rest of freqtrade on 2026-05-14; quanta-core's classifier (writes to ", h(K, null, "public.classifier_log"), ") is the post-cutover replacement."),
        h(Def, { term: "Gate" },
          "One of 11 independent checks. ANY failure blocks the trade."),
        h(Def, { term: "HMM" },
          "Hidden Markov Model. Classifies the current market regime."),
        h(Def, { term: "Half-open" },
          "Service-breaker state: cooldown expired, letting one probe through to test recovery."),
        h(Def, { term: "Hermes" },
          "User-systemd cron + adapter framework. Runs 9 LLM-driven jobs that read from the MCP server."),
        h(Def, { term: "Kill switch" },
          "Hardened pause requiring 1.5s ARM hold. Same effect as Pause."),
        h(Def, { term: "Live trades" },
          "Currently-open positions. Shown on the ops console live_trades card."),
        h(Def, { term: "Markers" },
          "Triangles on the candle chart marking entry (▲) and exit (▼) of past trades."),
        h(Def, { term: "MCP" },
          "Model Context Protocol. Server exposing read-only tools to Claude/Hermes."),
        h(Def, { term: "Meta-signal" },
          "Ensembled output of all 5 prediction sources. Drives the final entry decision."),
        h(Def, { term: "MVRV" },
          "Market Value to Realized Value (on-chain metric). High MVRV → market overheated."),
        h(Def, { term: "Netflow" },
          "Net BTC moving onto/off exchanges. Positive → buying pressure, negative → selling."),
        h(Def, { term: "Open / closed (breaker)" },
          "Service-breaker states. Open = blocking calls. Closed = normal. Half-open = probing."),
        h(Def, { term: "Paper mode" },
          "See dry-run. No real money at risk."),
        h(Def, { term: "Peak equity" },
          "Monotonic high-water mark of the account."),
        h(Def, { term: "Postgres" },
          "Long-term database. Holds trade_journal, regime_log, sentiment_scores, anchors."),
        h(Def, { term: "Quanta" },
          "The dashboard's brand name. v2.6 is the current SPA version."),
        h(Def, { term: "Regime" },
          "Market state: trending_up, trending_down, mean_reverting, high_volatility, unknown."),
        h(Def, { term: "Sharpe ratio" },
          "Risk-adjusted return: ", h(K, null, "mean(return) / std(return)"), ". Used for per-pair capital weighting."),
        h(Def, { term: "Shark TFT" },
          "The stocks-side TFT model + LLM analyst pipeline. Drives the Shark Briefing card."),
        h(Def, { term: "Slack brief" },
          "Daily 00:00 UTC summary posted to Slack with PnL, regime distribution, key alerts."),
        h(Def, { term: "Sparkline" },
          "Tiny inline line chart. Used for 24h price trends per pair."),
        h(Def, { term: "Stake" },
          "Dollar amount allocated to a single trade."),
        h(Def, { term: "TFT" },
          "Temporal Fusion Transformer. Deep-learning sequence predictor."),
        h(Def, { term: "Trailing stop" },
          "Stop-loss that follows the high-water mark of an open trade up but never down."),
        h(Def, { term: "Unrealised P&L" },
          "Mark-to-market profit on open positions. Becomes realised when the position closes."),
        h(Def, { term: "VWAP" },
          "Volume-Weighted Average Price. Indicator overlay on the candle chart."),
        h(Def, { term: "Wheel" },
          "Options strategy: CSP → assignment → CC → called-away → repeat. Used for stocks."),
        h(Def, { term: "Whale" },
          "On-chain entity holding >1 BTC. Tracked via mempool.space pubkey heuristics."),
      )
    );
  }

  // ── shell ────────────────────────────────────────────────────────────────

  function TocCard() {
    return h(QC.Card, { num: "00", title: "Table of contents", sub: "click any section to jump" },
      h("div", { style: { display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: "var(--s-2)", fontSize: "var(--t-sm)" } },
        [
          ["#overview", "01 · Overview"],
          ["#regimes", "02 · Market regimes"],
          ["#gates", "03 · Entry gates"],
          ["#breakers", "04 · Circuit breakers"],
          ["#strategy", "05 · Strategy stack"],
          ["#assets", "06 · Crypto vs Stocks"],
          ["#risk", "07 · Risk vocabulary"],
          ["#arch", "08 · Architecture"],
          ["#ops", "09 · Operator actions"],
          ["#glossary", "10 · Glossary A–Z"],
        ].map(([href, label], i) => h("a", { key: i, href,
          style: { color: "var(--fg-1)", textDecoration: "none", padding: "var(--s-1) var(--s-2)",
            border: "1px solid var(--line-1)", borderRadius: 4, display: "block" } }, label)))
    );
  }

  function DocsApp() {
    const [killState, setKillState] = React.useState("normal");
    return h(F, null,
      h("div", { className: "app" },
        h(QC.Topbar, {
          killState, setKillState, density: "default",
          onRefreshIntervalChange: () => {}, onRefreshNow: () => {},
          active: { mode: "paper", dryRun: true }
        }),
        h(QC.Sidebar, { active: "docs" }),
        h("main", { className: "main" },
          h("div", { className: "page-title" },
            h("h1", null, "Docs · glossary"),
            h("span", { className: "breadcrumb" }, "/ operator reference"),
            h("span", { className: "tb-spacer", style: { flex: 1 } }),
            h("span", { className: "dim mono", style: { fontSize: "var(--t-xs)" } }, "v2026-05-14 · post-cleanup")
          ),
          h("div", { style: { display: "flex", flexDirection: "column", gap: "var(--gap-grid)" } },
            h(TocCard),
            h("div", { id: "overview", className: "anchor" }, h(OverviewCard)),
            h("div", { id: "regimes",  className: "anchor" }, h(RegimesCard)),
            h("div", { id: "gates",    className: "anchor" }, h(GatesCard)),
            h("div", { id: "breakers", className: "anchor" }, h(BreakersCard)),
            h("div", { id: "strategy", className: "anchor" }, h(StrategyStackCard)),
            h("div", { id: "assets",   className: "anchor" }, h(CryptoVsStocksCard)),
            h("div", { id: "risk",     className: "anchor" }, h(RiskMgmtCard)),
            h("div", { id: "arch",     className: "anchor" }, h(ArchitectureCard)),
            h("div", { id: "ops",      className: "anchor" }, h(OperationsCard)),
            h("div", { id: "glossary", className: "anchor" }, h(GlossaryCard)),
            h("div", { style: { fontSize: "var(--t-2xs)", color: "var(--fg-3)", textAlign: "center", padding: "var(--s-5) 0" } },
              "QUANTA v2.6 · operator reference · built " + new Date().toISOString().slice(0, 10))
          )
        )
      )
    );
  }

  ReactDOM.createRoot(document.getElementById("root")).render(h(DocsApp));
})();
