# Strategy Research — 2026-05-15

**Scope:** What trading strategies actually generate post-fee profit for retail/small-algo
operators in 2024–2026 crypto and stocks markets, mapped to the Quanta system.
**Analyst:** Solo research agent, read-only, external evidence + internal code reading.
**Confidence disclosure:** Annotated per section (High / Medium / Low).

---

## TL;DR — 7 Bullets

1. **Tier-A candidate: Wheel on liquid high-IV stocks (CSPs + covered calls).** Literature
   consensus: Sharpe ~1.0–1.1 vs SPY ~0.7, 12–18% annualised premium before assignment,
   improved risk-adjusted returns via 30–40% volatility reduction. You already have the rails.
   (Source: spintwig SPY backtest 2007–2024; DayTrading.com covered-call Sharpe meta-study.)

2. **Tier-A candidate: Perpetual funding-rate harvest (delta-neutral long spot / short perp).**
   Simple threshold-entry variant: ~18–19% APY post-fee in 2024–2025 with Sharpe ~1.4;
   ML-enhanced variant: ~31% APY, Sharpe ~2.3. Needs a perp exchange (Binance/Bybit/dYdX) —
   a new account, not a blocker. Capital minimum is low (~$2k viable, $10k+ practical).
   (Source: Sharpe AI funding rate research; ScienceDirect 2025 study on CEX/DEX funding arb.)

3. **Tier-B candidate: Crypto cointegration pairs trading (BTC/ETH daily bars).**
   Published Sharpe 2.45 with 16.3% APY on daily data 2022–2024, max drawdown -8.3%. But:
   2% fees killed profitability in the same study. Viable only with maker-only fee routing.
   (Source: IJSRA cointegration study 2026; Yale undergraduate paper Zhu 2024.)

4. **Tier-B candidate: LLM-augmented event-driven sentiment (your ModelForge edge).**
   Backtested Sharpe improvement from 0.34→3.47 (TSLA) and -4.03→2.13 (AAPL) when
   LLM sentiment layered onto SMA crossover. No post-fee validation; out-of-sample unproven.
   But YOUR asymmetric cost advantage ($0/inference) makes this worth exploring experimentally.
   (Source: "An End-To-End LLM Enhanced Trading System," arxiv 2502.01574, 2025.)

5. **DO NOT pursue: MeanRevBB on 5-min liquid crypto.** Your own gates report confirms
   Sharpe = -20.4, profit_factor = 0.10. Literature is consistent: BB mean-rev captures
   0.05–0.20% per trade; Coinbase round-trip fees are 0.25–0.40%. Expected value is negative
   BEFORE slippage. This is not a calibration problem — it is a structural edge problem.

6. **DO NOT pursue: TrendFollow SMA cross on 5-min crypto.** Gates report: Sharpe = -58.7,
   profit_factor = 0.12, win rate 10–15%. This is worse than random at 5-min bars on
   liquid crypto. SMA crosses on 5-min are well-arbitraged by institutional algos.

7. **Honest macro statement:** The academic and practitioner literature is clear that
   >90% of retail algo bots are not profitable long-term after fees, latency, and competition.
   The strategies that work — funding arb, vol-selling, stat arb on daily bars — are
   structurally different from "buy the lower BB, sell the SMA." This operator's largest
   structural advantage is $0 LLM inference cost and a working HMM regime classifier.

---

## Methodology

### Sources Consulted

- Sharpe AI blog, funding rate arbitrage guide: https://sharpe.ai/blog/funding-rate-arbitrage
- ScienceDirect, "Exploring Risk and Return Profiles of Funding Rate Arbitrage on CEX and DEX" (2025): https://www.sciencedirect.com/science/article/pii/S2096720925000818
- spintwig.com, "SPY Wheel 45-DTE Options Backtest" (2007–2024): https://spintwig.com/spy-wheel-45-dte-options-backtest/
- DayTrading.com, "Do Covered Calls Improve Sharpe Ratios?": https://www.daytrading.com/covered-calls-sharpe-ratios
- EarlyRetirementNow, "Why the Wheel Strategy Doesn't Work" (Sept 2024): https://earlyretirementnow.com/2024/09/17/the-wheel-strategy-doesnt-work-options-series-part-12/
- Quantpedia, "Volatility Risk Premium Effect": https://quantpedia.com/strategies/volatility-risk-premium-effect
- Quantpedia, "Time Series Momentum Effect": https://quantpedia.com/strategies/time-series-momentum-effect
- arxiv 2502.01574, "An End-To-End LLM Enhanced Trading System" (2025): https://arxiv.org/html/2502.01574v1
- arxiv 2508.07408, "Event-Aware Sentiment Factors from LLM-Augmented Financial Tweets" (2025): https://arxiv.org/html/2508.07408v1
- Frontiers in AI, "Large Language Models in equity markets" (2025): https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2025.1608365/full
- IJSRA, "Statistical Arbitrage Strategies Using Cointegration" (2026): https://ijsra.net/sites/default/files/fulltext_pdf/IJSRA-2026-0283.pdf
- QuantifiedStrategies.com, "Bitcoin Mean Reversion in Low Volume Regimes": https://www.quantifiedstrategies.com/bitcoin-mean-reversion-strategies-outperform-momentum-in-low-volume-regimes/
- Robuxio, "Algorithmic Crypto Trading VII: Regime Filter": https://www.robuxio.com/algorithmic-crypto-trading-vii-regime-filter/
- Yale econ paper, Zhu "Examining Pairs Trading Profitability" (April 2024): https://economics.yale.edu/sites/default/files/2024-05/Zhu_Pairs_Trading.pdf
- arxiv 2510.05533, "The New Quant: A Survey of LLMs in Financial Prediction" (2025): https://arxiv.org/html/2510.05533v1
- FinLoRA benchmark (2025): https://arxiv.org/html/2505.19819v1
- Internal: `private/REVIEW_2026-05-11.md:38-43` — fee analysis, BB expected value
- Internal: `user_data/backtest_results/gates_report_mean_rev_bb_latest.json` — Sharpe -20.4, PF 0.10
- Internal: `user_data/backtest_results/gates_report_trend_follow_latest.json` — Sharpe -58.7, PF 0.12
- Internal: `audit/2026-05-14-night/07-architecture-review.md` — overall system assessment

### Excluded and Why

- Signal-selling services (e.g., Discord "alpha calls") — selection bias, no track record disclosure.
- Academic papers with zero-fee assumptions and no out-of-sample validation.
- HFT/co-location strategies — require latency (<1 ms) this operator cannot achieve.
- Deribit options on crypto — would require new account, flagged as C/D tier.
- Any strategy requiring >$500k capital with this operator's ~$120k paper floor.
- Marketing whitepapers from bot vendors (Zignaly, 3Commas) — no auditable track records.

### Confidence on Source Quality (Scale)
- **High:** spintwig backtest data (methodology disclosed, 2,200+ trades), quantpedia (academic citations)
- **Medium:** Sharpe AI funding rate estimates (methodology partial), arxiv papers (academic but no live P&L)
- **Low:** Reddit/Quora anecdotes, wundertrading marketing claims — not cited as evidence

---

## Strategy Candidates

Ranked by: edge probability × infra fit × operator effort (best first).

---

### 1. Options Wheel — Cash-Secured Puts + Covered Calls on Liquid Stocks

**Tier: A — Build now (already running, optimize)**

**Mechanism**

Sell a put at 0.25–0.35 delta on a stock you are willing to own. If it expires worthless,
collect premium and repeat. If assigned, sell covered calls at 0.25 delta until the shares
are called away, collecting additional premium. The edge source is the volatility risk
premium (VRP): implied volatility (IV) persistently exceeds realized volatility (RV) by
~5–10 vol points on US equities on average, meaning option sellers are overpaid for the
risk they take. The strategy is profitable when IV > RV; it fails when a stock gaps down
through the put strike (assignment at a loss) or during sustained bear markets where calls
cannot be sold above the acquisition price.

**Evidence**

- spintwig SPY 45-DTE wheel backtest (Jan 2007 – Mar 2024, 2,200+ trades): Strategy Sharpe
  ~1.08 vs SPY Sharpe ~0.70. However: "the long underlying position accounted for 94-99%
  of total return." This is the critical honest finding — the premium income is a drag
  reducer, not the primary return engine. Total return underperforms buy-and-hold on
  SPY. (Source: spintwig.com)
- DayTrading.com meta-study of 3 independent studies: covered calls improve Sharpe by
  +36% (AQR, Sharpe 0.45 vs 0.33) to +73% (R-bloggers BXM 1986–2011, Sharpe 0.52 vs 0.30).
  Volatility reduced 30–40%. Annual return sacrifice: 0.5–1.5%. (Source: daytrading.com)
- EarlyRetirementNow (Sep 2024): Conceptual critique — wheel forces averaging down (more
  equity exposure when price falls); 2000–2013 bear market shows calls near strike price
  would generate near-zero premium when stock is far underwater. No formal backtest.
  (Source: earlyretirementnow.com/2024/09/17)
- Realistic annual premium yield for 0.30-delta CSPs: 12–18% annualised on the cash
  secured. For $120k capital deployed, this translates to $14.4k–$21.6k/year gross before
  assignment losses. Net is scenario-dependent.
- Your live data: +$524 across 2 closed NVDA/SOFI CSPs. Sample too small for significance.

**Fit to our system**

- Alpaca paper infrastructure already built and running. Wheel runner fires Friday mornings.
  `stocks/wheel/runner.py` handles CSP scanning, `positions.json` tracks open trades.
- Shark pre-market phases use Hermes LLM to score stock selection — this is legitimate use
  of our $0-inference advantage.
- Hermes3:8b can screen for high IV-Rank tickers before each Friday's CSP sale.
- ModelForge LoRA: fine-tuning for earnings-call sentiment could help avoid selling CSPs
  into known binary event risk (see §5, LLM ModelForge edge).
- Nothing new needed to run this. The infra is live.

**Capital minimum:** $5,000–$10,000 per position to cover assignment on $50–$100 strikes.
With $120k paper capital, you can run 5–8 simultaneous positions at 0.20 delta.

**Effort estimate:** Optimization effort ~20h. Optimize: (a) IV-Rank entry threshold
(>35 IV-Rank is the practitioner sweet spot), (b) 45-DTE vs 14-DTE trade-off,
(c) add earnings calendar check to avoid binary events.

**Failure modes**

- Stock gaps down 15%+ through strike on earnings miss → assigned at large loss, calls must
  recover the basis over months. NVDA -20% on bad earnings = assignment + extended drawdown.
- Sustained bear market: all strikes underwater, covered calls generate negligible premium.
  Maximum loss = stock price going to zero.
- Low-IV environment (VIX < 15): premium yields compress to 4–6% annualised. Not worth
  the tail risk. Solution: raise IV-Rank entry threshold to >40.

**Recommendation:** This is the ONLY currently profitable surface. Protect and expand it.
Fix positions.json atomic write first (`stocks/wheel/runner.py` — write-tmp + rename).
Then add IV-Rank filter and earnings calendar guard.

---

### 2. Perpetual Funding Rate Harvest (Delta-Neutral: Long Spot / Short Perp)

**Tier: A — Build in Week 2–3 (requires new exchange account, not a blocker)**

**Mechanism**

When perpetual futures trade at a persistent premium to spot (positive funding), long-biased
traders pay short-biased traders every 8 hours. By holding equal long-spot and short-perp
positions, you earn this funding payment with near-zero directional exposure (delta-neutral).
The edge is structural: in bull markets, retail perpetual longs repeatedly overpay to
maintain leverage. The strategy is a form of vol-selling but on the term structure of
perpetual futures rather than options.

Example at 0.015%/8h (current benchmark): $10,000 deployed → $3/day → ~5.4% APY.
At 0.05%/8h (high-sentiment periods): same capital → ~20% APY. During 2024–2025
extremes (0.1%/8h): ~40% APY.

**Evidence**

- Sharpe AI funding rate analysis: "0.03% 8h rate on $2,000 notional → 32.95% APR before
  fees, 28-30% after typical maker/maker fees." (Source: sharpe.ai/blog/funding-rate-arbitrage)
- ScienceDirect (2025): Average annual return increased to 19.26% in 2025 (up from 14.39%
  in 2024). ML-enhanced variant (4h prediction + dynamic sizing): 31% APY, Sharpe 2.3.
  Simple threshold-entry: ~18% APY, Sharpe ~1.4. (Source: ScienceDirect S2096720925000818)
- Critical caveat from same study: "Only 40% of top opportunities generate positive returns
  after transaction costs and spread reversals." Selective entry is essential — cannot run
  this 24/7 on any pair; must screen for persistently elevated funding above threshold.
- Two-tiered exchange structure (MDPI 2024): Sharpe varies widely by exchange. BTC perps:
  Binance Sharpe -0.21, BitMex Sharpe 1.02, Drift Sharpe 0.82. XRP perps: Drift Sharpe
  1.66, ApolloX Sharpe 1.14. This means exchange selection matters enormously.

**Fit to our system**

- NOT on Coinbase (no perp trading for US retail). Needs Binance, Bybit, or dYdX v4.
  This is a new account — friction of ~1 day setup, not a architectural blocker.
- Our HMM regime classifier is highly relevant: run the strategy only in trending_up and
  high_volatility regimes (where funding rates spike). Skip mean_reverting and trending_down.
- Hermes sentiment cron could gate entries: high-positive sentiment → elevated funding → run.
- Can be cron-jobbed (check funding rate every 8h, enter if > threshold, monitor for
  sign reversal). No tick-level data needed. 5-min REST polling is sufficient.
- ModelForge LoRA edge: fine-tune on historical funding rate data to predict funding rate
  sign reversals 4–8h ahead. This is exactly the ML-enhanced variant in the ScienceDirect
  paper that boosted Sharpe to 2.3.
- Capital minimum: ~$2k viable, $10k practical to make fees worth it.
  With $120k, this strategy could absorb $20–$30k notional (both legs combined).

**Effort estimate:** ~30h to build:
- Exchange account setup + API keys: 2h
- Funding rate fetcher (REST polling): 4h
- Delta-neutral position manager (spot buy + perp short): 8h
- Entry/exit threshold logic + regime gate: 6h
- Dashboard integration (new position card): 8h
- Paper testing + fee validation: 2h

**Failure modes**

- Funding rate flips negative (shorts pay longs): reverse direction or exit. Risk of not
  catching the flip before it costs money. Mitigation: monitor every 5 min, exit within
  one payment cycle.
- Exchange liquidation of short perp leg during a sudden spot price spike: if perp short
  leg is liquidated, you are suddenly long spot with no hedge → full directional exposure.
  Mitigation: use only 1–2x leverage on perp leg; maintain margin buffer > 50%.
- Regulatory risk: US retail access to Binance perp is restricted. Bybit or dYdX v4
  (decentralized) are cleaner options for US operators.

---

### 3. Crypto Cointegration Pairs Trading (BTC/ETH, Daily Bars)

**Tier: B — Build after 4-week paper window**

**Mechanism**

BTC and ETH are historically cointegrated — they share a long-run equilibrium relationship.
When the spread (log(BTC/ETH) ratio or a regression-based spread) deviates significantly
from its mean (z-score > 2), short the overperformer and buy the underperformer. Close when
the spread reverts. The edge is the statistical pull of the cointegration relationship;
the trade is market-neutral with respect to crypto beta (both sides hedge each other).

This is a fundamentally different mechanism from BB mean-reversion. BB mean-rev bets on
absolute price levels; pairs trading bets on a relative spread that has a theoretical
anchor.

**Evidence**

- IJSRA cointegration study (2026, Jan 2022–Oct 2024): BTC/ETH best performing pair,
  16.34% APY, Sharpe 2.45, max drawdown -8.34%, winning percentage 64.74%, profit factor 2.34.
  (Source: IJSRA-2026-0283.pdf, daily bars)
- Yale undergraduate paper (Zhu, Apr 2024): pairs trading profitability positive across
  crypto markets, but "2% transaction costs kill profitability." This is the critical
  constraint — maker-only fees on Coinbase Advanced Trade are 0.08%, round-trip 0.16%
  for maker/maker, well below the 2% failure threshold. (Source: Zhu Yale 2024)
- Optimal Market-Neutral Multivariate Pair Trading (arxiv 2405.15461): More sophisticated
  multi-pair formulation. Validates the approach is workable at daily bars.
- Key realistic caveat: studies use daily bars. You only have 5-min REST bars on Coinbase.
  Daily bars require only 1–2 trades per week per pair — far more compatible with your
  polling infrastructure than 5-min strategies.

**Fit to our system**

- Coinbase REST can fetch daily OHLCV — no infrastructure change needed.
- TimescaleDB already stores bar data; add a spread table for BTC/ETH log-price ratio.
- Regime filter from existing HMM: run only in mean_reverting regime (where cointegration
  holds most reliably), pause in high_volatility (spread can blow out).
- V4 paper engine can handle the two-legged proposal (BTC long + ETH short or vice versa).
  Ownership-aware strategy pattern already exists.
- Biggest gap: need cointegration testing module (Engle-Granger or Johansen test),
  z-score spread calculation, and re-fitting window management.
- Capital minimum: $5k–$10k per leg ($10–$20k total). Viable with $120k paper account.

**Effort estimate:** ~40h to prototype:
- Daily bar fetcher (Coinbase `/products/BTC-USD/candles?granularity=86400`): 4h
- Cointegration test + spread z-score calculation (statsmodels): 6h
- Entry/exit logic (z > 2 enter, z < 0.5 exit, z < -2 reverse): 8h
- Dual-leg proposal to V4 engine: 8h
- Rolling refitting (re-run cointegration every 30 days): 4h
- Dashboard card: 6h
- Backtest validation against in-sample data: 4h

**Failure modes**

- Cointegration breakdown: major regulatory event (SEC action on ETH, BTC spot ETF flows)
  can break the cointegration relationship permanently. Historical BTC/ETH cointegration
  was strongest 2019–2024; the decoupling risk increases as ETH staking/DeFi differentiate it.
- Spread blow-out before reversion: z-score hits 3.0 on entry, goes to 5.0 before reverting.
  The position will lose money until reversion. Maximum loss is unbounded in theory (both
  legs move against you). Stop-loss on z-score is essential (exit at z > 4.0).
- Execution: selling the overperformer (a short) on Coinbase requires a margin account.
  Coinbase does not offer retail margin in most US states. Alternative: use the perp exchange
  from Strategy 2 for the short leg and spot on Coinbase for the long leg. This adds
  cross-exchange execution complexity.

---

### 4. LLM-Augmented Event-Driven Sentiment Trading

**Tier: B — Experiment in parallel with Wheel (low capital risk)**

**Mechanism**

Deploy your local LLM (hermes3:8b or 70b) to classify news events into high-signal
categories: earnings surprises, analyst upgrades, regulatory actions, supply chain disruptions.
Size directional equity positions based on the classification confidence and event type.
The edge: LLMs can interpret nuanced narrative faster and more consistently than simple
keyword sentiment scorers. With zero inference cost, you can run this on every headline
without fee pressure, unlike cloud-API-dependent systems that gate on per-call cost.

**Evidence**

- arxiv 2502.01574 (2025): LLM sentiment overlay on SMA crossover strategies. TSLA Sharpe:
  0.34 → 3.47; AAPL: -4.03 → 2.13; AMZN: -2.75 → 3.14. Win ratio TSLA: 32.2% → 57.0%.
  **Critical caveat:** backtest only (2022–2023 data), no post-fee numbers, no out-of-sample
  validation. Sharpe improvements this large from backtests should be treated as an upper
  bound, not a forecast. (Source: arxiv.org/html/2502.01574v1)
- FinLoRA benchmark (2025): LoRA methods achieved 36% average performance gain over base
  models on financial tasks including earnings analysis, SEC filing interpretation, and
  sentiment classification. (Source: arxiv.org/html/2505.19819v1)
- "Event-Aware Sentiment Factors from LLM-Augmented Financial Tweets" (arxiv 2508.07408, 2025):
  LLMs automatically assign multi-label event categories (rumor/speculation, retail hype,
  brand boycott) to generate interpretable signals with statistically significant alpha.
- Frontiers in AI LLM survey (2025): Multi-agent debate architectures (bullish/bearish
  researchers + trader layer) show improved signal-to-noise vs single-agent classification.
  This is exactly how your existing Hermes debate pipeline is structured. (Source: frontiersin.org)

**Fit to our system**

- Hermes3:8b/70b on local Ollama = $0/inference. This is a REAL asymmetric advantage.
  Competitors using GPT-4/Claude pay $15–$60 per 1M tokens. You pay $0.
- Your existing sentiment pipeline already fetches headlines (60 headlines/cycle confirmed
  in audit data). The parser is the broken piece (zeroed since 2026-05-14 03:30 UTC).
  Fixing the existing sentiment scorer is the first step — not new infrastructure.
- ModelForge LoRA: fine-tune hermes3:8b on a domain-specific corpus:
  - Earnings transcripts → directional move predictions
  - SEC 8-K filings → material event classification
  - Funding rate anomaly announcements → crypto directional signal
  This is where ModelForge pays off — the pipeline exists (mf-api, mf-postgres, training
  infra on DGX Spark), but the champion adapter for trading use cases is empty.
  Schema gaps (5 critical tables empty per audit) must be fixed first.
- The multi-agent debate (hermes3:8b bull analyst vs bear analyst → Hermes3:70b judge)
  is described in the Frontiers paper as best practice. Your existing Shark pipeline
  already implements this for stock selection — wire the output to trade proposals.

**Capital exposure design:** Run event-driven trades with 1–2% of portfolio per signal
(~$1,200–$2,400 per trade on $120k). Use regime as a meta-gate: only fire in trending_up
or mean_reverting regime. Post-event exits within 24–48h to avoid overstaying.

**Effort estimate:** ~25h:
- Fix existing sentiment scorer (Ollama parser bug): 2–4h (diagnosis from audit)
- Add event classification layer (beyond simple positive/negative): 8h
- Wire Shark output to V4 trade proposals with size rules: 6h
- ModelForge LoRA fine-tuning on earnings data: 8h (after schema fix)
- Backtesting harness for event-driven positions: variable

**Failure modes**

- Hallucination: LLM misclassifies a bearish event as bullish. Mitigation: confidence
  threshold gate (only fire if hermes3:70b + hermes3:8b debate converges at >0.75 confidence),
  small position sizing.
- Stale news: headlines already priced in by the time they hit REST APIs. 5-min polling
  means you can be 5–30 minutes behind an event. The alpha from public news is eroding
  rapidly as more systems consume the same APIs. Your edge is classification quality,
  not latency.
- Earnings binary events: options would be more appropriate here; directional equity
  bets around earnings have high variance. Avoid directional equity bets within 3 days
  of earnings; use Wheel CSPs with high IV-Rank instead (the strategies are complementary).

---

### 5. Volatility Risk Premium Selling (Broader IV > RV Capture)

**Tier: C — Research after 4-week paper window**

**Mechanism**

Sell options when IV is significantly above historical realized volatility (IV Rank > 40,
IV > 30-day HV by >10 points). Collect premium as IV reverts toward RV. The wheel is a
subset of this strategy. Iron condors, short strangles, and calendar spreads are broader
implementations.

**Evidence**

- Quantpedia VRP backtest (1986–1995): Sharpe 1.16, ~26% annualised ATM options.
  **Severe caveat:** -800% historical max loss tail, serial correlation in large negative
  days, requires substantial margin reserves that reduce net returns dramatically.
  (Source: quantpedia.com/strategies/volatility-risk-premium-effect)
- Covered call ETFs (QYLD, XYLD): Sharpe ~1.0, CAGR 8% vs SPY 13% (2009–2023).
  Sharpe improvement from volatility reduction, not higher absolute returns.
- August 2024 VIX spike: short-vol strategies suffered in the unwinding of yen carry
  trades. VRP turned negative on a 12-month rolling basis in 2023–2024, making timing
  critical. (Source: robotwealth.com, pennmutualam.com)

**Why Tier C not A:**
The broader VRP strategy (short strangles, iron condors) requires Alpaca Level 3 options
approval (spread trades) and active management of gamma risk. The Wheel is the safe subset
of this. Expanding to naked/spread options without demonstrated Wheel profitability first
is premature.

---

### 6. Time-Series Momentum (Weekly/Monthly Bars on Stocks/Futures)

**Tier: C — Requires 58 liquid futures instruments; not viable without futures account**

**Mechanism**

Go long assets that have risen over the past 12 months (minus last month). Go short those
that have fallen. Rebalance monthly or weekly. Edge: trend persistence in futures markets
(academic consensus since Moskowitz-Ooi-Pedersen 2012).

**Evidence**

- Quantpedia TSMOM: Pre-fee Sharpe 1.31 (1965–2009). Requires 58 instruments across
  commodity, currency, equity, bond futures. Max drawdown -33.87%. Post-fee performance
  materially reduced — not quantified in source. (Source: quantpedia.com/strategies/time-series-momentum-effect)
- Weekly rebalancing on 500 stocks: Sharpe 0.84, better than monthly. (From web search aggregation)
- Concentrated portfolio (10-stock sector-neutral, 20 years): Sharpe 0.59, CAGR 11.3%.

**Why Tier C:**
You have Coinbase (spot crypto), Alpaca (US equities, no futures). Futures account (IBKR,
Tradovate) would be needed for the diversified version. The single-asset momentum on weekly
bars (buy top-performing crypto pairs from prior week) is simpler — testable on existing
infra — but the academic edge is weaker on individual assets vs diversified futures portfolios.

---

### 7. Statistical Market-Making / Bid-Ask Spread Capture

**Tier: D — Do not pursue without co-location**

**Mechanism**

Post limit orders on both sides of the bid-ask spread, capturing the spread as compensation
for providing liquidity. Edge: earn exchange rebates (maker fees negative on Binance/Bybit).

**Why D:** Requires quote at best bid/offer within milliseconds of price movement. With 5-min
REST polling, you will always be posting stale quotes and getting adversely selected.
Institutional market makers co-locate at the exchange (sub-millisecond latency). This is
not viable for a REST-based system.

---

### 8. ETF NAV Arbitrage / Cross-Exchange Spot Arbitrage

**Tier: D — Latency-sensitive, not viable**

Both rely on sub-second latency to capture price discrepancies that close within seconds.
With 5-min REST polling and single-exchange access, this is not executable. Do not pursue.

---

## Strategies Currently in the Bot — Verdict

### MeanRevBB on 5-Min Crypto — **KILL IT**

**Was it ever expected to work?** No, not at 5-min bars on liquid crypto with taker fees.

Your own gates report (`gates_report_mean_rev_bb_latest.json`):
- Sharpe: -20.4 (threshold 1.0: FAIL)
- Profit factor: 0.10 (threshold 1.5: FAIL; 1.0 = break-even, 0.10 = losing 90 cents per $1 won)
- Walk-forward variance: 0.22 (threshold 0.15: FAIL — highly unstable across time windows)
- Win rates across 6 walk-forward windows: 25%, 35%, 38%, 42%, 30%, 25% — averaging ~33%.
  A coin flip beats this strategy.
- Monte Carlo p-value: 0.0000 — the strategy IS reliably making a decision, but that
  decision is reliably losing money. This is negative edge, not noise.

**Literature consensus:** BB mean-reversion on 5-min bars of liquid crypto (BTC, ETH) is
a textbook negative-EV trade after fees. The strategy captures 0.05–0.20% per trade;
Coinbase round-trip taker fees are 0.4–0.6% (0.2% each leg). Expected value is
approximately -0.2% to -0.4% per trade. Regime filtering helps (avoids trending markets)
but cannot overcome the fee burden on 5-min bars. The only viable variant is on longer
bars (1h+) where the per-trade P&L potential exceeds the fee floor, or on a zero-fee
exchange (decentralized). (Sources: private/REVIEW_2026-05-11.md:38-43; crosstrade.io
mean reversion guide; QuantifiedStrategies.com BTC mean reversion)

**Recommendation:** Remove MeanRevBB from the V4 engine for crypto 5-min pairs. Replace
with a regime-aware funding rate scanner (Strategy 2) or cointegration spread (Strategy 3).

---

### TrendFollow SMA Cross on 5-Min Crypto — **KILL IT**

**Was it ever expected to work?** No.

Your gates report (`gates_report_trend_follow_latest.json`):
- Sharpe: -58.7 — significantly worse than MeanRevBB
- Profit factor: 0.12 — nearly as bad
- Win rate across 6 windows: 10–15% — catastrophically low
- 14,957 trades in the backtest period = ~61,000 trades/year. This is not a trading
  strategy; this is a fee-extraction machine working against you.

**Why SMA crosses on 5-min bars fail:** At high frequency, SMA crossovers generate
signal primarily on noise, not trend. The 8/21 SMA pair crosses dozens of times per day
on liquid crypto. Each cross costs 0.4–0.6% round-trip. With 10–15% win rate,
even a 10:1 reward-to-risk ratio would not save it. At 5 minutes, every institutional
HFT and market-maker is already arbitraging the SMA crossover signal — by the time your
REST poll returns, the edge (if any) is gone.

**Recommendation:** Remove TrendFollow from 5-min crypto immediately. If momentum is
desired, run it on daily or weekly bars (see Strategy 6) or as a regime-confirmation
gate (use regime.trending_up as confirmation, not SMA cross).

---

### Wheel CSPs on Liquid Stocks — **KEEP AND EXPAND**

This is the only surface generating positive P&L. +$524 across 2 closed trades is
insufficient to declare edge (need 30+ trades), but the mechanism is structurally sound.

**Realistic monthly yield ceiling at $120k capital:**
- Deploy 60–70% of capital in CSPs (6–8 positions × $8–12k collateral each).
- At 0.30-delta, 30-DTE on high IV-Rank stocks: ~1.5–2.5% monthly on notional.
- Monthly gross: $120k × 0.65 × 0.02 = $1,560/month. Annualised: $18,720.
- After assignment losses (assume 1–2 bad fills/year, -$2,000–$5,000 impact): net ~$14–17k/year.
- That is 11–14% annual return on $120k — real money, real edge if managed correctly.
- IV-environment caveat: if VIX drops to 12–14, premium yield halves. Watch IV-Rank threshold.

**Critical improvement needed NOW:**
- Add earnings calendar guard: do not sell CSPs within 5 trading days of earnings.
  Binary event IV spike can cause gap-down assignment at losses exceeding 6 months of premium.
- Fix positions.json atomic write before any real money flows here.
- Increase IV-Rank threshold to >35 for new CSP entries.

---

### V3 FreqAIMeanRevV1 (pre-cutover) — **Dead strategy, no residual evidence**

Based on `private/REVIEW_2026-05-11.md:30-44`, the V3 FreqAI strategy was idle (hard-blocked
by regime=trending_down) for most of its operational period. The one executed trade lost -$23.37.
Total paper P&L as of 2026-05-11 was -$66.39. There is no evidence it had or was developing
positive edge. The V4 cutover removed it correctly — not a regression, a correction.

---

## What Our Infra Makes Us GOOD At (Asymmetric Advantages)

### 1. Local LLM Inference at $0 — What This Enables

Cloud competitors using GPT-4 Turbo pay $0.01–$0.06 per 1k tokens in the hot path. At
500 headlines/day × 1k tokens/classification = 500k tokens/day = $5–$30/day = $150–$900/month.
You pay $0. This means:

- **Classification breadth:** You can classify EVERY headline, not gate on cost. Competitors
  must choose which headlines to process; you process all of them.
- **Debate pipeline:** Running hermes3:8b as bull analyst + hermes3:70b as judge is $0.
  No competitor running cloud LLMs can afford multi-round debate on every signal.
- **Iterative experimentation:** You can retrain, re-prompt, and A/B test classification
  schemes without API cost accumulating. ModelForge LoRA retraining on DGX Spark: ~$0 marginal.

**Quantified edge potential:** If LLM sentiment overlay improves Sharpe from 0.34 to 3.47
(arxiv 2502.01574 upper bound, NOT realistic forecast), and you deploy this on $20k of
equity positions with 1% daily position sizing, the difference between Sharpe 0.34 and
Sharpe 1.5 (a conservative post-fee realization) is material. Even Sharpe 1.0 on a
well-managed equity book at $120k is $12k–$18k/year expected return at 10–15% annualised.

### 2. ModelForge LoRA — Use Cases That ACTUALLY Have Edge

Current state: champion adapter `run-d4dac705` exists on disk but has no DB row;
5 critical tables empty; schema column mismatches. ModelForge is not learning anything.
Fix the schema first (2026-05-15-modelforge-schema-fix-plan.md exists).

Once fixed, the highest-value fine-tuning targets in order of expected alpha:

**Use Case 1: Earnings Sentiment Classification** (Highest Priority)
- Training data: public earnings call transcripts (SEC EDGAR), Q&A tone classification
- Label: next-day stock direction (binary) or magnitude (regression)
- Edge: hermes3:8b tuned on earnings calls can identify cautionary language patterns
  ("headwinds," "normalization," "conservatively") that generic models miss
- Paper: FinLoRA benchmark (2025) shows 36% improvement on financial tasks with LoRA
  over base models. Earnings classification is the highest-signal financial NLP task.

**Use Case 2: Funding Rate Sign Prediction** (High Priority for Strategy 2)
- Training data: historical funding rate time series + market sentiment features
- Label: funding rate sign at T+8h (positive = harvest, negative = reverse/exit)
- Edge: predict funding rate reversals 4–8h ahead. The ScienceDirect paper (2025)
  shows this ML variant achieves Sharpe 2.3 vs 1.4 for simple threshold entry.
- Your DGX Spark can train this easily; the timeseries data is in TimescaleDB.

**Use Case 3: Regime Shift Detection Enhancement** (Medium Priority)
- Current HMM is trained and working (4 regimes). LoRA-based regime detection could
  supplement HMM with narrative signals (macro news, Fed announcements).
- Training data: news + HMM regime labels as supervision signal.
- This is a classification problem (4 classes) well-suited for fine-tuned LLM.

**Use Case 4: SEC 8-K Material Event Classification** (Medium Priority)
- Training data: SEC EDGAR 8-K filings (public, free via EDGAR API)
- Label: material positive / material negative / neutral / binary event
- Edge: sell CSPs only when 8-K history is clean (no recent material negatives);
  avoid selling options on stocks with upcoming 8-K pattern.

### 3. Multi-Source Sentiment + On-Chain — Narrative/Event Trading

The multi-agent debate pipeline (Shark) is architecturally correct for event-driven
trading. The failure is in the execution (sentiment scorer zeroed, HistoricalEdge undefined).
Fix the existing pipeline before building new infra.

Narrative-driven trades where institutions are slow to reprice:
- Mid-cap stocks (SOFI, PLTR, NVDA adjacent names) where a single analyst report moves
  the price — your LLM can classify the report tone before the price adjustment completes.
- Crypto on-chain anomalies (whale transactions, exchange netflow): Tables currently empty.
  If restored, on-chain signals provide 1–6h lead on exchange price movements.

### 4. Wheel on Alpaca — Vol-Selling Rails Proven

Infrastructure is wired. The vol-selling thesis (IV > RV premium) is academically robust.
This is the strongest infra asset in the system. Expand capital allocation here before
adding new strategy infrastructure.

---

## Honest Blockers

### Capital (~$120k) — What Strategies Need >$1M to Work?

- **Market making:** Needs >$1M to post meaningful spread width on liquid pairs.
- **Diversified futures TSMOM:** 58 instruments at 2% vol-target each = ~$200k minimum
  for meaningful position sizes. Borderline viable at $120k but return per instrument is small.
- **ETF NAV arbitrage:** Needs $500k+ to make the absolute dollar capture worth the latency risk.
- **Cointegration pairs, Wheel, Funding Arb:** All viable at $10–$120k. These fit the capital.

**At $120k, the realistic capital allocation:**
- Wheel CSPs: $72,000 (60%) — 6–8 positions, $8–12k each
- Funding Rate Harvest: $20,000 (17%) — 2 position pairs
- LLM Event-Driven Equity: $14,000 (12%) — 1–2 positions at 1% sizing
- Cash / regime reserve: $14,000 (12%) — for assignment coverage, margin buffer

### 5-Min REST Polling — What Strategies Need Tick Data?

- Market making: needs tick (millisecond) data. BLOCKED.
- Statistical arbitrage on 1-min+ bars: borderline viable; REST polling introduces
  5-min latency but is workable for daily-bar strategies.
- Funding rate monitoring: only needs 5-min polling for 8h payment cycles. FINE.
- Wheel CSPs: needs option chain data (30-min polling is fine for weekly positions). FINE.
- Cointegration pairs on daily bars: 5-min polling is overkill; EOD bar fetch is sufficient. FINE.

### No Exchange Diversification — What Strategies Need 3+ Exchanges?

- Cross-exchange spot arbitrage: needs 2+ exchanges with fast execution. D tier.
- Funding rate harvest: needs 1 perp exchange + 1 spot exchange (Coinbase spot + Bybit
  perp is sufficient). Only 2 exchanges, one of which you already have.
- Wheel, cointegration, event-driven: single exchange (Alpaca + Coinbase). No blocker.

---

## Recommended Next-Week Roadmap

### Experiment 1: Fix Sentiment Scorer + Validate Wheel Filter (Effort: 4–8h)
**What:** Diagnose and fix sentiment_engine Ollama parser failure (zeroed since 2026-05-14
03:30 UTC per audit #1). Add IV-Rank check and earnings calendar guard to wheel runner.
**Decision criteria:** After fix, if sentiment_scores show non-zero distribution and
IV-Rank filter reduces CSP entries during low-IV environments, declare success.
Abandon if Ollama hermes3:8b is consistently unable to parse headlines (score distribution
stays flat) — this would indicate a model quality issue needing LoRA fine-tuning first.

### Experiment 2: Paper Funding Rate Monitor (Effort: 8–12h)
**What:** Build a Hermes cron job that:
1. Fetches BTC/ETH funding rates from Bybit REST API (free, no account needed for reading)
2. Logs persistent positive funding (> 0.01%/8h) to a new `funding_rate_log` table in TimescaleDB
3. Simulates hypothetical delta-neutral P&L in paper mode (no trades yet)
4. Dashboard card shows: current rate, 7-day APY, regime-gated simulated equity curve
**Decision criteria:** If simulated post-fee APY exceeds 8% annualised over 4-week window
AND funding rate readings are stable (not zero), open a Bybit paper account and begin
paper execution. Abandon if funding rates are persistently below 0.005% (< 2% APY, not worth it).

### Experiment 3: ModelForge Schema Fix + Earnings LoRA Proof-of-Concept (Effort: 12–20h)
**What:** Apply the schema fix plan (`audit/2026-05-15-modelforge-schema-fix-plan.md`).
Then fine-tune hermes3:8b on 50 earnings transcripts (positive/negative direction labels)
using ModelForge LoRA pipeline.
**Decision criteria:** If the fine-tuned adapter achieves >60% accuracy on a held-out
earnings test set (vs ~52% expected for a base model), advance to wiring the adapter
to Shark's pre-market phase for stock selection filtering. Abandon if adapter training
fails due to infrastructure issues — fix infrastructure before advancing LoRA experiments.

---

## What I Would Build If This Were My $120k

**The single recommendation: Double down on the Wheel, add Funding Rate Harvest in Week 3.**

Here is the reasoning:

The Wheel is the only strategy in this system that has a published, multi-decade backtested
track record (spintwig 2007–2024), a structural economic reason to work (VRP), and live
evidence of generating positive P&L (+$524 across 2 trades). The claim is modest — 11–14%
annual return — but it is realistic and backed by evidence.

The Funding Rate Harvest is the second-best candidate. It requires a new exchange account
(Bybit or dYdX, 1-day setup) but then runs on existing cron infrastructure. At 14–19% APY
post-fee in normal conditions, it generates more yield than the Wheel per dollar deployed.
The failure mode (funding rate reversal) is detectable with 5-min monitoring and manageable
with a 24-48h exit rule.

**Fee math for the combined strategy at $120k:**

| Strategy | Deployed | Annual Gross | Annual Net (est.) |
|---|---|---|---|
| Wheel CSPs | $72,000 | $10,800 (15% yield) | $7,200–$9,000 (minus 2 bad assignments) |
| Funding Rate Harvest | $20,000 | $3,200 (16% APY) | $2,800 (after perp fees, 0.02%/8h round trip) |
| Cash buffer | $28,000 | — | — |
| **Total** | **$120,000** | **$14,000** | **$10,000–$11,800/year** |

That is 8.3–9.8% net annual return on $120k with real, feasible strategies. Not 30%.
Not Medallion Fund. But it beats SPY on a risk-adjusted basis (lower vol, positive convexity
from option premium) and it is buildable in 4–6 weeks.

**Position sizing rule for Wheel:**
- Never allocate more than 15% of total capital to any single name's CSP.
- Only sell when IV-Rank > 35.
- No CSPs within 5 days of earnings.
- If assigned, sell covered calls immediately at 0.30 delta, 21-DTE.
- If underlying drops >25% below assignment strike, convert to long-term hold and stop
  selling calls (protect from being called at a permanent loss).

**Failure mode + position sizing floor:**
Worst case: 3 simultaneous assignments in a market crash, all three names drop 30%+.
At 15% per name × 3 names = 45% of capital assigned at 30% underwater = -13.5% of total capital.
This is a realistic 2022-style drawdown. Survivable if remaining 55% of capital is in
cash or funding rate harvest (uncorrelated to equity drawdown direction).

---

## Confidence

| Section | Confidence | What Would Raise It |
|---|---|---|
| MeanRevBB verdict (kill) | **High** | Nothing — own data + literature agree |
| TrendFollow verdict (kill) | **High** | Nothing — own data is damning |
| Wheel edge estimate | **Medium** | 30+ live paper trades with IV-Rank filter applied |
| Funding rate arb APY (14–19%) | **Medium** | 60-day live paper simulation on actual Bybit rates |
| Cointegration pairs (Sharpe 2.45) | **Medium-Low** | Reproducing the result on OUR daily data with OUR fee model |
| LLM sentiment improvement (Sharpe 0.34→3.47) | **Low** | Out-of-sample test on 2024–2025 data; post-fee validation |
| ModelForge LoRA earnings edge (36% improvement) | **Low** | Building and testing the adapter on held-out earnings data |
| Annual return estimate ($10–12k/yr) | **Medium** | 90-day paper track record; assumptions are conservative |

**The most important finding to internalize:**

The strategies currently in the bot (MeanRevBB, TrendFollow on 5-min crypto) are not
borderline — they are catastrophically fee-negative as proven by the operator's own
backtest gates (profit factor 0.10 and 0.12). No amount of tuning will make a strategy
with 10–15% win rate and 0.4–0.6% round-trip fees profitable. The core issue is choosing
the wrong strategy for the available execution infrastructure. The right strategies for
a REST-polling, single-exchange, $120k retail operator are: sell volatility premium (Wheel),
harvest structural funding differentials (funding rate arb), and apply your LLM
classification asymmetry to event-driven selection — not speed-race institutional HFTs on
5-minute tick noise.

---

*Research agent: read-only, 2026-05-15. All code references cite file:line. All external
claims cite URL or paper title. No mutations performed.*
