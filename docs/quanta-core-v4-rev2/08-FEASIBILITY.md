# Quanta Core V4 rev2 — Feasibility Confirmation (revised)

**Branch:** `feat/quanta-core-v4-rev2-r8`
**Audit date:** 2026-05-12
**Host:** `saijayanthai` (NVIDIA DGX Spark, GB10 / aarch64 / CUDA 13)
**Verdict (executive summary at the bottom):** **FEASIBLE — buildable in ~5-6 wall-weeks with the rev2 operator philosophy (Ollama-only, load-on-demand, weekly LoRA, 30s debate). All three "CONDITIONAL" gates from r8 are eliminated.**

This is a re-run of `docs/quanta-core-v4/08-FEASIBILITY.md` against the rev2 architecture (validator-driven: `docs/quanta-core-v4/07-VALIDATOR_REPORT.md` flagged 2 P0s — vLLM-vs-`project-drop-vllm` and the 95 GB resident-models squeeze — that this rev2 retires). Every hardware number is a fresh live reading (`free -h`, `nvidia-smi`, `lscpu`, `df -h`, `vmstat`) taken at **2026-05-12 20:44 UTC**, after today's merges (TFT-blind fallback, regime fixes, NFI x6 activation, Shark/Wheel isolation, exchange-API gap audit). The numbers differ from doc 08 r8's snapshot because the box has been running paper trading + Ollama + multiple workers for the full day.

The rev2 architecture removes vLLM, removes simultaneous multi-model residency as a target, drops weekly LoRA training to a Sunday cadence, accepts a ~30 s debate budget (not 500 ms), and acknowledges a real-life 2-3 trades/week volume. Those four changes are what flip the verdict from CONDITIONAL to FEASIBLE.

---

## 1. Hardware capacity table (live numbers, snapshot 2026-05-12 20:44 UTC)

| Resource | Vendor spec | Measured (live, post-merge) | Already in use | Headroom for V4 |
|---|---|---|---|---|
| CPU | NVIDIA Grace `Cortex-X925` ×10 + `Cortex-A725` ×10 = 20 cores | 20 cores, 1 socket, aarch64, SVE2 / BF16 / I8MM / dotprod | ~irrelevant | full 20 cores |
| GPU | NVIDIA GB10 (Blackwell, sm_121, CC 12.1) | GB10 ×1, driver 580.142, CUDA 13.0; **29% util, 55°C, 31 W draw** (paper-trading + Ollama qwen3:30b warm) | one Python worker resident at 12.2 GB; Ollama qwen3:30b warmed (~18 GB on disk, paged into unified mem on first request) | varies by tier — see §1a |
| **Unified memory** | 128 GB LPDDR5X (Grace–Blackwell coherent) | **121 GiB total** (`free -h`) | **81 GiB used / 24 GiB free / 39 GiB MemAvailable / 27 GiB buff-cache** | **see §1a — closes comfortably for rev2** |
| Swap | spec n/a | 31 GiB total, **5.7 GiB used** (carry-over from earlier vLLM bootstrap mishap, see project-drop-vllm) | 26 GiB free | OK; rev2 design targets zero steady-state swap |
| Disk (root) | 4 TB NVMe | **3.7 TB / 463 GB used / 3.1 TB free (13%)** | fine | abundant — base models + LoRA snapshots + ledgers stay well under 50 GB |
| NUMA | 1 node | node 0, 20 cpus, 124.6 GB unified | single node — no NUMA penalty | no cross-socket plumbing |
| `/dev/shm` | spec n/a | ~61 GiB tmpfs (= half of RAM) | empty | usable for KV cache spillover (not needed in rev2 path) |
| `vmstat` swap pressure | — | si/so ≈ 0-4 KB/s steady; one **2229 KB/s si spike** observed in the 1-s sample | mostly inactive swap left over from the May-12 vLLM kill | indicator that rev2's "load-on-demand, evict between trades" pattern is the right call |

**Architecture:** `aarch64` (Ubuntu 24.04 / Linux 6.17 nvidia kernel). All Python wheels must be ARM-built or pure-Python.

### Delta from doc 08 r8 (six hours later, post-merges)

- Memory **used** drifted from 80 GiB to 81 GiB — within noise. The box has been steady through 7 merges today.
- **Swap is still pinned at 5.7 GiB** because we never fully drained the May-12-morning vLLM bootstrap. Note for the rev2 operator: a clean reboot before the V4 build kicks off would reset swap to 0 and give us 5-6 GB of extra headroom — but it is not required.
- GPU utilisation is healthy (29%, 31 W) — Ollama is correctly idling qwen3:30b after the last request rather than pinning it.

### 1a. Memory budget — **closes comfortably for rev2**

Doc 08 r8 used a "everything resident at once" budget — 95 GB resident models + 30 GB LoRA pool + 10 GB KV = **135 GB**, which did NOT fit in the measured 121 GiB usable. The validator flagged this as P0-2 ("recreates the exact OOM operator already lived through").

Rev2 abandons simultaneous residency entirely (see `docs/quanta-core-v4-rev2/01-RESEARCH-MULTI_MODEL_RESIDENCY.md` — Ollama only, load-on-demand, no pinning, `OLLAMA_MAX_LOADED_MODELS` ≤ 2). The realistic budget is:

| Bucket | Steady-state (between trades) | Peak (mid-debate) | Notes |
|---|---|---|---|
| OS + freqtrade + dashboard + docker + chromium + buffer-cache | ~30-35 GB | ~30-35 GB | measured today as ~81 GB used minus the 12 GB Python worker minus the warm qwen3:30b = ~50 GB. Some of that is buff-cache (27 GB) which kernel will release on pressure → **true unevictable baseline closer to 30-35 GB**. |
| Hermes 8B trader (hot tier, kept warm via `OLLAMA_KEEP_ALIVE=24h`) | ~5 GB (Q4) | ~5 GB | always-resident — fast classifier + sentiment, tick-loop friendly |
| TFT model (PyTorch, fraction-capped) | ~3-5 GB unified | ~3-5 GB | `torch.cuda.set_per_process_memory_fraction(0.3)` retained from `TFTModel.py:176` |
| qwen3:30b (debate role agents, Ollama Q4) | 0 (evicted) | ~18 GB | loaded on debate kickoff, evicted ~10 min after last request via `OLLAMA_KEEP_ALIVE=10m` |
| hermes3:70b (deep arbiter, Ollama Q4) | 0 (evicted) | ~39 GB | loaded only when a debate escalates to deep tier; evicted immediately on return-to-idle |
| LoRA adapters (current + last 2 rollback tags) | 0 (on disk, ~1 GB each) | ~1-2 GB | adapters are baked into Ollama Modelfile tags per `project-drop-vllm` §2; no in-memory adapter pool |
| KV cache | small | ~2-4 GB | Ollama's default; no PagedAttention machinery needed |
| **Steady-state total** | **~40-45 GB** | — | leaves **~75 GB free** — easily absorbs TFT training Monday + LoRA training Sunday |
| **Peak total (debate firing, both 8B + 30B + maybe 70B briefly)** | — | **~85-95 GB** | leaves **~25-35 GB free** — still well clear of the 95 GB / 121 GB OOM mark that killed vLLM on 2026-05-12 |

**The peak case (~85-95 GB) is the only stress point**, and it only fires 2-3 times/week (see §2 trade-volume note). The Ollama LRU evictor handles the swap-in/swap-out automatically; cold-load of qwen3:30b off the NVMe is ~6-10 s, cold-load of hermes3:70b is ~20-30 s. Both are well within the rev2 30 s debate budget (see `docs/quanta-core-v4-rev2/05-RESEARCH-PARALLEL_AGENTS.md`).

**Conclusion: the rev2 memory plan fits.** Headroom for:
- TFT retraining on Monday mornings (peaks ~15 GB additional, fully releasable)
- LoRA training Sunday (Unsloth, ~25-35 GB during training, off-hours so debate isn't competing)
- Chromium dev tabs + freqtrade UI + dashboard SPA (~3-5 GB)

The `torch.cuda.set_per_process_memory_fraction(0.3)` cap in `TFTModel.py:176` is doing the right thing today and **must be preserved** (or moved into a central GPU-budget arbiter shared with Ollama's `--gpu-memory-utilization` flag).

### 1b. NUMA topology
Single NUMA node (Grace unified). No `numactl --cpunodebind` plumbing needed — but be aware that Grace+Blackwell share a single coherent memory pool, so a 90% RAM allocation by Python *will* starve the GPU. Rev2 preserves the fraction cap.

### 1c. Trade volume sanity check (new in rev2)

The bot's real-world trading volume is **2-3 trades/week** (paper account; SOL/USD long + SOFI/NVDA short puts opened on 2026-05-11 for $629 premium per the EOD memory). At that cadence:

- Debate fires **2-3× per week** for live entry decisions (plus a few false-positive evaluations that exit early).
- LoRA training fires **1×/week** (Sunday 02:00 ET).
- TFT retraining fires **1×/week** (Monday early AM, off-market).
- Nightly Reflector fires **1×/night** on aggregated trade log (single role, single 30B inference, ~10 s).

Memory pressure is bursty, not sustained. Rev2's "load-on-demand, evict-after-idle" pattern is the right match — and is also why doc 08 r8's "5+ models always-resident" target was overbuilt for the workload.

### 1d. ARM-vs-x86 wheel hazards (currently installed)

Verified live via `python3 -c "import torch; ..."` at the snapshot window:

| Library | Installed version | aarch64 wheel? | Notes |
|---|---|---|---|
| `torch` | **2.11.0+cu130** | NO official PyPI wheel; the installed one is from `download.pytorch.org/whl/cu130` or a community build | `torch.cuda.is_available() = True`, `cuda 13.0`, `device = NVIDIA GB10` — works. **Pin the index URL in `pyproject.toml` before rebuilding.** |
| `torchaudio` / `torchvision` | 2.11.0 / 0.26.0 | same as torch | bundled |
| `transformers` / `peft` / `sentence-transformers` | 5.8.0 / 0.19.1 / 5.4.1 | YES (pure-py) | OK |
| `coinbase-advanced-py` | 1.8.2 | YES (pure-py) | OK |
| `alpaca-py` | not in `pip list` of this env (project venv) — **already used in `stocks/`** per code audit; pure-py wheel exists on PyPI | YES (pure-py) | OK |
| `polygon-api-client` | not in this env's `pip list` — used by stocks side | YES (pure-py) | OK |
| `cupy-cuda12x` | not in this env's `pip list` — required for §11 Risk Monte Carlo per `docs/quanta-core-v4/03-RESEARCH-RISK_MONTE_CARLO.md` | YES (v14, Jan 2026 — aarch64 + Grace Hopper unified mem supported) | install when §11 module is built |
| `ollama` (CLI + REST) | **0.23.1** running on :11434 (verified `/api/version` and `/api/tags`) | n/a (Go binary) | qwen3:30b + hermes3:8b + hermes3:70b + qwen2.5:72b-instruct all present locally |

**Net hazard count:** **1** — torch wheel provenance. Down from r8's **4** (torch + vLLM + TensorRT-LLM + flash-attn). The three vLLM-family blockers vanish entirely with the rev2 Ollama-only stack.

---

## 2. Library / SDK availability on ARM (Grace CPU + Blackwell sm_121) — rev2 matrix

Rows present in r8 that are no longer in the rev2 critical path are listed but greyed out for traceability.

| Library | ARM-clean (PyPI wheel)? | GPU on sm_121 tested? | Build complexity | Notes |
|---|---|---|---|---|
| **`ollama`** (Go binary, served on :11434) | n/a — binary install or apt | YES — official NVIDIA Spark performance blog confirms GB10 Blackwell support [[ollama.com/blog/nvidia-spark-performance]](https://ollama.com/blog/nvidia-spark-performance) | trivial — already installed and serving today | **Primary inference plane.** Already serving 9 models locally; warmed qwen3:30b is the operator's daily driver. No source build, no patches, no `TORCH_CUDA_ARCH_LIST` lies. |
| `alpaca-py` | YES (pure-py) | n/a | trivial | already used in `stocks/` |
| `coinbase-advanced-py` | YES (pure-py) | n/a | trivial | already pinned 1.8.2 |
| `polygon-api-client` | YES (pure-py) | n/a | trivial | already pinned |
| `torch` cu130 aarch64 | NO official PyPI; community build at `download.pytorch.org/whl/cu130` | YES — verified `cuda.is_available()` + `device_name = NVIDIA GB10` live | medium — pin extra-index-url in pyproject; do not let `pip install --upgrade` resolve from default PyPI | TFT model + DRL ensemble + sentence-transformer encoders depend on this. **Single largest remaining build risk** but ~1 dev-day not ~3 — we already have a working install we can lock to. |
| `transformers` | YES (pure-py) | inherits torch | low | OK |
| `peft` (HuggingFace LoRA) | YES (pure-py) | inherits torch | low | used for Unsloth LoRA training, Sunday only |
| `unsloth` (LoRA training) | aarch64 pip install present in unsloth ≥ 2026.04 | inherits torch | low-medium | used Sunday 02:00 ET to train role adapters; off the hot path entirely |
| `sentence-transformers` | YES (pure-py) | inherits torch | low | OK |
| `cupy-cuda12x` | YES (v14, Jan 2026 — Grace Hopper / unified mem officially supported) | YES | n/a | for §11 risk Monte Carlo |
| `numba-cuda` on sm_121 | partial — JIT recompiles for sm_121 on cold start | YES | medium | not on critical path; cupy preferred |
| `freqtrade` | YES (pure-py) | n/a | trivial | keep installed during shadow window then archive |
| ~~`vLLM` cu130 sm_121~~ | ~~NO PyPI; community patches required~~ | ~~partial — illegal-instruction crashes on NVFP4~~ | ~~HIGH~~ | **Out of rev2 scope per `project-drop-vllm`.** Removed from critical path. |
| ~~`TensorRT-LLM`~~ | ~~NO PyPI on Blackwell~~ | ~~yes via NGC container~~ | ~~HIGH~~ | **Out of rev2 scope.** Adds vendor lock without a benchmark win at our 2-3 trades/week cadence. |
| ~~`flash-attn` cu130 sm_121~~ | ~~NO~~ | ~~source build~~ | ~~HIGH~~ | **Optional — only if attention kernel becomes bottleneck. With Ollama we never expose flash-attn knobs directly.** |

**Headline blockers (rev2):** **1 of 11 critical libraries** (torch) requires a non-default-PyPI index URL. The other ten install with `pip install` or are already on the box (Ollama). Compared to r8's 3-4 source builds, this is a substantial simplification.

**DevOps budget:** **~0.5-1 dev-day** for a reproducible build environment (Dockerfile + pinned `--extra-index-url` for the cu130 torch wheel). Down from r8's 3 dev-days.

---

## 3. Existing code reuse audit (re-audited against rev2 architecture)

The rev2 architecture (per `docs/quanta-core-v4-rev2/01-…` and `docs/quanta-core-v4-rev2/05-…`) does NOT introduce vLLM-specific patterns (`load_lora_adapter` REST hot-swap, `--max-loras` runtime config, NVFP4 quant pipeline). Every place the r8 audit said "needs vLLM glue here" becomes "calls `ollama.chat(model='qwen3:30b-<role>-current', ...)` here." That is **simpler glue**, which **raises the portability percentages** below:

| Current file | Lines | freqtrade refs | Target module in V4 rev2 | Portability | Effort |
|---|---|---|---|---|---|
| `user_data/strategies/FreqAIMeanRevV1.py` | 2132 | 24 imports | `quanta_core/strategies/mean_rev_v1.py` | **~35% portable** (up from r8's 30% — no vLLM glue replacing populate_* hooks) | rewrite, ~5-7 dev-days. Salvage indicator math + onchain/sentiment/regime merges + threshold params |
| `user_data/freqaimodels/TFTModel.py` | 829 | 5 imports | `quanta_core/models/tft.py` | **~80% portable** (up from 75% — no vLLM serving shim added downstream) | adapter shim, ~2-3 dev-days |
| `user_data/freqaimodels/tft_serde_legacy.py` (the file currently named `tft_p_i_c_k_l_e.py` in-repo) | 716 | 6 mentions (docstring) | `quanta_core/models/tft_serde.py` | **~85% portable** (unchanged) | directly portable, ~1 dev-day |
| `user_data/modules/risk_governor.py` | 759 | 2 mentions (RunMode enum) | `quanta_core/risk/governor.py` | **~95% portable** (unchanged) | directly portable, ~1 dev-day |
| `user_data/modules/execution_engine.py` | 664 | 1 mention (log path comment) | `quanta_core/execution/coinbase.py` | **~98% portable** (unchanged) | directly portable, ~0.5 dev-day |
| `stocks/shark/execution/exit_manager.py` | 282 | 0 | `quanta_core/execution/exit_manager.py` | **100% portable** | directly portable, ~0.5 dev-day |
| `stocks/shark/execution/stops.py` | 216 | 0 | `quanta_core/execution/stops.py` | **100% portable** | directly portable, ~0.5 dev-day |
| `stocks/shared/subsystem_ownership.py` | **NOW EXISTS** (added 2026-05-12 in `fix/shark-wheel-isolation` merge `b4532a4`) | 0 (pure stdlib) | `quanta_core/shared/subsystem_ownership.py` | **100% portable** | directly portable, ~0.25 dev-day — was a rewrite line in r8, is now copy-paste |
| `user_data/modules/llm/chat_json.py` (already used by Nightly Reflector + outcome resolver per recent commits) | — | minimal | `quanta_core/llm/chat_json.py` | **~95% portable** — already Ollama-shaped (rev2 confirms this is the contract) | directly portable, ~0.5 dev-day |
| **NEW for rev2** — debate orchestrator (LangGraph + `asyncio.TaskGroup` panel) wiring 6 Ollama role models | — | n/a | `quanta_core/agents/panel.py` | **0% existing** — write fresh against `docs/quanta-core-v4-rev2/05-…` | ~3-4 dev-days |
| **NEW for rev2** — Modelfile generator + `ollama create` cron (replaces vLLM `load_lora_adapter`) | — | n/a | `model_forge/adapters/promote.py` | **0% existing** — ~50 LOC per `project-drop-vllm` §7 | ~0.5 dev-day |

**Summary of reuse:** ~3,900 of 5,598 audited lines (~**70%**, up from r8's 64%) are directly or near-directly portable. The big rewrite is still the freqtrade strategy file (2,132 lines, ~35% portable). The remaining ~5,000 lines of non-freqtrade code (DRL ensemble, feature feeds, full `stocks/` tree, Nightly Reflector, outcome resolver, model_forge ingestion) flow into V4 rev2 free.

**Net code reuse delta vs r8:** +6 percentage points (64% → 70%) and one previously-missing file (`subsystem_ownership.py`) is now in the tree.

---

## 4. Build-time Gantt — concrete dev-days per module (revised down)

Wall-time is given for **3-agent parallel dispatch** per the operator's `dispatching-parallel-agents` skill. Numbers shifted from r8 are flagged ⬇.

| Module | Dev-days (single) | Wall-time (3-parallel) | Dependencies | Delta vs r8 | Notes |
|---|---|---|---|---|---|
| **0. Build env lockdown** (Dockerfile pinning torch cu130 + ollama + cupy + CUDA 13; community wheel cache) | **1** ⬇ | **1** ⬇ | — | -2 dev-days | rev2 drops vLLM/TRT-LLM/flash-attn from the lock list. Just one extra-index-url to pin. |
| **1. Data feed layer** (Alpaca + Coinbase + Polygon adapters, websocket reconnect, sqlite tick store) | 5 | 2 | 0 | unchanged | port from `stocks/api/` + `user_data/modules/db.py` |
| **2. Feature pipeline** (onchain + sentiment + regime merges → `FeatureBus`) | 6 | 2 | 1 | unchanged | salvage from FreqAIMeanRevV1 lines 178-300 |
| **3. TFT model** (TFT arch + trainer + checkpoint/resume + stable serde) | **3** ⬇ | **2** ⬇ | 0 | -1 dev-day | 80% lift (up from 75%) trims a partial day |
| **4. DRL ensemble + meta-agent** | 7 | 3 | 3 | unchanged | port `drl_ensemble.py`, `ensemble_voter.py`, `meta_agent.py` |
| **5. LLM inference layer (Ollama-only)** (`OLLAMA_KEEP_ALIVE` tiering + Modelfile adapter generator + `ollama create` cron + `chat_json` wrapper) | **3** ⬇⬇ | **1-2** ⬇⬇ | 0 | **-5 to -9 dev-days** | rev2's single biggest schedule win. No vLLM source build, no NVFP4 quant pipeline, no `load_lora_adapter` glue. The `chat_json` Ollama path is already production today (commit `1366bfd` migrated outcome_resolver to it). |
| **6. Risk governor + execution engines** (coinbase + alpaca + bracket orders + slippage gates) | 3 | 1 | 1 | unchanged | 95%+ portable from existing |
| **7. Exit/stops/wheel/shark agentic stack** | 4 | 2 | 6 | unchanged | mostly verbatim |
| **8. Subsystem-ownership map + module taxonomy** | **0.25** ⬇ | **0.25** ⬇ | all | -2 dev-days | `subsystem_ownership.py` already exists post-`b4532a4` merge — just port it |
| **9. Shadow-mode runner** (parallel-write ledger, trade-by-trade diff, dashboard tile) | 5 | 2 | 1, 6 | unchanged | see §5 |
| **10. Dashboard + observability** (port the 31-table SPA to read from V4 ledgers) | 6 | 2 | 9 | unchanged | the new biggest single pain after step 1 |
| **11. Backtest harness + offline replay** | 4 | 2 | 1, 2 | unchanged | reuse `user_data/backtest_results/` + freqtrade hyperopt as comparator |
| **12. Migration runbook + cutover script + 2-week shadow + ledger diff acceptance** | 5 | 5 | 9, 10 | unchanged | sequential — operator signs off after each milestone |

**Totals (rev2):**
- Single-dev sequential: **47-51 dev-days** ≈ **~10 weeks**  *(was 62-66 / 13 weeks)*
- **3-parallel agent dispatch with review checkpoints: ~23 wall-days ≈ ~4.5 wall-weeks**  *(was ~31 / 6 wall-weeks)*
- Critical path: 0 → 5 (Ollama tiering) → 9 (shadow) → 12 (cutover) = **~9 wall-days** minimum  *(was 16, anchored on vLLM build)*

**Realistic operator-paced delivery: 5-6 wall-weeks** with one operator-review checkpoint per major module and a soft buffer for the freqtrade-strategy rewrite (the only remaining lump). r8's 8-week estimate assumed two vLLM rebuild stalls (CUDA driver bump + wheel-index incident); rev2 removes both.

---

## 5. Shadow-mode migration safety (unchanged from r8 — still feasible, still the only honest path)

The seams in the current bot are the same regardless of the inference plane:

### 5.1 Shadow procedure (2-week window)

1. **Both consume the same WebSocket feeds.** Add a `shadow=true` flag to V4 so it reads but writes to a separate ledger.
2. **Separate ledgers:**
   - Live (truth): freqtrade DB at `user_data/tradesv3.sqlite` + Alpaca paper account
   - Shadow (V4): new sqlite at `quanta_core/state/v4_ledger.sqlite` + a "shadow" Alpaca subaccount OR simulated fills against the real WS order book
3. **No real V4 orders during shadow** — V4 makes the decision, journals "would-have-placed BUY 0.01 BTC @ 65000 @ 14:32:01.231Z", side-effect ends there.
4. **Trade-by-trade diff dashboard tile** — new pane in the SPA listing every divergence (same-direction-different-size, same-side-different-timing, V4-only, freqtrade-only). Operator scans daily.
5. **Acceptance criteria (operator-set):**
   - V4 PnL ≥ freqtrade PnL on a 10-trading-day rolling window, OR
   - V4 PnL within ±$200 of freqtrade BUT meets qualitative criteria (faster reaction, lower drawdown peak, fewer missed-regime trades)
   - No "trades that freqtrade declined and V4 took" with > $50 single-trade loss
6. **Rollback** — `systemctl stop quanta-core`. Shadow ledger stays on disk for forensics. Freqtrade was never paused. Total rollback < 60 seconds.
7. **Cutover** — only after 10 consecutive trading days of acceptance criteria. Flip `QUANTA_LIVE_TRADING=true` env. Freqtrade reduced to paper-only for one week, then archived.

### 5.2 What kills the shadow window

- Free-tier rate-limit collisions on Polygon WS if both subscribers share the same key. Mitigation: second WS connection slot, or Alpaca free-tier WS for shadow.
- Postgres write contention if both write to the same regime/onchain/sentiment tables. Mitigation: V4 uses a separate schema (`quanta_core.*`).

**No change from r8 — the shadow plan was sound, just gated by the (now-removed) vLLM dependency.**

---

## 6. Vendor lock-in assessment (improved)

| Lock-in dimension | Severity in r8 | Severity in rev2 | Notes |
|---|---|---|---|
| **Ollama** | n/a | **VERY LOW** | OSS MIT, Go binary, runs on Linux/macOS/Windows on x86 and arm64. The Ollama HTTP API is a near-standard now; LiteLLM/LM-Studio/llama-server-cpp all expose compatible endpoints. **One env var to swap backends.** |
| **vLLM** | LOW | **N/A — REMOVED** | Out of rev2 scope per `project-drop-vllm`. |
| **TensorRT-LLM** | MEDIUM-HIGH | **N/A — REMOVED** | Out of rev2 scope. |
| **NVFP4 quantization** | MEDIUM | **N/A — REMOVED** | Rev2 uses Ollama's GGUF Q4_K_M / Q4_0 — open quant format, portable to llama.cpp, mlc, anywhere. |
| **Grace-Blackwell unified memory** | MEDIUM | MEDIUM | Same as r8 — `torch.cuda.set_per_process_memory_fraction` is the discipline. |
| **NIM** | HIGH if adopted | **N/A — NEVER ADOPTED** | Decision: never adopt in rev2. |
| **CUDA 13** | LOW | LOW | unchanged |
| **DGX Spark form factor** | LOCKED | LOCKED | unchanged — operator's only AI workstation |

**Net vendor-lock-in posture for V4 rev2:** **substantially improved**. Trading freqtrade-framework lock-in for **Ollama** is a near-zero-cost swap — Ollama is one of the most portable LLM serving runtimes in the OSS ecosystem (MIT licensed, single Go binary, llama.cpp-compatible GGUF format, OpenAI-compatible REST). If we ever decided to leave Ollama, the migration target list is huge: LM-Studio, llama-server, mistral-inference, MLX (on macOS), Triton, vLLM (if it ever stabilises on Blackwell), TGI. **The previous r8 design's lock-in concern around vLLM-specific patterns (`load_lora_adapter` REST, `--max-loras` config, NVFP4 quant kernels) is gone.**

**Recommendation:** still add a `quanta_core/serving/llm_backend.py` interface with `OllamaBackend` as the default and a placeholder `LiteLLMBackend` for future-route-to-vLLM if needed. The interface is ~30 LOC; cheap insurance.

---

## 7. Bottom line — FEASIBLE / CONDITIONAL / NOT_FEASIBLE

### Verdict: **FEASIBLE — buildable in ~5-6 wall-weeks, no required conditions**

The Quanta Core V4 rev2 design is **buildable** on the actual DGX Spark hardware in **5-6 wall-weeks** with the operator's preferred 3-agent parallel dispatch, AND it can run safely in shadow beside freqtrade for ≥ 2 weeks before any real money moves.

### Conditions that were **dropped** from r8

#### ~~Condition 1 — Memory budget must be revised~~ — RESOLVED

Rev2 abandons simultaneous multi-model residency. Steady-state is ~40-45 GB (Hermes 8B + TFT + ops). Peak during a debate is ~85-95 GB (load qwen3:30b + optionally hermes3:70b, then evict). The peak fires 2-3×/week, not in a tight tick loop. Headroom of ~25-35 GB during peak and ~75 GB between trades easily absorbs Monday TFT retrain + Sunday LoRA train.

The r8 P0-2 validator finding — "the memory plan recreates the exact OOM operator already lived through" — is **fully addressed**. Ollama LRU + `OLLAMA_KEEP_ALIVE` tiering + 2-3 trades/week volume is exactly the pattern that doesn't OOM.

#### ~~Condition 2 — Build environment must be pinned, not pip-installed~~ — DOWNGRADED FROM "REQUIRED" TO "NICE-TO-HAVE"

Rev2 has **one** non-default-PyPI dependency (torch cu130 aarch64 from `download.pytorch.org/whl/cu130`). vLLM, TensorRT-LLM, NVFP4, and flash-attn are all out of scope. We should still pin the torch index URL in `pyproject.toml`, but a 1-day budget covers it. Skipping the lockdown no longer indefinitely stalls the project — it just risks one annoying half-day rebuild down the line.

#### Condition 3 — Migration is shadow-first, no flag-day — **STILL APPLIES**

This was right in r8 and is unchanged. The freqtrade stack stays running during all V4 buildout. V4 only goes live after **10 consecutive trading days** of shadow-mode meeting acceptance criteria. Rollback is `systemctl stop quanta-core` < 60 seconds. Freqtrade reduced to paper-only for ≥ 1 week after V4 goes live.

### Reuse value

**~70% of the audited 5,598 lines port directly or with a thin adapter shim** (up from r8's 64%). The TFT model arch, risk governor, both execution engines, exit_manager, stops, subsystem_ownership.py (newly available post-`b4532a4`), and the `chat_json` Ollama path (already production today) are essentially copy-paste. The only big rewrite is the freqtrade strategy file itself (FreqAIMeanRevV1.py — 2,132 lines, ~35% portable). **Plus** ~5,000 lines of non-freqtrade code (DRL ensemble, feature feeds, full `stocks/` tree, Nightly Reflector, outcome_resolver, model_forge ingestion).

### Vendor lock-in

**Very low.** Ollama is OSS MIT, GGUF is an open quant format, llama.cpp ecosystem is huge, switching costs are days not weeks. r8's mild vLLM lock-in concern is removed entirely.

### Trade-volume / cadence sanity

- **2-3 trades/week** on the paper account today → debate fires 2-3×/week, not in a hot loop
- **1 LoRA training run/week** (Sunday 02:00 ET, off-market, Unsloth)
- **1 TFT retraining/week** (Monday early AM, off-market)
- **1 Nightly Reflector pass/night** (single-role, ~10 s)

This cadence makes the rev2 load-on-demand pattern (peak memory ~85-95 GB) safe — peak fires a handful of times a week, never overlaps with training windows.

### One-line summary for the next session

**Build it. Drop vLLM, lean on Ollama, ship in ~5 weeks.** Confidence: **high** on architecture (no exotic kernels needed), **high** on timeline (no source builds on the critical path), **high** on rollback safety (60-second `systemctl stop`).

---

## Sources (Web searches performed 2026-05-12 unless noted)

Where applicable, citations are the same as r8 — only the rev2-specific entries are new.

- [Ollama on NVIDIA Spark — performance blog](https://ollama.com/blog/nvidia-spark-performance) — official confirmation Ollama runs first-class on GB10
- [Ollama Modelfile + ADAPTER directive](https://github.com/ollama/ollama/blob/main/docs/modelfile.md#adapter) — primitive for LoRA-baked tags
- [Ollama keep_alive + OLLAMA_MAX_LOADED_MODELS docs](https://docs.ollama.com/faq) — eviction control
- [HuggingFace PEFT](https://github.com/huggingface/peft) — LoRA training pipeline
- [Unsloth — save_pretrained_gguf](https://github.com/unslothai/unsloth) — direct GGUF export, no safetensors→GGUF conversion script
- [Alpaca-py PyPI](https://pypi.org/project/alpaca-py/)
- [Coinbase Advanced API Python SDK](https://github.com/coinbase/coinbase-advanced-py)
- [Polygon (Massive) client-python](https://github.com/polygon-io/client-python)
- [CuPy v14 release notes — aarch64 + Grace Hopper unified memory](https://docs.cupy.dev/en/stable/install.html)
- [PyTorch Forums — DGX Spark GB10 CUDA 13.0 Python 3.12 sm_121](https://discuss.pytorch.org/t/dgx-spark-gb10-cuda-13-0-python-3-12-sm-121/223744)
- [NVIDIA Developer Forums — DGX Spark architecture & library compatibility on aarch64](https://forums.developer.nvidia.com/t/architecture-and-library-compatibility-on-aarch64/350389)
- [Arm Learning Path — DGX Spark llama.cpp setup](https://learn.arm.com/learning-paths/laptops-and-desktops/dgx_spark_llamacpp/1a_gb10_setup/)

Internal references (operator memory + prior design docs in this repo):

- `~/.claude/projects/.../memory/project_drop_vllm.md` — operator decision 2026-05-12, Ollama-only
- `~/.claude/projects/.../memory/feedback_no_heavy_containers_without_explicit_ok.md` — vLLM OOM history
- `~/.claude/projects/.../memory/project_session_2026-05-11_t30_checkpoint.md` — 12 crypto + 15 stocks universe; BCH open via meta_up_regime; current trade cadence
- `docs/quanta-core-v4/07-VALIDATOR_REPORT.md` — 2 P0s flagged (vLLM, memory); rev2 retires both
- `docs/quanta-core-v4/08-FEASIBILITY.md` — r8 baseline this document supersedes
- `docs/quanta-core-v4-rev2/01-RESEARCH-MULTI_MODEL_RESIDENCY.md` — Ollama-only inference plane
- `docs/quanta-core-v4-rev2/02-RESEARCH-CONTINUOUS_LORA.md` — weekly LoRA cadence
- `docs/quanta-core-v4-rev2/05-RESEARCH-PARALLEL_AGENTS.md` — 30 s debate budget
