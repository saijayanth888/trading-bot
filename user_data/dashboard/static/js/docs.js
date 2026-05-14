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
      h(P, null, "Post-cutover (2026-05-13): the live trading engine is ",
        h(K, null, "quanta_core"), " (V4) running ",
        h(K, null, "MeanRevBB + TrendFollow"), " strategies on 12 crypto pairs via the ",
        h(K, null, "quanta-core"), " container. Stocks continue on the ",
        h(K, null, "wheel runner"), " (CSP / covered-call cycle, Alpaca paper). ",
        "Legacy ", h(K, null, "freqtrade"), " was decommissioned 2026-05-14 (services dropped from compose, dashboard backend purged). ",
        "Total starting equity is $119,000 (crypto $19k + stocks $100k). ",
        h(K, null, "Paper mode"), " — orders go through a paper-fill simulator, not real exchanges."),
      h(P, null, "Trade entries are gated by an 11-condition pipeline (see ", h("a", { href: "#gates" }, "Entry gates"), ")."),
      h(L, { items: [
        h("span", null, h(K, null, "Pause"), " freezes new entries; positions stay open."),
        h(K, null, "Kill / hold ARM 1.5s"),
        h(K, null, "Resume"),
      ]}),
      h(P, null, "Top of every dashboard page shows the operator's three watch-numbers: ",
        h(K, null, "EQUITY"), " (total combined account value), ",
        h(K, null, "day-pct"), " (account move since UTC midnight), and ",
        h(K, null, "BOT UP h m"), " (process uptime since last restart).")
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
    return h(QC.Card, { num: "03", title: "Entry gates · 11 conditions", anchor: "gates",
      sub: "ANY failing gate blocks the entry · first_blocker tells you which" },
      h(P, null, "Every potential trade is run through 11 independent gates. ",
        "ALL must pass (or be N/A) for the order to fire. ",
        "On /ops the Entry Gates card shows the per-pair status as a dot strip — green=pass, red=block, gray=n/a — and the ", h(K, null, "first_blocker"), " column tells you which gate refused first."),
      h(Def, { term: h("span", null, h(K, null, "capital_allocation")) },
        "The pair has a non-zero allocation in the capital weights (set by rebalance). If 0, the pair is benched."),
      h(Def, { term: h("span", null, h(K, null, "model_freshness")) },
        "The FreqAI model file for this pair is younger than 72 hours. Stale models silently skew predictions, so the gate forces a retrain."),
      h(Def, { term: h("span", null, h(K, null, "freqai_predict")) },
        "FreqAI returned ", h(K, null, "do_predict=1"), " for the candle. If FreqAI couldn't compute a prediction (NaN inputs, missing features), this is N/A and the entry is blocked."),
      h(Def, { term: h("span", null, h(K, null, "volume")) },
        "Recent volume is above the minimum (avoids dead-market entries). N/A when the candle's volume field is missing."),
      h(Def, { term: h("span", null, h(K, null, "regime")) },
        "The current market regime allows entries. Hard-blocks on trending_down by default (see ", h("a", { href: "#regimes" }, "Regimes"), ")."),
      h(Def, { term: h("span", null, h(K, null, "up_prob_threshold")) },
        "The predictor's \"up\" probability is above the regime-adjusted threshold. trending_down forces this to fail outright (`hard block due to regime`)."),
      h(Def, { term: h("span", null, h(K, null, "tft_confidence")) },
        "The Temporal Fusion Transformer's confidence is above ", h(K, null, "tft_min_confidence"), " (default 0.50). If TFT is uncertain about direction, no entry."),
      h(Def, { term: h("span", null, h(K, null, "high_vol_confidence")) },
        "Only enforced in high_volatility regime. Predictor must clear ", h(K, null, "high_vol_min_confidence"), " (0.65) to overcome the wider noise floor."),
      h(Def, { term: h("span", null, h(K, null, "meta_confidence")) },
        "The ensembled meta-signal (TFT + DRL + sentiment + on-chain weighted) must clear ", h(K, null, "meta_min_confidence"), " (default 0.35). This is the final cross-model sanity check."),
      h(Def, { term: h("span", null, h(K, null, "sentiment_floor")) },
        "Aggregate sentiment score is not deeply negative. Catastrophic news (e.g., -0.8 score) blocks entries until the news cycle cools down."),
      h(Def, { term: h("span", null, h(K, null, "onchain_safety")) },
        "On-chain signals (whale netflow, MVRV) aren't flashing red. Heavy whale exit + extended MVRV = block until normal flow resumes."),
      h(P, null, "The aggregate banner at the top of the Entry Gates card tells you the SYSTEMIC reason for any block — e.g., \"8 of 9 pairs blocked · most common: regime (8×) · up_prob_threshold (8×)\" means the bear market is keeping everything offline.")
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
    return h(QC.Card, { num: "05", title: "Strategy stack · 5 prediction sources ensemble", anchor: "strategy",
      sub: "TFT + DRL + HMM + sentiment + on-chain → meta_signal" },
      h(P, null, "The bot doesn't trade on a single model — it ensembles five independent signal sources into a single ",
        h(K, null, "meta_signal"), " that drives the entry decision."),
      h(Def, { term: "TFT (Temporal Fusion Transformer)" },
        "Deep-learning sequence model trained on price + indicator history. Outputs probabilities for up / down / flat over the next N candles. Used as the primary directional signal."),
      h(Def, { term: "DRL ensemble (Deep Reinforcement Learning)" },
        "PPO / A2C / DDPG agents trained against historical PnL. Each agent votes BUY / SELL / HOLD; the ensemble averages confidence-weighted. Loaded lazily — first call after restart pays the cold-start cost."),
      h(Def, { term: "HMM (Hidden Markov Model)" },
        "Classifier for the market regime (see ", h("a", { href: "#regimes" }, "Regimes"), "). Doesn't predict direction directly; it determines WHICH entry rules apply."),
      h(Def, { term: "Sentiment pipeline" },
        "Reddit (r/cryptocurrency, r/bitcoin) + news headlines + Fear & Greed Index, scored by ",
        h(K, null, "hermes3:70b"), " (or Anthropic fallback) into [-1, +1]. Affects ", h(K, null, "sentiment_floor"), " gate."),
      h(Def, { term: "On-chain signals" },
        "Free providers: blockchain.info, mempool.space, glassnode-public. Tracks whale netflow (>1 BTC tx count in last 1h), MVRV (market cap / realized cap), exchange-to-cold-wallet ratio. Drives ",
        h(K, null, "onchain_safety"), " gate."),
      h(Def, { term: "Meta-signal" },
        "Weighted sum of all five sources, normalized to [-1, +1]. The ", h(K, null, "meta_confidence"), " is how strongly the sources agree. Trades fire only when |meta_signal| > 0 AND meta_confidence > threshold.")
    );
  }

  function CryptoVsStocksCard() {
    return h(QC.Card, { num: "06", title: "Crypto vs Stocks · two completely separate engines",
      sub: "quanta_core V4 (crypto, 12 pairs) · wheel runner (stocks, 1 active symbol)" },
      h(H2, null, "Crypto"),
      h(L, { items: [
        h("span", null, "Pairs: BTC, ETH, SOL, ADA, XRP, DOGE, AVAX, LINK, DOT, ATOM, LTC, BCH — all /USD on Coinbase"),
        h("span", null, "Engine: ", h(K, null, "quanta_core"), " (V4) running ", h(K, null, "MeanRevBB + TrendFollow"), " — Coinbase REST + paper-fill simulator. Legacy freqtrade was decommissioned 2026-05-14 (services dropped from compose)."),
        h("span", null, "Timeframe: 5-minute candles by default"),
        h("span", null, "Regime model: HMM trained on aggregate crypto features"),
        h("span", null, "Gates: 11-condition pipeline above"),
      ]}),
      h(H2, null, "Stocks"),
      h(L, { items: [
        h("span", null, "Symbols: SOFI (currently traded), plus chart-only watchlist PLTR, NVDA, AMD, SPY"),
        h("span", null, "Engine: ", h(K, null, "stocks/wheel/runner.py"), " — a CSP / Covered Call wheel cycle"),
        h("span", null, "Broker: Alpaca paper API; snapshots refreshed every 5 min via Hermes cron"),
        h("span", null, "Regime model: SEPARATE HMM (just for SOFI) — currently shows trending_up while crypto is trending_down"),
        h("span", null, "Cycle: Sell Cash-Secured Put → assigned shares → Sell Covered Call → called away → repeat"),
      ]}),
      h(P, null, "Crypto and stocks share ONE risk governor (the portfolio breaker above) but have independent regime gates. ",
        "A crypto bear market does NOT auto-pause stocks; that's currently a configuration choice, not a hardcoded rule.")
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
    return h(QC.Card, { num: "08", title: "Architecture · 5-layer stack",
      sub: "quanta_core (V4) ⇄ postgres ⇄ dashboard ⇄ MCP ⇄ Hermes gateway" },
      h(P, null, "Each layer is a separate Docker container (or systemd unit) with its own port. All bound to ",
        h(K, null, "127.0.0.1"), " — nothing exposed to the network."),
      h(Def, { term: "quanta-core · V4 trading engine" },
        "Post-cutover (2026-05-13) strategy execution. Pulls Coinbase REST candles every 5 min, runs MeanRevBB + TrendFollow, writes proposals/fills to ",
        h(K, null, "quanta_schema"), " with a paper-fill simulator. Container ", h(K, null, "quanta-core"), ". Legacy freqtrade decommissioned 2026-05-14 — services removed from compose."),
      h(Def, { term: "postgres · 127.0.0.1:5434" },
        "Long-term storage for trade journal, regime history, sentiment scores, anchors, V4 ledger (proposals/orders/fills/decisions). Container ", h(K, null, "tradebot-postgres"),
        ". TimescaleDB extension for time-series queries."),
      h(Def, { term: "dashboard · 127.0.0.1:8081" },
        "FastAPI app serving the SPA. Reads from quanta_schema + postgres + Alpaca + on-chain providers. Container ",
        h(K, null, "dashboard"), ". Contains the routes you're reading right now."),
      h(Def, { term: "MCP server · hermes-mcp.service" },
        "Read-only Model Context Protocol surface exposing 27 tools to Hermes / Claude (get_open_trades, get_risk_status, pause_trading, etc.). Runs as a system systemd unit."),
      h(Def, { term: "Hermes gateway · hermes-gateway.service" },
        "User-systemd unit that runs the cron scheduler + Telegram/Slack adapters. Fires 9 LLM-driven cron jobs (risk_monitor, market_research, post_mortem, wheel_snapshot, etc.). See ",
        h(K, null, "docs/HERMES_GATEWAY_RUNBOOK.md"), " for lifecycle details."),
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
            h("span", { className: "dim mono", style: { fontSize: "var(--t-xs)" } }, "v2026-05-11")
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
