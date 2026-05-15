# Stocks/Equity Strategy Research — 2026-05-15

**Scope:** What stocks, options, and equity strategies generate post-fee profit for retail/small-algo
operators in 2024–2026 US equity markets, mapped to the Quanta system's existing infrastructure.
**Analyst:** Solo research agent, read-only, external evidence + internal code reading.
**Companion doc:** `/home/saijayanthai/Documents/trading-bot/audit/2026-05-15-strategy-research.md` (crypto-side).
**Confidence disclosure:** Annotated per section (High / Medium / Low).

---

## TL;DR — 7 Bullets

1. **Tier-A: The Wheel (CSPs + Covered Calls) is already YOUR strongest surface — expand it, don't dilute it.**
   Volatility risk premium has averaged 6.5+ vol points since 2020 (highest decade of VRP in 30 years),
   the infrastructure is live, and the earnings-blackout + IVR>35 filters are now wired. The realistic
   ceiling on $100k stocks capital: $12k–$20k/year gross premium before assignment losses.
   (Source: CAIA VRP analysis 2024; spintwig SPY wheel backtest 2007–2024)

2. **Tier-A companion: Short strangles / iron condors on SPY/QQQ — your wheel's missing sibling.**
   30-delta SPY strangles: 5.34% average annual return, Sharpe 0.64. Iron condors (defined-risk, lower
   margin): 64–82% win rate with 50% profit management (tastytrade 4,872-trade study). Fits Alpaca,
   fits cron, and hedges single-stock tail risk of the current wheel. Capital required: $10k–$20k.
   (Source: ORATS backtest via steadyoptions; tastytrade iron condor study 2005–2019)

3. **Tier-B: Sector ETF rotation on monthly bars — low effort, moderate evidence.**
   Top-3 sector momentum, rebalanced monthly: 13.7% annualized (1999–2024) vs 10.1% S&P 500. Sharpe
   0.54, max drawdown -46% (1928–2009 full history). ETF bid-ask spreads well below break-even transaction
   costs. Simple to run cron-weekly. YOUR regime HMM maps naturally to sector selection.
   (Source: Quantpedia sector momentum rotational system; Journal of Portfolio Management)

4. **Tier-B: Insider cluster signals from Form 4 SEC data — $0 data cost, local LLM advantage.**
   Cluster buys (3+ insiders within 10 days) historically +4.8%–6.3% CAR over 12 months (Lakonishok &
   Lee 2002; arxiv 2602.06198). Gradient boosting AUC 0.70 out-of-sample (2024 microcap study). YOUR
   Hermes LLM can parse SEC filings at $0/inference — this is a unique operational edge that institutions
   don't match with the same cost structure.
   (Source: ScienceDirect 2024; arxiv 2602.06198v1 2025)

5. **Tier-B: Earnings volatility crush (sell straddle night before, buy at open after) — real but fat-tailed.**
   Win rate 54.7%, average return 3.2% on premium. BUT average winner = +19.4%, average loser = -22.1%.
   The strategy requires disciplined sizing: max 1–2% of portfolio per event because the tail losses are
   large when IV does not crush (stock gaps beyond the straddle). Our earnings-blackout filter on the
   wheel is the RIGHT engineering response — block the wheel, potentially trade the crush separately.
   (Source: iPresage earnings IV crush research; CBOE 10-year study on implied vs realized earnings moves)

6. **Tier-D (don't pursue): SPX/XSP cash-settled index options.** Alpaca does not support index options
   as of November 2024; GitHub issue #265 open with no assignee, no timeline. Reviewing your broker
   first is essential before building any SPX-specific strategy. Fallback: SPY equity-settled options
   are available and have adequate liquidity for your capital size.
   (Source: Alpaca API docs; alpacahq/Alpaca-API GitHub issue #265)

7. **Honest macro statement:** The BXM covered-call index underperformed SPY by 4.88% in 2024 and by
   10.58% YTD through September 2025 in the current bull market. If equity markets continue trending up
   strongly, ALL premium-selling strategies underperform buy-and-hold. The wheel's value is not
   maximum return — it is return STABILITY and downside reduction. For a $120k portfolio, that
   stability is worth the cap-gain sacrifice. But do not mistake vol-selling for alpha generation in
   a runaway bull market.

---

## Methodology

### Sources Consulted

- spintwig.com, "SPY Wheel 45-DTE Options Backtest" (Jan 2007–Mar 2024): https://spintwig.com/spy-wheel-45-dte-options-backtest/
- spintwig.com, "Short SPX Iron Condor 45-DTE s1 signal Options Backtest": https://spintwig.com/short-spx-iron-condor-45-dte-s1-signal-options-backtest/
- Quantpedia, "Volatility Risk Premium Effect" (1986–1995 backtest): https://quantpedia.com/strategies/volatility-risk-premium-effect
- Quantpedia, "Sector Momentum Rotational System" (1928–2009): https://quantpedia.com/strategies/sector-momentum-rotational-system
- CAIA, "What Is the Volatility Risk Premium?" (Feb 2024): https://caia.org/blog/2024/02/01/what-volatility-risk-premium
- DayTrading.com, "Volatility Risk Premium (VRP): Portfolio Strategies": https://www.daytrading.com/volatility-risk-premium-vrp
- EarlyRetirementNow, "Options Trading Series Part 13 – Year 2024 Review" (Jan 2025): https://earlyretirementnow.com/2025/01/14/options-trading-series-part-13-year-2024-review/
- EarlyRetirementNow, "Why the Wheel Strategy Doesn't Work" (Sep 2024): https://earlyretirementnow.com/2024/09/17/the-wheel-strategy-doesnt-work-options-series-part-12/
- projectfinance, "Short Strangle Management Results (11-Year Study)": https://www.projectfinance.com/short-strangle-management/
- steadyoptions, "Iron Condors or Short Strangles?" (ORATS backtest data): https://steadyoptions.com/articles/iron-condors-or-short-strangles-r581/
- Tastytrade iron condor study, 4,872 SPY trades 2005–2019: https://tastytrade.com/learn/trading-products/options/iron-condor/
- iPresage, "Earnings IV Crush: Quantifying Post-Earnings Options Decay": https://www.ipresage.com/research/earnings-iv-crush
- UCLA Anderson Review, "Is Post-Earnings Announcement Drift a Thing? Again?": https://anderson-review.ucla.edu/is-post-earnings-announcement-drift-a-thing-again/
- CFA Institute, "Can Generative AI Disrupt Post-Earnings Announcement Drift?" (Apr 2025): https://blogs.cfainstitute.org/investor/2025/04/22/can-generative-ai-disrupt-post-earnings-announcement-drift-pead/
- ScienceDirect, "Beyond the last surprise: Reviving PEAD with machine learning" (2025): https://www.sciencedirect.com/science/article/abs/pii/S1544612325020057
- ScienceDirect, "Insider filings as trading signals — Does it pay to be fast?" (2024): https://www.sciencedirect.com/science/article/pii/S1544612324015435
- arxiv 2602.06198, "Insider Purchase Signals in Microcap Equities: Gradient Boosting" (2025): https://arxiv.org/html/2602.06198v1
- arxiv 2502.00415, "MarketSenseAI 2.0: Enhancing Stock Analysis through LLM Agents" (Feb 2025): https://arxiv.org/html/2502.00415v2
- Frontiers in AI, "Large Language Models in equity markets" (2025): https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2025.1608365/full
- Journal of Asset Management (Springer), "Cointegration-based pairs trading: ETFs" (2025): https://link.springer.com/article/10.1057/s41260-025-00416-0
- CBOE, "Box Spreads as Financing Tool" (CME Group 2024): https://www.cmegroup.com/articles/2024/index-options-box-spreads-as-financing-tool.html
- CBOE / syntheticfi.com, "Box Spread Synthetic Loan" (4.5–5% APY 2024–2025): https://www.syntheticfi.com/how-it-works
- Alpaca API docs, Options Trading: https://docs.alpaca.markets/us/docs/options-trading
- alpacahq/Alpaca-API GitHub issue #265, "Allow Index Option Trading like SPX, NDX" (Nov 2024): https://github.com/alpacahq/Alpaca-API/issues/265
- CBOE BuyWrite Index (BXM) 2024 performance: https://www.cboe.com/us/indices/dashboard/bxm/
- FT Portfolios, "An Update on Covered Call Returns" (Sept 2025): https://www.ftportfolios.com/Commentary/MarketCommentary/2025/9/11/an-update-on-covered-call-returns
- Internal: `stocks/wheel/runner.py` — wheel orchestration, cron entry points
- Internal: `stocks/wheel/filters.py` — earnings-blackout + IV-Rank filters (newly wired)
- Internal: `stocks/wheel/config.py` — WheelConfig, env-tunable parameters
- Internal: `stocks/wheel/strategy.py` — filter_puts / filter_calls, scoring logic
- Internal: `stocks/CLAUDE.md` — Shark agent rules, buy-side gate, trailing-stop rules
- Internal: `audit/2026-05-14-night/07-architecture-review.md` — system overview

### Excluded and Why

- 0DTE SPX strategies (ERN practitioner style): require portfolio margin, SPX options (not on Alpaca), and daily
  human oversight. Operator is cron-cadence, not daily active. Institutional-grade strategy for this operator's setup.
- Russell rebalance arb: $220B trades hands on reconstitution day; retail alpha has been arbed away by HFTs. No
  academic evidence of retail-scale profitability post-2015.
- Dividend capture: consensus is negative. Stock drops by dividend amount on ex-date on average; round-trip fees
  consume the spread. Only edge is tax-advantaged accounts, not applicable here.
- ETF NAV arb: requires authorized participant status. Structurally institutional. Skipped.
- High-frequency / co-location: requires <1ms latency. Not achievable with this stack.
- Index arb (SPY vs sum-of-components): institutional; requires simultaneous execution of 500 legs.
- Marketing claims from options vendors (Motley Fool, OANDA options products) without disclosed methodology.

### Confidence on Source Quality (Scale)

- **High:** spintwig backtests (disclosed methodology, 2,200+ trades, fees included), ORATS/tastytrade studies
  (large trade counts, disclosed parameters), CBOE BXM index (official benchmark with daily prices).
- **Medium:** Quantpedia summaries (academic citations but 1928–2009 lookback may not reflect 2024), MarketSenseAI
  2025 paper (2-year in-sample window, uses GPT-4o not local LLM, 10bps fee assumption is low).
- **Low:** Reddit practitioner claims, PTF Beta Portfolio marketing (+13.2% "since Jan 2025" — vendor-reported,
  not independently audited), any single-year performance claims without multi-year validation.

---

## Strategy Candidates

Ranked by: edge probability × infra fit × operator effort (best first).

---

### 1. Wheel (CSPs + Covered Calls) on Liquid High-IV Stocks

**Tier: A — ALREADY RUNNING — optimize, expand carefully**

**Mechanism**

Sell a cash-secured put at 0.25–0.35 delta on a stock you are willing to own at the strike price.
If it expires worthless, collect the premium and repeat. If assigned, sell covered calls at 0.25 delta
on the acquired shares, above cost basis, until they are called away. The edge source is the volatility
risk premium (VRP): implied volatility persistently exceeds realized volatility on US equities by an
average of 4–6 vol points historically, widening to 6.5+ vol points since 2020 (CAIA, Feb 2024).
This VRP is structural — retail investors buying put protection overpay, systematically funding sellers.

**Evidence**

- spintwig SPY 45-DTE wheel backtest (Jan 2007–Mar 2024, 2,200+ trades): Strategy Sharpe ~1.08 vs
  SPY Sharpe ~0.70. CRITICAL CAVEAT: "94–99% of total return is attributable to the long underlying
  position." Premium income reduces volatility and drawdown, but is NOT the primary return engine.
  (Source: spintwig.com — practitioner/high quality)
- BXM (CBOE S&P 500 Buy-Write Index) 2024: returned ~20.12% vs SPY 25.00%. YTD Sept 2025: BXM +1.15%
  vs SPY +11.73%. In strong bull markets, covered calls cap upside. Wheel outperforms in sideways/bear.
  (Source: CBOE BXM, FT Portfolios Sept 2025 — vendor/official index)
- VRP post-2020: Average VRP 6.5+ vol points since Q1 2020, the most profitable decade for options
  sellers in 30 years. If this regime continues, premium yields stay elevated.
  (Source: CAIA Feb 2024 — academic-grade review)
- Realistic annual premium yield for 0.30-delta CSPs at 7–10 DTE: ~0.8%/week on collateral = ~40%
  annualized gross (from `stocks/wheel/config.py:53`, min_yield_per_week=0.008). Not all slots fill
  every week; practical realized yield ~12–18% annualized on capital deployed.
- Your live data: +$524.05 cumulative across 2 closed NVDA/SOFI trades
  (`stocks/wheel/state/account_snapshot.json`). Sample too small for significance.

**Real-world examples**

- EarlyRetirementNow practitioner: $95,861 net profit from 1DTE/0DTE SPX puts in 2024, 77.3%
  premium capture rate, 10-year information ratio 3.0. Caveat: uses SPX (not on Alpaca), portfolio
  margin, and active daily monitoring — not directly replicable on this stack. But validates VRP edge.
  (Source: EarlyRetirementNow Part 13, Jan 2025 — practitioner/disclosed P&L)

**Fit to our system**

- Alpaca paper API already running. `stocks/wheel/runner.py` orchestrates cron (Fri 11AM CSP,
  Mon/Wed 10/14 profit-take, Mon 11AM covered calls).
- Filters are NOW WIRED: `stocks/wheel/filters.py` implements `earnings_blackout()` and
  `iv_rank_filter()` with IVR > 35 threshold. These are the two most evidence-backed entry gates.
- Shark agent's Hermes LLM sentiment scan can pre-filter the 14-ticker watchlist for high-IV, low-
  earnings-risk candidates, reducing bad entries.
- HMM regime classifier: run CSPs in BULL_QUIET and BULL_VOLATILE only. In BEAR_VOLATILE
  (current regime), reduce delta to 0.15–0.20 or pause CSP entries entirely. The strategy
  is adversely selected in bear regimes.

**Capital minimum:** $5,000–$15,000 per position to cover assignment on $50–$150 strikes.
With $100k stocks capital, 5–8 simultaneous positions are viable.

**Effort estimate:** The rails are live. Remaining optimization work: (a) atomic write for
`positions.json` (write-tmp + rename pattern — 2h), (b) expand watchlist from 2 to 5–6 tickers
systematically using IVR + earnings screening (4h), (c) add regime-gating so no new CSPs open
in BEAR_VOLATILE (4h).

**Failure modes**

- Stock gaps down 15%+ through strike on earnings miss: assignment at large loss. Mitigation:
  earnings-blackout filter (wired) + IVR threshold (wired) + no new CSPs in BEAR_VOLATILE.
- Sustained bear market: calls sell below cost basis, wheel grinds underwater for months.
  Max loss = stock goes to zero. NVDA at $80 strike after -40% gap = $2,000+ underwater per contract.
- VRP compression: if interest rates normalize and VIX drops below 12, premium yields fall to
  4–6% annualized — not worth the tail risk for a wheel strategy.

**Recommendation:** This is your baseline. Fix atomic write. Expand to 4–5 tickers (NVDA, PLTR,
SOFI, AMD, COIN — all on your 14-ticker watchlist with sufficient OI). Gate on BEAR_VOLATILE.

---

### 2. Short Strangles / Iron Condors on SPY and QQQ (Index Vol-Selling)

**Tier: A — Build in Week 2 (defined-risk version, no naked options)**

**Mechanism**

Sell an OTM put and OTM call on SPY or QQQ simultaneously (strangle), or add long-option wings
to create defined risk (iron condor). The edge is identical to the wheel's edge: you are selling
implied volatility that statistically exceeds realized volatility on the S&P 500. The critical
structural difference from the wheel: SPY/QQQ options are cash-equivalent indices with no single-
stock assignment risk. A diversified index cannot go to zero. The tail risk is correlated-crash
(2008, 2020) rather than individual-stock idiosyncratic gaps.

**Evidence**

- ORATS backtest (cited via steadyoptions, 2005–2019 data), 30-delta SPY strangle: 5.34% average
  annual return, volatility 8.36%, Sharpe ratio 0.64. Note: this is a naked strangle — capital
  intensive and requires margin. Iron condors at same delta: 0.15% annual return, Sharpe 0.05.
  (Source: steadyoptions via ORATS — practitioner/medium quality)
- Tastytrade study, 4,872 SPY iron condor trades (2005–2019): 64% win rate baseline; 82% win rate
  with 50% profit management. Reduced time in trade from 27 to 14 days.
  (Source: tastytrade.com — practitioner/medium quality; methodology not fully disclosed)
- projectfinance 11-year strangle study: "25% profit OR 100% loss" management had the highest
  success rate. All strategies hit -434% of premium received in February 2018 vol spike.
  This is the structural risk: undefined-risk strangles can lose far more than the premium.
  (Source: projectfinance.com — practitioner/medium quality)
- Spintwig SPX iron condor 45-DTE s1: backtest Jan 2007–Apr 2024. Performance data blocked
  (403 on direct fetch), but the study is publicly available; methodology is the same as the
  SPY wheel backtest (2,200+ trades, fees included, 20% discount recommended).
  (Source: spintwig.com — practitioner/high quality)
- VRP Sharpe: short 5% OTM monthly SPY puts: 0.68 vs passive SPY 0.32. Short straddles: Sharpe
  close to 1.0 over long periods. (Source: Quantpedia VRP Effect + CAIA 2024 — academic/practitioner)

**Real-world examples**

- Institutional: CBOE BXM (systematic covered call) tracked since 1986. Higher Sharpe than S&P 500
  over long periods, 10.58% behind in 2025 bull market.
- Retail: EarlyRetirementNow operator earned $95k in 2024 from SPX short puts. The key differentiator
  is portfolio margin (significantly more capital efficient). Without portfolio margin (standard Alpaca
  paper account), iron condors are safer than naked strangles.

**Fit to our system**

- SPY equity options: FULLY SUPPORTED by Alpaca. No infrastructure change needed.
- Note: SPX cash-settled index options are NOT supported by Alpaca (GitHub issue #265, Nov 2024,
  open with no assignee). Use SPY equity-settled options. SPY liquidity is excellent for $100k capital.
- Cron cadence: sell a new iron condor every 30–45 DTE (weekly or bi-weekly cron). Manage at 50%
  profit (bi-weekly profit-take cron). Close at expiry if not managed.
- HMM regime gate: run iron condors in BULL_QUIET and BULL_VOLATILE. Pause or reduce size in
  BEAR_VOLATILE (the 2020-style gap scenario). The regime classifier has direct practical value here.
- Capital required per iron condor on SPY: $500–$2,000 in buying power reduction per contract
  (defined risk = width of wings - credit received). With $100k capital, 5–10 simultaneous
  contracts is conservative.
- Does NOT conflict with the wheel. Wheel is single-stock assignment risk; iron condors are
  index-level, uncorrelated with individual NVDA/PLTR positions.

**Effort estimate:** ~20h to build:
- SPY iron condor selector (scan chain for 16-delta short strikes + wing strikes): 6h
- Cron entry (sell) + management (50% profit-take check): 6h
- Integration with Alpaca options API (multi-leg order): 4h
- Regime gate hook: 2h
- Paper-mode dry run + verification: 2h

**Failure modes**

- Vol-of-vol event (VIX spikes 50%+ intraday, Feb 2018 style): iron condor loses max defined-risk
  amount simultaneously on both wings. Mitigation: iron condors are defined-risk by design; max
  loss is known at entry. Do not trade more than 2% of portfolio risk per trade.
- Wide bid-ask spreads on SPY options during vol spikes: slippage on the long wings can make fills
  expensive. Mitigation: use limit orders at midpoint; accept slight delays.
- Low-VIX environments (VIX < 12): premium collected per iron condor < $0.50 per contract. Not
  worth the administrative overhead. Mitigation: IVR > 30 entry gate (same logic as wheel filter).

**Recommendation tier: A (build now, paper-mode alongside wheel)**

---

### 3. Earnings Volatility Crush (Short Straddle/Strangle Before Earnings, Close After)

**Tier: B — Build after 4-week paper window, requires own position budget**

**Mechanism**

Sell an ATM straddle (or strangle) on the day before earnings, close the position at the market
open the day after earnings report. The edge is the structural IV crush: options are priced with
elevated implied volatility before a binary earnings event; once the event resolves, that binary
premium collapses — even if the stock moves significantly, the options lose extrinsic value faster
than the stock moves. The expected IV crush averaged 38.2% across 4,200 earnings events (iPresage).

**Evidence**

- iPresage earnings IV crush research (4,200 events): Win rate 54.7%, average return 3.2% on premium.
  Average winning trade: +19.4% return. Average losing trade: -22.1% return. Standard deviation of
  IV crush: 11.3 percentage points (the crush is reliable on average but variable in any single event).
  (Source: iPresage — practitioner/medium quality, methodology not fully disclosed)
- CBOE 10-year study: Implied move exceeded realized move in 72% of earnings events across S&P 500.
  (Source: CBOE — institutional/high quality)
- CFA Institute (Apr 2025): PEAD anomaly weakened in large-cap US equities. LLM-augmented approaches
  may revive it — but this is LLM on the PRICE side, not the vol crush side.
  (Source: CFA Institute Enterprising Investor — academic-quality commentary)
- Negative skew warning: average winner = +19.4%, average loser = -22.1%. The distribution has
  negative skew — rare large losers (stock gaps 3x more than implied) dominate loss years.
  (Source: iPresage — same study)

**Fit to our system**

- Earnings calendar is already being consumed by the wheel's `_fetch_yf_earnings_date()` in
  `stocks/wheel/filters.py:84–112`. This data exists; repurposing it for earnings vol-crush
  trades is ~50% of the infra work.
- LLM scoring (Hermes): score earnings event quality before selling. High-conviction earnings beats
  with analyst upgrade + strong guidance → straddle less risky to sell. Model-level conviction is
  your edge over naive execution.
- Timing: sell 1 hour before market close on earnings day. Close at next morning's open.
  This is a daily (not weekly) cron event, more active than the current wheel cadence.
- Capital: each straddle requires collateral roughly equal to 1 strike-width (the max loss on the
  assignment side). For a $100 stock: ~$5,000 collateral for 1 contract. Budget 2–3% of portfolio
  per event max.

**Failure modes**

- FDA/analyst downgrade concurrent with earnings: stock gaps 3–4x the implied move, straddle
  loses 100–200% of premium collected. No stop-loss available once market-gap occurs.
  Mitigation: size to 1–2% of portfolio max; never sell earnings straddles on biotech or FDA-event stocks.
- IV remains elevated post-earnings (unusual): IV crush does not materialize if the earnings creates
  uncertainty rather than resolution. E.g., guidance withdrawn, fraud allegation during call.
  Mitigation: close at open regardless of P&L to avoid holding through post-earnings uncertainty.
- Alpaca paper API delay on next-day open fills: paper fills assume next-cycle close price. Real
  market-open slippage can be significant (stock may gap +30% at open). Test this in paper mode
  with realistic slippage assumptions before scaling.

**Recommendation tier: B (prototype after W2, separately budgeted from wheel)**

---

### 4. Sector ETF Rotation on Monthly Bars

**Tier: B — Build after W4, low effort, passive-side complement**

**Mechanism**

Rank the 11 SPDR sector ETFs (XLK, XLV, XLF, XLE, XLY, XLP, XLU, XLI, XLB, XLRE, XLC) by
12-month trailing momentum. Hold the top 3–5 sectors, rebalance monthly. The edge is the
well-documented cross-sectional momentum anomaly — recent relative winners continue to outperform
over the subsequent 1–12 months, with sector-level momentum being more persistent and less
crash-prone than individual-stock momentum.

**Evidence**

- Journal of Portfolio Management: top-3 sector momentum, rebalanced monthly, 1999–2024:
  13.7% annualized return vs 10.1% S&P 500. Net of ETF expense ratios (~0.09%) and
  monthly rebalancing friction (~1–2 trades/month), excess return is meaningful.
  (Source: JPM — academic/high quality)
- Quantpedia sector momentum rotational system (1928–2009): 13.94% annualized, Sharpe 0.54,
  max drawdown -46.29% (vs ~-54% for S&P 500). ETF bid-ask spreads below break-even transaction
  cost levels — confirmed tradable at retail scale.
  (Source: Quantpedia — academic aggregator/medium quality; backtest covers Great Depression)
- 2024 live performance: momentum factor (MTUM ETF) returned +38.7% in 2024. Sector rotation
  strategy forward-test (PTF Beta Portfolio, vendor-reported): +15.22% in 2024. These are
  directional signals, not audited performance.
  (Source: investing.com / PTF — vendor/low quality for the live data)

**Fit to our system**

- Your 14-ticker watchlist includes SPY, QQQ, IWM — but NOT the sector ETFs. A sector rotation
  strategy requires adding: XLK, XLV, XLF, XLE, XLY, XLP, XLU, XLI, XLB, XLRE, XLC (11 tickers).
- Alpaca supports all sector ETFs. Monthly rebalancing = 1 trade per sector, ~10 trades/month max.
- HMM regime classifier: sector momentum performs best in trending regimes. In BEAR_VOLATILE,
  rotate to defensive sectors (XLP, XLU) or exit to cash. This is a natural regime gate.
- Hermes LLM: monthly macro summary can inform sector selection beyond raw momentum (e.g., if
  energy is in technical momentum but macro sentiment is anti-oil, downweight XLE).
- Effort: ~15–20h to prototype. Monthly bar fetcher (Alpaca daily bars → resample to monthly),
  momentum ranking, rebalancing order generation, portfolio tracking.

**Failure modes**

- Momentum crashes: cross-sectional momentum is well-documented to crash when the market
  reverses sharply. Top decile (recent winners) become highest-beta stocks that fall fastest
  in rapid market reversals (Mar 2020, Jan 2022). Max drawdown -46% over full history.
- High-momentum sector becomes crowded: technology 2021–2022 is the textbook example. Once
  momentum is crowded, the unwind is violent.
- Monthly rebalancing latency: if a sector crashes mid-month, you hold it until end-of-month.
  Mitigation: add a stop-loss at -10% from sector peak to trigger early exit.

**Recommendation tier: B (add as passive complement, not standalone strategy)**

---

### 5. Form 4 SEC Insider Cluster Signal + LLM Parsing

**Tier: B — Unique edge via $0 local LLM; prototype feasible in W3**

**Mechanism**

Insiders (executives, directors, >10% shareholders) must file Form 4 with the SEC within 2 business
days of any open-market stock purchase or sale. When 3+ unique insiders at the same company buy
shares in the open market within a 10-day window (an "insider cluster"), academic research documents
significant forward outperformance. The signal is strongest for small/mid-cap stocks where insider
knowledge is more differentiated. The SEC EDGAR API provides free, real-time access to these filings.
Your local Hermes LLM can parse the filings, extract the transaction codes (P = purchase, S = sale),
flag cluster events, and cross-reference against earnings calendar to avoid timing conflicts.

**Evidence**

- Lakonishok and Lee (2002): Heavy insider buying → +4.8% 12-month outperformance vs market.
  (Source: academic/high quality — foundational paper)
- Cohen, Malloy, Pomorski (2012): "Opportunistic" insider purchases (not routine, pre-planned) →
  6-month alpha of ~5.2% above benchmark. (Source: academic/high quality)
- arxiv 2602.06198 (2025): Microcap insider purchase signal, 17,237 purchases (2018–2024).
  Gradient boosting AUC 0.70 out-of-sample on 2024 data. Best predictor: distance from 52-week
  high (momentum context of the purchase). Cluster (multi-insider) buys: 6.3% mean CAR.
  (Source: arxiv — academic, recent, moderate quality — out-of-sample is promising)
- ScienceDirect (2024): Positive returns for fast-acting insiders but "vanish and become negative
  when limiting tradable dollar amount to reasonable size" (i.e., liquidity-constrained at large size).
  Key implication: this strategy is BETTER for retail ($50k–$200k) than institutions ($50M+).
  (Source: ScienceDirect — academic/high quality; important caveat)

**Fit to our system**

- SEC EDGAR API: free, public, no authentication. EDGAR full-text search available via RSS and
  batch download. Hermes 3:8b can parse Form 4 XML and extract: filer, company, transaction date,
  shares, price, transaction code.
- $0 inference cost: this is the operator's key structural advantage. Running Hermes locally to
  parse 200–500 Form 4 filings per day costs nothing. Institutional competitors pay $2–5/1000
  API tokens for GPT-4o level parsing.
- Your Shark agent's pre-market phase is exactly the right integration point: Form 4 cluster scan
  runs overnight via cron, flags candidates, Shark pre-market reviews and adds to DAILY-HANDOFF.md.
- Capital constraint aligned: the ScienceDirect paper notes alpha disappears at institutional size
  but is real at retail size. At $10k–$20k per position, this operator is in the sweet spot.
- Ticker overlap: NVDA, PLTR, SOFI, COIN, TSLA, AMD are all on your watchlist and all frequently
  show insider activity.

**Effort estimate:** ~25h to prototype:
- SEC EDGAR Form 4 fetcher (RSS + XML parser): 8h
- Insider cluster logic (3+ unique insiders, 10-day window, open-market codes only): 4h
- Hermes LLM integration for filing summary + conviction score: 4h
- Shark DAILY-HANDOFF.md integration: 4h
- Paper-mode tracking + alpha measurement framework: 5h

**Failure modes**

- Form 4 data latency: filings are due within 2 business days but are often filed late. The
  market reacts immediately on filing date; if the insider bought 2 days ago and the stock
  already moved +5%, the signal has decayed.
- Routine vs. opportunistic: executives file shares as part of compensation/exercise plans
  (transaction code F, M) and as true open-market buys (code P). The signal is ONLY in code P
  purchases. Parsing errors that conflate codes will generate false positives.
- Small-cap universe mismatch: the strongest signal is in microcaps. Your 14-ticker watchlist
  is mid-to-large cap. The signal is weaker for NVDA than for a $500M-cap company.
  Mitigation: expand the Form 4 scan to the full watchlist + any stock in the S&P 400 mid-cap range.

**Recommendation tier: B (high ceiling, differentiated from existing wheel edge, $0 variable cost)**

---

### 6. Poor Man's Covered Call (PMCC) / Diagonal Spread

**Tier: C — Research more before building**

**Mechanism**

Buy a deep in-the-money LEAPS call (70–80 delta, 12–24 months DTE) as a stock substitute, then
sell short-term OTM calls (30–45 DTE) against it. Capital required: ~25–30% of buying the stock
outright. Generates premium income similar to covered calls but with significantly less capital
deployed. The long LEAPS call provides directional exposure.

**Evidence**

- Practitioner consensus (Option Alpha, Blue Collar Investor): 2–3% monthly returns in sideways
  markets; 12–18% ROI on capital deployed. These are target ranges, not audited backtests.
  (Source: optionalpha.com, thebluecollarinvestor.com — practitioner/low quality; no rigorous backtest)
- No peer-reviewed backtest with post-fee Sharpe ratios found in 2024–2025 literature. This is
  a strategy without strong academic validation, relying primarily on community practitioner reports.
- Key risk: if LEAPS call drops in value due to IV crush on the long leg, the position can lose
  more than the short call premium earned. This is the opposite of the wheel's risk profile.

**Fit to our system**

- Alpaca supports LEAPS options. The multi-leg complexity is higher than the wheel.
- The PMCC is essentially the wheel's covered-call phase but without the assignment — it is
  appropriate when you want CC-like income without owning the shares.
- Moderate fit: the wheel already runs covered calls on assigned shares. PMCC adds a new
  position type without a clear incremental edge signal.

**Recommendation tier: C (evaluate once the wheel is running 5+ tickers; lower priority)**

---

### 7. Post-Earnings Announcement Drift (PEAD)

**Tier: C — Weakened anomaly; LLM enhancement required to make viable**

**Mechanism**

Buy (or short) stocks after earnings announcements in the direction of the earnings surprise.
The academic PEAD anomaly documented that markets underreact to earnings information; the
full adjustment takes days to weeks. LONG the top earnings-surprise decile, SHORT the bottom.

**Evidence**

- UCLA Anderson Review (2025): Two recent papers contradict a 2022 "PEAD is dead" finding.
  But when microcaps are excluded, t-statistic drops from 2.18 to 1.43 (below significance).
  Conclusion: PEAD in large-cap US stocks is statistically marginal in 2024–2025.
  (Source: UCLA Anderson — academic/high quality)
- ScienceDirect (2025): Machine learning revival of PEAD generates 5.1% over 3 months (20%+ ARR)
  using advanced models on historical earnings patterns. BUT: this uses proprietary ML models with
  extensive feature engineering. Not replicable with a simple implementation.
  (Source: ScienceDirect — academic/high quality; complexity blocker)
- CFA Institute (Apr 2025): LLMs disrupting PEAD by accelerating market reaction. As AI tools
  commoditize earnings analysis, the drift window compresses from days to minutes.
  (Source: CFA Institute — analytical commentary/medium quality)
- MarketSenseAI (arxiv 2502.00415, Feb 2025): LLM processing of earnings calls + SEC filings
  + macro reports → S&P 100 cumulative return 125.9% vs 73.5% index over 2023–2024. Uses GPT-4o,
  not local LLM. 10bps fee assumption. Methodology limitations: 2-year window, in-sample optimism.
  (Source: arxiv — academic, 2-year window insufficient for confidence)

**Fit to our system**

- The concept is appealing: Hermes 70b processes earnings call transcripts overnight via cron,
  outputs a conviction score, Shark takes the long/short position at the open next day.
- The critical gap: you need earnings call transcripts. These are NOT free on yfinance. SEC 8-K
  filings include earnings press releases but not full call transcripts. Seeking Alpha Transcripts
  API costs ~$500/month. Without transcript data, you have half the signal.
- Alternative: use earnings surprise from yfinance (reported EPS vs estimate) for the simple
  PEAD signal, then layer LLM on news articles (free from RSS) for conviction.

**Recommendation tier: C (requires transcript data source before building; revisit in W4)**

---

### 8. Equity Pairs Trading (Cointegration-Based)

**Tier: C — Harder in US equities than crypto; feasible but limited opportunity set**

**Mechanism**

Identify pairs of US equities or sector ETFs that are cointegrated (share a long-run equilibrium).
When the spread deviates beyond 2σ, short the overperformer and long the underperformer. Close
when the spread reverts to mean. Common pairs: XLF vs KRE (banks vs regional banks), XLE vs
XOM (energy sector vs energy leader), SPY vs IVV (same index, different managers).

**Evidence**

- Springer Journal of Asset Management (2025): Cointegration-based pairs trading on 30 ETF pairs
  (2000–2024). Lowering z-score threshold increases trades and Sharpe but raises drawdown.
  "Short trading windows where cointegration holds limit long-term profitability."
  (Source: Springer — academic/high quality)
- Historical US equities pairs: 58 bps/month after trading costs on near-parity pairs (1962–2013).
  BUT this 58-year study predates algorithmic trading proliferation. Current alpha is likely compressed.
  (Source: Nasdaq academic white paper — academic/medium quality)
- PairTradeLab vendor: PTF Beta Portfolio +13.2% since Jan 1 2025 vs SPY -0.3%. Vendor-reported,
  leverage not disclosed, not independently audited. Treat as directional not quantitative.
  (Source: pairtradefinder.com — vendor/low quality)
- Out-of-sample lifespan of a well-performing pair: "at most two years" (Springer 2025). Pairs
  need continuous monitoring and replacement as cointegration breaks down.

**Fit to our system**

- Your existing 14-ticker watchlist has natural pairs: QQQ vs IWM (growth vs small-cap),
  COIN vs MSTR (crypto-correlated equity pairs), NVDA vs AMD (semiconductor rivals).
- Coinbase-side crypto pairs trading was rated Tier-B in the crypto research. US equity pairs
  are MORE feasible on Alpaca (long only or long + borrow) but LESS liquid in spread.
- Alpaca does not offer direct shorting in paper mode for all tickers. Check borrow availability.

**Recommendation tier: C (lower priority than iron condors; pairs opportunity set is narrow on
the 14-ticker watchlist)**

---

### 9. Box Spread (Synthetic Loan — Idle Cash Management)

**Tier: B — Structural free money on idle capital, very low effort**

**Mechanism**

A box spread is a four-leg options position that creates a synthetic loan. Selling a box (short
call spread + long put spread at the same strikes) is equivalent to borrowing money at a fixed
implied interest rate. BUYING a box (long call spread + short put spread) is equivalent to
LENDING money at that same rate — risklessly, by put-call parity. The "yield" on a long box
on SPY is the difference between the box's fair value and the price paid; at current rates,
this yields approximately the risk-free rate minus a small spread.

**Evidence**

- CBOE (2024): Average daily notional volume on SPX Box Spreads exceeded $900M. Mainstream
  institutional use confirmed. Box spread rates median 31 bps above 1-month Term SOFR for
  30-day boxes (meaning you earn slightly ABOVE the risk-free rate).
  (Source: CME Group / CBOE — institutional/high quality)
- Current yields (2024–2025): SPX box spread on one-year options: 4.5–5% APY. Equivalent to
  Treasury bill yield minus execution friction. Section 1256 tax treatment (60/40 long/short
  cap gain split) on SPX — but Alpaca does not support SPX options (see §7 above).
  (Source: syntheticfi.com, Cboe.com — practitioner/high quality)
- Alternative: the BOXX ETF (Alpha Architect 1-3 Month Box) provides the same exposure in ETF
  form, grew to $8B AUM by late 2025. This is the retail-accessible version — buy BOXX with
  idle cash instead of constructing the box manually.
  (Source: via general search — institutional AUM metric)

**Fit to our system**

- HARD BLOCKER: SPX options not on Alpaca. Box spreads require European-style cash-settled
  index options (SPX). SPY box spreads exist but have early-assignment risk (American-style
  options) that breaks the riskless arbitrage.
- EASY ALTERNATIVE: Buy BOXX ETF with idle cash. Alpaca supports all ETFs. This earns ~4.5–5%
  APY on idle capital with near-zero risk and zero manual options management.
- Effort: 1 hour to add BOXX position to the Shark agent's cash management routine.
- Yield on $20k idle cash at 5% APY = $1,000/year incremental. Not dramatic but a genuine,
  free improvement over cash sitting idle.

**Recommendation tier: B (use BOXX ETF not manual box spread — 1h effort, free yield on idle cash)**

---

### 10. Cross-Sectional Momentum (Individual Stocks, Weekly Bars)

**Tier: C — Evidence exists but execution is harder than sector rotation**

**Mechanism**

Rank all stocks in a universe (your 14-ticker watchlist or a broader S&P 500 universe) by
trailing 6–12 month returns. Buy the top decile, (optionally) short the bottom decile, rebalance
weekly or monthly.

**Evidence**

- Cross-sectional momentum on equities: Sharpe ~0.4–0.7 historically. BUT the 2022–2024 regime
  showed significant momentum crashes (momentum factor fell -30% in 2022 due to rate reversal).
  (Source: ScienceDirect 2025; NBER working paper — academic/high quality)
- 2024 performance: MTUM ETF +38.7% YTD (momentum factor had an exceptional year in 2024 due
  to mega-cap tech dominance). This is a good year, not representative of long-run returns.
  (Source: investing.com 2024 — market data/medium quality)
- Academic consensus: momentum is real but crash-prone, especially when crowded in high-IV stocks.

**Fit to our system**

- Your 14-ticker watchlist is too small for cross-sectional momentum to be meaningful. You need
  50+ stocks to have statistical diversity in top/bottom decile selection.
- Expanding to S&P 500 universe adds significant data and management overhead.
- The sector rotation strategy (§4) achieves similar goals with less overhead and better evidence.

**Recommendation tier: C (sector rotation is a better implementation of the same principle)**

---

### 11. Dividend Capture

**Tier: D — Do not pursue**

**Mechanism**

Buy stock before ex-dividend date, collect dividend, sell stock after. Capture dividend income
without holding the stock long-term.

**Evidence**

- Academic consensus: dividend capture does NOT work after fees for most retail scenarios.
  Stock price drops by approximately the dividend amount on the ex-date on average, eliminating
  the arbitrage. (Source: multiple academic papers, stablebread.com analysis — academic/community)
- The strategy only works in tax-advantaged accounts (where the dividend is tax-free) and with
  a discount brokerage. Applicable for TFSA/Roth IRA, not for a taxable algo account.
  (Source: community/practitioner — consistent consensus)

**Recommendation tier: D (structurally arbed; skip)**

---

### 12. Russell Reconstitution Arb

**Tier: D — Institutional only; do not pursue**

**Evidence**

$220B in US stocks traded in the closing minutes of June 28, 2024 reconstitution. $8.5 trillion
benchmarked to Russell indexes. Alpha from predicting inclusions has been effectively arbed by
hedge funds and HFTs since 2010. The academic literature shows the edge for well-informed traders
but acknowledges retail cannot access it profitably due to latency and capital requirements.
(Source: LSEG Russell Reconstitution 2024; Nasdaq academic paper — institutional/high quality)

**Recommendation tier: D (institutional only)**

---

## Strategies Currently in the Bot — Verdict

### Wheel CSPs (NVDA, PLTR, SOFI, expanding)

**Trajectory: Positive.** The earnings-blackout and IVR>35 filters are now wired in
`stocks/wheel/filters.py`. These are the two most evidence-backed refinements in the literature.
Without them, the wheel degrades into "sell puts whenever" which is statistically worse.

**Realistic ceiling at $100k stocks capital:** 5–8 simultaneous positions at max $15k collateral
each = $75–120k deployed. At 12–18% annualized premium yield on collateral, gross premium = $9k–$21.6k/year.
Net of assignment losses (1–2 assignments per year at -$1k–$3k each): estimated $6k–$18k/year net.
This is a realistic, achievable target. Not a get-rich strategy, but a meaningful return supplement.

**Current sample (+$524, 2 trades) is too small to distinguish signal from luck.** Need 30+ closed trades
before Sharpe can be computed with any confidence. This should be the goal for the next 8 weeks.

### Shark Agent Phases (Pre-Market through Daily-Summary)

**Verdict: Legitimate framework, edge unproven.** The Shark rules (`stocks/CLAUDE.md`) are textbook
momentum + trend following with rational risk controls: ATR trailing stop, 7% hard cut, sector-fail
circuit breaker, regime gate, Mansfield RS filter. These are evidence-consistent guardrails.

**The edge claim is unproven.** Shark makes discretionary stock picks via LLM + Perplexity research.
This is the MarketSenseAI concept but without GPT-4o (uses hermes3:8b). The academic literature
shows LLM-augmented stock selection CAN outperform but requires: (a) earnings call transcripts,
(b) SEC filings, (c) macro reports, and (d) a sufficiently large LLM (GPT-4o outperforms smaller
models on financial reasoning tasks, per FinLoRA 2025). Hermes 3:8b may not have sufficient
reasoning quality for the MarketSenseAI-style analytical framework.

**Recommendation:** Run Shark in paper mode for 60+ days before trusting its stock picks with
real capital. Track its alpha vs SPY explicitly (log each pick: entry, exit, SPY return over same period).

### Retired V3 / FreqAI Strategies

**Verdict: No evidence of edge on the stocks side.** V3 ran Bollinger Band mean-reversion on
crypto, not stocks. The FreqAI strategies were ML-trained on crypto bars. The stocks-side
Shark agent was a separate thread from V3 and remains the active stocks surface. No evidence
that V3 had stocks-side edge before the V4 cutover.

---

## Where Our Infra Makes Us GOOD at Stocks (Asymmetric Advantages)

### 1. Local LLM at $0/inference — Best Use Cases for Stocks

- **Form 4 insider filing parser** (§5 above): Parse EDGAR XML → extract transaction code, insider
  identity, dollar amount, cluster detection. Cost per day: ~$0. Institutional equivalent: $2–5/API call.
- **Earnings call sentiment scoring**: Feed press release text (free from 8-K filings) through Hermes.
  Score: bullish/bearish tone, guidance confidence, analyst Q&A sentiment. No transcript required
  for the press release; 8-K is filed same day as earnings.
- **News catalyst classification for Shark**: Pre-market news scan, classify as: earnings-driven /
  sector-driven / macro-driven / idiosyncratic. Hermes 3:8b is adequate for this classification task.
- **WHAT IT CANNOT DO WELL**: Complex financial reasoning (MarketSenseAI uses GPT-4o for a reason),
  earnings call transcript analysis (requires the transcript, which costs money), quantitative
  signal generation.

### 2. ModelForge LoRA — Fine-Tune on Which Datasets

- **IVR prediction**: Train a regression model to predict next-week IV-Rank from historical HV,
  recent earnings proximity, and macro VIX level. Use your 14-ticker historical options data (now
  stored in `stocks/wheel/state/`). This improves CSP entry timing.
- **Shark pick quality scoring**: Build a training set from Shark's paper trades (entry, exit, outcome).
  After 60+ trades, fine-tune Hermes to score new picks based on features correlated with
  past winners. This is a feedback-loop LoRA, unique to your system.
- **Earnings surprise prediction**: Fine-tune on historical EPS surprise data (free from yfinance)
  + sentiment from prior quarter's press releases. Low data volume initially; grow over 6 months.

### 3. Wheel Infrastructure — Nearly Free Extensions

- **BOXX ETF position for idle cash**: 1h effort, $1,000/year yield on $20k idle capital. Already
  have Alpaca order infrastructure.
- **Iron condor runner**: ~20h effort. The multi-leg order structure is the same as the wheel's
  CSP + CC legs. The regime gate and profit-take cron patterns are identical.
- **Earnings volatility crush runner**: ~15h effort. Reuses `filters.py` earnings calendar, adds
  short-straddle order type, adds open-fill cron.

### 4. Alpaca Paper for Risk-Free Dry-Runs

Every new strategy should run 4–8 weeks in paper mode before any real capital allocation. The paper
API is free, supports all options strategies on US stocks, and fills at realistic mid-prices.
This is a meaningful cost advantage — strategy iteration is $0.

---

## Honest Blockers

1. **$100k stocks capital limits simultaneous positions.** The wheel needs $5k–$15k per position
   collateral. Iron condors need $1k–$2k. Sector rotation needs ~$15k–$20k per ETF position.
   Total demand across all Tier-A/B strategies: $70k–$130k. Cannot run all simultaneously.
   Prioritize: wheel + iron condors share the options pipeline and are the highest evidence-quality plays.

2. **1-min bar resolution limits intraday strategies.** 0DTE, intraday mean-reversion, and scalping
   are all structurally off the table without tick-level data. This is NOT a blocker for the
   strategies ranked Tier-A/B above — all use daily bars or option-cycle cadence.

3. **No futures account means no /ES, /NQ, /VX.** Cannot do volatility futures hedging (long VX
   against short options). Cannot do index futures arbitrage. Iron condors are the practical
   substitute for hedged vol-selling at this account level.

4. **Alpaca does not support SPX/XSP index options** (cash-settled, Section 1256 treatment).
   GitHub issue #265 open since November 2024 with no assignee. Use SPY equity options as the
   substitute. SPY has excellent liquidity at $100k scale; the only loss is the tax efficiency
   of cash-settled index options (60/40 cap gain treatment).

5. **Single operator = finite cron management capacity.** Each additional strategy is a new
   failure mode to monitor. At current footprint (wheel + Shark + 4 Docker containers + 31
   Hermes crons), adding iron condors + insider signal + BOXX is 3 new modules. Budget for
   this integration time before committing.

6. **Hermes 3:8b reasoning quality is uncertain for stock-picking tasks.** The MarketSenseAI
   result uses GPT-4o; smaller models produce significantly worse financial reasoning. The Shark
   framework is sound but the underlying LLM may not generate alpha at 8b parameter scale.
   Hermes 70b (available on the DGX Spark) is the minimum for complex analytical tasks.

---

## Recommended Week-2-4 Roadmap

### Experiment 1: Wheel Expansion (W2)

**Goal:** Expand from 2–3 tickers to 5 tickers systematically. Add AMD, COIN to NVDA + PLTR + SOFI.
**Decision criteria:** Each new ticker must pass: IVR > 35, no earnings within 14 days, min OI > 500,
delta 0.25–0.35 band, weekly yield > 0.8%. The filters are already wired.
**Success metric:** After 30+ closed trades across all tickers, compute Sharpe vs weekly cash returns.
Sharpe > 0.5 is the bar for keeping the strategy. Below 0.3 = reassess ticker selection.
**Effort:** 4h (update `WHEEL_SYMBOLS` env var + verify filters fire correctly on new tickers).

### Experiment 2: SPY Iron Condor (W2–W3)

**Goal:** Run a single SPY iron condor per month. 16-delta short strikes, 2-point wings, 45 DTE entry.
Manage at 50% profit OR close at 21 DTE (whichever comes first).
**Decision criteria:** After 6 trades (6 months), compute win rate and average credit captured vs max risk.
Win rate > 60% AND average return > 2% of collateral per trade = proceed to scaling.
**Effort:** ~20h to build the iron condor selector + multi-leg Alpaca order + management cron.
**Capital budget:** $5k–$10k in buying power reduction (paper mode initially).

### Experiment 3: BOXX ETF for Idle Cash (W2 — near-zero effort)

**Goal:** Allocate $15k–$20k of idle stocks cash to BOXX ETF via the Shark portfolio manager.
**Decision criteria:** Track 1-month yield vs money market. Should be approximately 4.5–5% APY.
**Effort:** 1h (add a BOXX allocation rule to Shark's cash management logic).
**Expected value:** ~$800–$1,000/year incremental on $20k. Not dramatic but $0 risk.

### Experiment 4: Form 4 Insider Cluster Scanner (W3–W4)

**Goal:** Build an SEC EDGAR Form 4 scraper that flags cluster buys on your 14-ticker watchlist.
Run in notification-only mode (Hermes Telegram alert) for 30 days before acting on signals.
**Decision criteria:** Track all flagged clusters for 30 days. If median 30-day forward return > SPY
over the same period, proceed to paper-trading position sizing.
**Effort:** ~25h (see §5 above). High ceiling, differentiated edge.

---

## What I Would Build If This Were My $120k Stocks Side

### The Single Recommendation: Wheel + Iron Condors on Parallel Rails, With BOXX for Idle Cash

**Core allocation:**
- $70k → Wheel (5 CSP positions × $14k collateral each): NVDA, PLTR, SOFI, AMD, COIN
- $15k → SPY Iron Condors (3 contracts × $5k buying power reduction each)
- $15k → BOXX ETF (idle cash earning risk-free rate)

**Fee math (Alpaca $0.65/contract for options):**
- Wheel CSPs: open 5 contracts/week + profit-take checks ~2/week. ~7 option transactions/week
  × $0.65 = $4.55/week = ~$237/year. Trivial relative to $12k–$20k annual premium.
- Iron condors: 4 legs × $0.65 = $2.60 to open + $2.60 to close = $5.20/trade × 12 trades/year
  = $62.40/year in options fees. Negligible.
- BOXX: 1 equity transaction = $0 commission. One-time buy.
- Total fee burden: ~$300/year on a $12k–$20k gross premium strategy. Fee drag < 2.5%.

**Position-sizing rule:**
- Never deploy more than 15% of total portfolio per single-stock CSP ($18k max per ticker on $120k).
  This leaves room for the iron condors and BOXX without over-concentrating.
- Iron condors: max 2% of portfolio risk per trade. On $120k portfolio, that is $2,400 max loss.
  With a 4-point wing width at $5 width = $500 max loss per contract → 4 contracts max.
- BOXX: treat as a substitute for money market. Allocate any cash not needed for CSP collateral.

**Why this beats alternatives:**
- Both the wheel and iron condors are vol-selling strategies, but they are uncorrelated in their
  failure modes: wheel fails on single-stock gap risk; iron condors fail on correlated market crashes.
  Running both provides structural diversification within the VRP universe.
- BOXX earns ~4.5–5% on idle cash that would otherwise earn 0% sitting in Alpaca cash.
- The entire stack is daily-cron compatible (no minute-level monitoring required).
- Fee total: ~$300/year. This is the single most capital-efficient, evidence-backed configuration
  for this operator's constraints.

---

## Comparison to the Crypto-Side Research

### Where Stocks and Crypto Complement Each Other

| Dimension | Crypto Side | Stocks Side |
|---|---|---|
| Edge source | Perpetual funding rate (structural vol premium) | Options VRP (implied > realized vol) |
| Correlation | BTC/ETH movements | US equity beta (S&P 500) |
| Bear regime behavior | Funding rate drops / may flip negative | Wheel goes underwater; IV spikes (more premium) |
| Regime gate | HMM trending_up / down | HMM BULL_QUIET / BEAR_VOLATILE |
| LLM advantage | Sentiment on crypto Twitter/Reddit | Form 4 parsing, earnings press releases |

The two sides are COMPLEMENTARY in a critical way: in bear markets, crypto funding rates compress
(reducing the crypto-side edge) while options implied volatility INCREASES (expanding the stocks-side
premium). This is a natural cross-hedge. The $120k portfolio split (~$100k stocks, ~$19k crypto)
is already capturing this diversification.

### Where They Overlap — Correlation Risk

- Both crypto and stocks strategies are net-short volatility (selling premium). In a 2008/2020-style
  correlated crash, BOTH sides will lose simultaneously: crypto funding flips negative, wheel CSPs
  get assigned, iron condors hit max loss. This is the primary systemic risk in the portfolio.
- Mitigation: the regime HMM is the critical risk gate. If it classifies BEAR_VOLATILE, both
  sides should reduce exposure simultaneously. This is a single point of failure (the HMM
  classification must be correct) but is the correct architecture.
- The BOXX ETF position is the only truly uncorrelated element — it earns the risk-free rate
  regardless of vol regime.

### Cross-Side Opportunity Not Yet Exploited

The insider cluster signal (§5) has no crypto equivalent (crypto insiders are not regulated by
SEC Form 4). This is a stocks-ONLY edge where the LLM advantage is asymmetric. Building this
signal creates genuine portfolio-level diversification beyond just asset class split.

---

## Confidence

| Section | Confidence | Basis |
|---|---|---|
| Wheel VRP evidence | High | spintwig 2,200+ trades; CBOE BXM index 37+ years; CAIA academic review |
| Iron condor evidence | Medium | Tastytrade study methodology not fully disclosed; ORATS Sharpe from secondary source |
| Sector rotation evidence | Medium | Quantpedia 1928–2009 (old data); JPM 1999–2024 (better but more recent) |
| Insider Form 4 signal | Medium | Academic papers are strong; out-of-sample microcap study (2024) is promising |
| Earnings vol crush | Medium | iPresage methodology not publicly validated; CBOE 72% stat is solid |
| PEAD (LLM augmented) | Low | MarketSenseAI 2-year window is insufficient; hermes 8b quality uncertain |
| PMCC/Diagonal evidence | Low | No rigorous backtest found; practitioner-only claims |
| Box spread / BOXX | High | CBOE institutional data confirmed; BOXX ETF AUM $8B validates retail adoption |
| Alpaca SPX support | High | GitHub issue confirmed, open, no assignee as of Nov 2024 |

### What Would Raise Confidence

- 30+ closed wheel trades across 5 tickers with Sharpe computable (currently at 2 trades).
- 6-month iron condor paper-mode results from this specific Alpaca API implementation.
- 30+ Shark agent stock picks tracked vs SPY benchmark (currently no tracked baseline).
- Form 4 insider cluster signal: 60+ cluster events tracked prospectively with forward-return attribution.

---

*Evidence index (internal files):*
- `stocks/wheel/runner.py:1–21` — wheel orchestration entry points
- `stocks/wheel/filters.py:118–192` — earnings_blackout() implementation
- `stocks/wheel/filters.py:195–344` — iv_rank_filter() implementation
- `stocks/wheel/config.py:43–65` — WheelConfig parameters, IVR threshold 35
- `stocks/wheel/strategy.py:45–73` — filter_puts() delta-band + yield-floor gate
- `stocks/CLAUDE.md:29–64` — Shark hard rules, position limits, regime gate
- `stocks/wheel/state/positions.json` — 2 open positions (NVDA CSP, PLTR CSP)
- `stocks/wheel/state/account_snapshot.json` — $100,528.31 portfolio value, +$524.05 cumulative wheel P&L
- `audit/2026-05-14-night/07-architecture-review.md:17` — 3/10 readiness rating, +$524 noted as insufficient sample
