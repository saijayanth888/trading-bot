# 4-Week Execution Plan — trading-bot × model-forge

> **Dual goal**: hit +$2,000 paper P&L by end of week 4 AND ship a public open-source release that goes viral on HN/X/Reddit.
> **Locked decisions**: qwen3:30b base for 6+ months · adapters to private HF Hub only · predictive hit-rate is the truth signal · strict $0 paid-API budget (fully local always).
> **Written**: 2026-05-12 AM ET. **Owner**: Sai Jayanth.

---

## The architecture commitment

```
┌────────────────────────────────────────────────────────────────┐
│  trading-bot repo (MIT) — runs the actual trading              │
│                                                                │
│  • Freqtrade (FreqAI MR + NFI X6 + BollingerRSI)               │
│  • Shark multi-agent debate (qwen3:30b grunt, vLLM serving)    │
│  • Wheel CSP/CC                                                │
│  • Hermes cron orchestration                                   │
│  • React dashboard                                             │
│  • Postgres trade_journal + decisions.md                       │
│                                                                │
│  EXPORTS to model-forge: training data (JSONL → HF Arrow)      │
│  CONSUMES from model-forge: adapters via HTTP /api/forge/query │
└────────────────────────────────────────────────────────────────┘
                    │                              ▲
                    │ HTTP only (no shared DB)     │ HTTP only
                    │ ~/.dgx-train/datasets/       │ /api/forge/query
                    │                              │ with track_id
                    ▼                              │
┌────────────────────────────────────────────────────────────────┐
│  model-forge repo (MIT) — runs all training & evaluation       │
│                                                                │
│  • LangGraph evolution orchestrator                            │
│  • Unsloth LoRA training on DGX Spark FP4                      │
│  • lm-eval-harness + custom trading evals                      │
│  • Pareto-multi-objective adapter promotion                    │
│  • Lineage DB (Postgres + pgvector)                            │
│  • vLLM multi-LoRA serving on :8090                            │
│  • React adapter-evolution dashboard on :3001                  │
│                                                                │
│  ALL training, eval, lineage, adapter storage lives HERE.      │
│  trading-bot never imports model-forge code, never touches its │
│  DB. Pure HTTP boundary.                                       │
└────────────────────────────────────────────────────────────────┘
```

**Why this matters**: keeps the trading hot-path lean, makes both repos independently demoable (trading-bot for traders, model-forge for ML researchers), and the two-repo story is itself the viral hook — two MIT projects, same operator, same DGX Spark.

---

## The 6 "tracks" registered in model-forge

Each is one row in ModelForge's `evolution_tracks` table. Each gets its own LoRA stack on the qwen3:30b base.

| Track ID | Trading role | Base | Eval shape | Latency budget |
|---|---|---|---|---|
| `trading-reflector` | post-mortem writer (2-4 sentences, cites alpha) | qwen3:30b | faithfulness regex + 30d hit-rate + HITL + debate-impact A/B | 1×/day, nightly |
| `trading-bull` | bullish debater (prose w/ evidence cites) | qwen3:30b | evidence-density + judge-preference | 5-15× per pre-market |
| `trading-bear` | bearish debater | qwen3:30b | mirrors trading-bull | 5-15× per pre-market |
| `trading-arbiter` | Portfolio Manager (structured `TraderProposal`) | qwen3:30b | decision consistency + downstream P&L | 1× per debate |
| `trading-regime-tagger` | regime JSON classifier | qwen3:30b (or hermes3:8b if we add a 2nd base) | structured-output validity + agreement w/ HMM | intraday |
| `trading-indicator-selector` | ≤8 indicators per regime, JSON | qwen3:30b | structured validity + downstream-strategy alpha | per pair-day |

---

## Week 1 — Foundation (May 12 → May 18)

**Theme**: pipes built, no real training yet. Data starts flowing.

| Day | Trading-bot side | Model-forge side | Viral artifact |
|---|---|---|---|
| Mon May 12 | ✅ overnight merge of 13 staging branches · ✅ LLM logger with prompt+response capture · ⏳ ModelForge Day 1-2 (ingest+curate scripts) — staging agent in flight · ⏳ Slack bloat fixes applied | ⏳ OpenOrca hardcode fix in `training_backend.py` — staging agent in flight · register 6 evolution_tracks rows via API | architecture diagram drafted (this doc) |
| Tue May 13 | enable `SHARK_LLM_LOG_FULL_TEXT=1` in `.env` · merge Day 1-2 branches · first dry-run of ingest cron (no data yet, just plumbing) | install Unsloth on DGX Spark via haven-jeon docker image · confirm 200-step PiSSA-LoRA refresh of qwen3:30b runs in <30 min on a tiny mock dataset | tweet teaser: "I'm building a fully local trading agent that learns from its own paper trades. Day 1 thread →" |
| Wed May 14 | stand up trading-bot's first **WeeklyTrainingLive** dashboard card (placeholder, hits model-forge `/api/forge/tracks` for status) | stand up **vLLM 0.5+ on :8090** with qwen3:30b base + adapter hot-swap endpoint `/v1/load_lora_adapter` · confirm Ollama on :11434 stays warm in parallel | screenshot the dashboard card (empty state) for the launch thread |
| Thu May 15 | wire Shark debate to call **vLLM for prose roles** (Bull/Bear/Arbiter), keep Ollama for JSON roles (RegimeTagger/IndicatorSelector) | trading-reflector track gets its first **cold-start adapter** trained on synthetic mocked reflections (just to validate the pipeline end-to-end) | nothing public — internal milestone |
| Fri May 16 | first real reflections start hitting `stocks/memory/decisions.md` from the nightly Reflector cron (paper trades from M-W close) | nothing — waiting on data accumulation | first real reflection sample (anonymized) tweeted with commentary |
| Sat May 17 | NFI X6 remediation: implement cron-resample 1h→4h to unblock the 4h-data gap (decision deferred from week 0) | nothing — Saturday | nothing |
| Sun May 18 | **Sunday 02:00 ET**: first real LoRA refresh fires — `trading-reflector` adapter retrained on the ~5-10 reflections from M-W-F closes. Eval pass/fail per Pareto rules. | promote-or-rollback gates fire automatically. Adapter symlink updates if promoted. | **week 1 commit graph screenshot** — daily green squares — for the build-in-public credibility shot |

**Success gate at end of week 1**: trading-bot writes reflections, model-forge ingests them, one full ingest→curate→train→eval→promote cycle completed (even on tiny data). Plumbing works.

---

## Week 2 — Real data, first improvements (May 19 → May 25)

**Theme**: enough data to start measuring. First adapter v1 → v2 improvement visible.

| Day | What ships | Why it matters |
|---|---|---|
| Mon May 19 | trading-bot: cron-resample 1h→4h fully validated, NFI X6 paper-soak begins on `freqtrade-nfi` profile (8 pairs, isolated $50k wallet) | NFI X6 finally trades. Second P&L source online. |
| Tue-Wed | model-forge: extend the 6 tracks to all consume real data. Bull/Bear/Arbiter start training on actual debate transcripts (now that logger captures full text) | All 6 roles learning, not just Reflector |
| Thu May 22 | trading-bot: **WeeklyTrainingLive dashboard card** goes live with real adapter versions, eval scores, last-promotion-timestamp per track | This is the screenshot for the launch thread |
| Fri May 23 | model-forge: **side-by-side reflection diff** UI — pick any 2 adapter versions, see how they write the same closed-trade post-mortem | The visual proof of learning — most viral artifact in the deck |
| Sat May 24 | trading-bot: secrets audit script (`scripts/audit_for_public_release.sh`) — gitleaks + trufflehog + custom path-scanner. Run pre-publish. | Required before any public push |
| Sun May 25 | **Sunday 02:00 ET**: weekly LoRA refresh #2. By now should see measurable eval improvement vs cold-start. | First "AI got smarter this week" data point |

**Week 2 viral moment**: tweet at end of week 2 with the side-by-side reflection diff: "Same trade, week 1 vs week 2. My bot's getting sharper. Read for yourself →"

---

## Week 3 — Compounding + launch prep (May 26 → June 1)

**Theme**: refine, polish, prep the public release.

| Day | What ships | Why it matters |
|---|---|---|
| Mon May 26 | model-forge: SuRe surprise-replay buffer landed — adapter refresh now mixes in 30% top-K-surprise prior examples to prevent catastrophic forgetting | The Rung-2 intelligence move from yesterday's deep research |
| Tue May 27 | trading-bot: **README rewrite** with architecture diagram, install one-liner, comparison table vs ruflo/TradingAgents/dexter, screenshot pack, roadmap | The single most important viral artifact |
| Wed May 28 | trading-bot: **demo dataset + degradation path** — sanitized trade journal + `MODEL_TIER=laptop` knob (auto-picks llama3.2:3b on consumer hardware) | Lets strangers `make demo` and see something working in 60 seconds |
| Thu May 29 | model-forge: README rewrite + same architecture / install treatment | Sibling repo needs equal polish |
| Fri May 30 | both repos: **launch artifacts** — 30-second screen recording, polished architecture PNG, Show HN draft, X thread draft (8 tweets), Reddit drafts | Pre-staged so launch day is execution, not creation |
| Sat May 31 | dress rehearsal: clone trading-bot to a fresh DGX-class machine (or your laptop with `MODEL_TIER=laptop`), follow README from scratch, fix anything that breaks | The "stranger experience" smoke test |
| Sun June 1 | **Sunday 02:00 ET**: weekly LoRA refresh #3. Adapter v3 should now have ~30-50 real reflections trained on. Eval scores meaningfully above cold-start. | Last data point before launch |

**Week 3 viral moment**: thread teasing the launch with the dashboard GIF + the architecture diagram.

---

## Week 4 — Launch (June 2 → June 8)

**Theme**: ship it.

| Day | What ships |
|---|---|
| Mon June 2 | secrets audit final run · push both repos to public (or flip private→public) · GitHub stars start trickling in from your immediate network |
| Tue June 3 | **Show HN: trading-bot** post lands with the 30s demo GIF as the hero · live link to a sanitized dashboard demo (read-only) · paper P&L number publicly shown |
| Wed June 4 | X thread (8 tweets) lands · cross-posts to r/algotrading + r/MachineLearning · live-Q&A in HN comments |
| Thu-Fri | tend the fire — respond to issues, accept PRs, demo to anyone who asks |
| Sat June 7 | Sunday recap thread: "Week 1 of public. Here's what happened. Here's where the bot is now. Here's the next adapter refresh."  |
| Sun June 8 | **+$2,000 paper P&L tally**. If hit: celebratory tweet with the screenshot. If missed: honest retrospective tweet (still viral — *real* numbers in either direction). |

**Week 4 viral moment**: the launch itself. If we hit $2k, the tweet writes itself. If we miss, the honest "here's why" is its own engagement driver.

---

## Per-role training cadence (the actual cron schedule)

All times ET. Driven by Hermes cron in trading-bot for ingest; model-forge runs its own internal scheduler for training.

| Time | Trading-bot side | Model-forge side |
|---|---|---|
| 21:00 daily (M-F) | `modelforge_ingest.py` — pull yesterday's closed trades + LLM calls, write raw JSONL per role | — |
| 21:30 daily (M-F) | `modelforge_curate.py` — filter + transform to HF Arrow, drop into `~/.dgx-train/datasets/<role>/curated/` | — |
| 21:45 daily | — | model-forge's data_curator picks up the new `curated/` files, registers in lineage DB |
| 23:00 Saturday | — | `eval.py` against time-frozen test set, score current adapter per metric |
| 02:00 Sunday | — | per-track LoRA refresh (PiSSA rank=16 alpha=32 + SuRe replay buffer 30%) |
| 04:00 Sunday | — | eval new adapter |
| 04:30 Sunday | — | promote-or-rollback per Pareto + your **predictive-hit-rate wins** rule. Push adapter to private HF repo. |
| 04:45 Sunday | trading-bot's vLLM client pulls the new adapter via `/api/forge/query` next call | — |

**GPU sharing during the 02:00-04:00 ET training window**: vLLM scales KV cache down to 8 GB (`--gpu-memory-utilization 0.15`), training takes the GPU, then vLLM restores. Market is closed, no requests in flight. No retrain disruption.

---

## The viral story arc (in one paragraph)

We built a fully local, self-improving trading agent. It runs on a single NVIDIA DGX Spark. It uses zero paid APIs. It writes a 2-4 sentence post-mortem after every closed paper trade. Every Sunday at 2am, those post-mortems train a LoRA adapter that makes the agent's next week of decisions sharper. We tracked predictive hit-rate, not benchmark numbers. We published the reflection log, the adapter versions, the eval scores, the paper P&L — all of it, live, daily, on a public dashboard. After 4 weeks, the bot was [+$X / −$X] in paper. Here's the repo. Here's the dashboard. Watch the AI learn.

That's the launch. Two MIT repos. Two diagrams. One GIF.

---

## What's NOT on this plan (deliberately)

- ❌ **No paid LLM APIs ever** (locked decision #4)
- ❌ **No going-live with real money** in this 4-week window (paper only)
- ❌ **No NFI X6 activation on Coinbase as-is** (the 4h-data gap blocks it — fix is cron-resample in week 2)
- ❌ **No multi-base model** (locked to qwen3:30b for 6+ months per decision #1)
- ❌ **No trading data to HF Hub ever** (only adapters; decision #2)
- ❌ **No rebuild of the trading-bot dashboard from scratch** in this window (#19 is your separate Claude Code track; the WeeklyTrainingLive card is a one-page addition, not a rewrite)

---

## How we know we're on track

| Week | Metric | Target |
|---|---|---|
| 1 | One full ingest→curate→train→eval cycle completed | end of Sunday |
| 2 | Reflection count in `decisions.md` | ≥ 15 |
| 2 | Adapter versions per track | ≥ 2 |
| 3 | Predictive hit-rate (where measurable) | adapter v3 > cold-start |
| 4 | Public commits visible on GitHub | every weekday since week 1 |
| 4 | Paper P&L cumulative | +$2,000 (stretch); +$500 (floor) |
| 4 | Launch artifacts ready | demo GIF + 1-pager + 3 platform drafts |
| 4 | HN / X / Reddit posts live | end of week 4 |

If we miss the $2k stretch but hit the +$500 floor with all the rest, the launch still goes — the *architecture novelty* + *fully local* + *real numbers* story carries it. We don't need to win to go viral; we need to be real.

---

_Generated 2026-05-12 AM ET to align with locked operator decisions and yesterday's continual-training deep research. Living document; revise weekly._
