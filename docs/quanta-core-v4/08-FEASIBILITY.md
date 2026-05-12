# Quanta Core V4 — Feasibility Confirmation

**Branch:** `feat/quanta-core-v4-design-r8`
**Audit date:** 2026-05-12
**Host:** `saijayanthai` (NVIDIA DGX Spark, GB10 / aarch64 / CUDA 13)
**Verdict (executive summary at the bottom):** **CONDITIONAL — buildable, but the memory math doesn't close on resident-models alone and ~30% of the Python install graph requires source builds or unofficial wheels.**

This report is ground-truth, not theory. Every hardware number is from a live `nvidia-smi` / `free -h` / `lscpu` / `df -h` taken at 15:55 UTC on 2026-05-12 (during normal paper-trading operation, ollama + a python3.14 worker were already resident). Every "ARM-clean?" cell is from a Web search performed in the same window.

---

## 1. Hardware capacity table (live numbers, snapshot 2026-05-12 15:55 UTC)

| Resource | Vendor spec | Measured (live) | Already in use | Headroom for V4 |
|---|---|---|---|---|
| CPU | NVIDIA Grace `Cortex-X925` ×10 + `Cortex-A725` ×10 = 20 cores | 20 cores, 1 socket, aarch64, SVE2/BF16/I8MM | ~irrelevant | full 20 cores |
| GPU | NVIDIA GB10 (Blackwell, sm_121, CC 12.1) | GB10 ×1, driver 580.142, CUDA 13.0 | ollama 21.2 GB + python3.14 12.2 GB ≈ **33.3 GB resident** | varies — see §1a |
| **Unified memory** | 128 GB LPDDR5X (Grace–Blackwell unified) | **121 GiB total** (`free -h`) | **80 GiB used / 41 GiB available** | **see §1a — does NOT close** |
| Swap | spec n/a | 31 GiB total, 5.6 GiB used | 26 GiB free | OK as overflow only |
| Disk (root) | 4 TB NVMe | **3.7 TB / 462 GB used / 3.1 TB free (13%)** | fine | abundant |
| NUMA | 1 node | `numactl --hardware`: node 0, 20 cpus, 124.6 GB | single node — no NUMA penalty to model | no cross-socket plumbing needed |
| /dev/shm | spec n/a | 61 GiB tmpfs (= half of RAM) | empty | usable for fast KV cache spillover if needed |

**Architecture:** `aarch64` (Ubuntu 24.04 / Linux 6.17 nvidia kernel). All Python wheels must be ARM-built or pure-Python.

### 1a. The 135 GB resident-models squeeze — does NOT close

The proposed budget from the V4 design is:

| Bucket | Proposed | Reality check |
|---|---|---|
| Resident models | 95 GB | possible only if everything else is *zero* |
| LoRA pool | 30 GB | n/a — adapters can live on disk and load on demand (~1 GB each, sub-second) |
| KV cache | 10 GB | depends entirely on context length × concurrent sessions |
| **Total** | **135 GB** | — |
| **Physical unified** | — | **121 GiB usable (`free -h`)** = 130.0 GB nominal minus kernel/firmware reserve |
| OS + freqtrade + ollama + dashboard + docker + chromium dev tabs (current steady state) | — | **~33 GB already burned at idle today** |
| **Realistic V4 ceiling** | — | **~85-90 GB before swap thrashes** |

**Conclusion: the 95 GB resident-models target is infeasible at full simultaneous residency.** We have to choose one of:

1. **Time-slice eviction** (already what `TFTModel.py` lines 1-12 documents — "Hermes 3 70B: ~40 GB, evicts between 15-min sentiment polls"). Use the same trick: a "hot tier" (Hermes 8B + TFT + ModelForge controller ≈ 35-45 GB) is always resident, plus *one* heavy model (Hermes 70B OR a 30B DeepSeek-coder-style code model) at a time. LRU evict the cold one. Disk → unified-mem reload of a 40 GB model is ~25-40 s off the NVMe; acceptable for non-tick-path use cases.
2. **Drop or quantize** the 70B Hermes to a 4-bit NVFP4 variant (~22 GB instead of 40 GB) — this re-balances the budget to ~75 GB resident, which DOES fit.
3. **Punt anything > 13B to an external Anthropic / Groq API endpoint** at request time. The operator has already paid that bill once (Hermes integration); the cost-aversion note in MEMORY.md says default to local.

Recommended path: **(1) + (2) — quantize the heavy LLM to NVFP4 and time-slice the 30B coder.**

### 1b. NUMA topology
Single NUMA node (Grace unified memory). No `numactl --cpunodebind` plumbing needed — but **be aware** that Grace+Blackwell share a single coherent memory pool, so a 90% RAM allocation by Python *will* starve the GPU. The `torch.cuda.set_per_process_memory_fraction(0.3)` cap in `TFTModel.py:176` is doing exactly the right thing today and must be preserved (or moved into a central GPU-budget arbiter).

### 1c. ARM-vs-x86 wheel hazards (currently installed)
Inspected via `pip3 list`:

| Library | Installed version | aarch64 wheel on PyPI? | Notes |
|---|---|---|---|
| `torch` | 2.11.0 | NO official cu130-aarch64 wheel | currently working — must be from a community build or `download.pytorch.org/whl/cu130`. **Provenance unknown — verify before V4 rebuild** |
| `torchaudio` | 2.11.0 | same as torch | bundled |
| `torchvision` | 0.26.0 | same as torch | bundled |
| `transformers` | 5.8.0 | yes (pure Python) | OK |
| `peft` | 0.19.1 | yes (pure Python + torch dep) | OK |
| `sentence-transformers` | 5.4.1 | yes | OK |
| `coinbase-advanced-py` | 1.8.2 | yes (pure Python) | OK |

**Hazard:** the existing torch 2.11.0 install almost certainly came from an unofficial wheel (cypheritai, assix, or natolambert dgx-spark-setup). If we touch `pip install --upgrade` on torch during V4 setup without pinning that index URL, we'll get an x86 wheel or no wheel and break the whole stack. **Action item: pin torch via a `--extra-index-url` written into `pyproject.toml` constraints, not into ad-hoc bash.**

---

## 2. Library / SDK availability on ARM (Grace CPU + Blackwell sm_121) — compatibility matrix

Each row reflects a Web search performed 2026-05-12. "ARM-clean" = PyPI wheel installs without source build. "GPU-tested" = at least one credible report of working on GB10 (sm_121). "Build complexity" = effort if pip fails.

| Library | ARM-clean (PyPI wheel)? | GPU on sm_121 tested? | Build complexity if needed | Notes |
|---|---|---|---|---|
| `alpaca-py` | YES (pure-py) | n/a (no GPU code) | trivial | already in `stocks/`; safe |
| `coinbase-advanced-py` | YES (pure-py) | n/a | trivial | already pinned 1.8.2; safe |
| `polygon-api-client` | YES (pure-py) | n/a | trivial | rebranded "massive.com" Oct 2025; API still works |
| `peft` (HuggingFace LoRA) | YES (pure-py + torch) | inherits torch ARM gotchas | low | OK once torch is OK |
| `transformers` | YES (pure-py) | inherits torch | low | OK |
| `sentence-transformers` | YES (pure-py) | inherits torch | low | OK |
| `torch` cu130 aarch64 | **NO official PyPI wheel** | yes (community wheels) | HIGH from source (~2 h, needs CUDA 13 + cuDNN 9, 80 GB build dir) | use `--extra-index-url=https://download.pytorch.org/whl/cu130` OR cypheritai/pytorch-blackwell community wheel. Pin or break. |
| `vLLM` (cu130 + sm_121) | **NO PyPI ARM cu130 wheel** | partial — known failure modes documented on NVIDIA forums (v2 protocol, kv_cache layout) | HIGH — 20-30 min build, prerelease | use `eelbaz/dgx-spark-vllm-setup` script OR `lharillo/vllm-blackwell-gb10-spark` Docker. Stay on vLLM ≥ 0.14 (the Jan-2026 NVFP4 build). |
| `cupy-cuda12x` ARM | YES (v14, Jan 2026) | yes (HMM/ATS unified mem supported) | n/a | install `cupy-cuda12x>=14.0` from PyPI — Grace Hopper / unified memory officially supported |
| `numba` CUDA on sm_121 | partial — `numba-cuda` works but JIT recompiles for sm_121 each cold start | yes via numba-cuda + CUDA 13 | medium | for the V4 risk-engine vectorisation we'd prefer `cupy` over `numba.cuda` — cupy's NVFP4 kernels are pre-compiled |
| `TensorRT-LLM` | **NO PyPI install on Blackwell** | yes (NVIDIA forum confirms GB10 + multi-node Llama working) | HIGH — NGC container required (~10 GB, build from `nvcr.io/nvidia/pytorch:25.01` base) | only justify if vLLM perf is insufficient; **add 1-2 dev-weeks** if we go this route |
| `flash-attn` cu130 sm_121 | **NO** | needs source build | HIGH | optional — only if attention kernels are a bottleneck |
| `freqtrade` | YES (pure-py) | n/a | trivial | keep installed during shadow window then archive |

**Headline blockers:** **3 out of 12 libraries** (torch, vLLM, TensorRT-LLM, flash-attn) require unofficial wheels or source builds. None are blocked outright — but none ship a single-command `pip install` either. **Plan ~3 dev-days of pure DevOps to lock down a reproducible build environment** (pinned wheel index URLs, a CI cache of the community wheels, a Dockerfile that pins CUDA 13.0.x + cuDNN 9.y exactly).

---

## 3. Existing code reuse audit

For each file: line count, freqtrade-import count, portability verdict. **"% portable" = approximate share of the code that would survive a copy-paste into a vanilla-Python project after the listed adapter work.**

| Current file | Lines | freqtrade refs | Target module in V4 | Portability | Effort |
|---|---|---|---|---|---|
| `user_data/strategies/FreqAIMeanRevV1.py` | 2132 | **24 imports** (`IStrategy`, `DecimalParameter`, `qtpylib`, `BasePyTorchClassifier`, FreqaiDataKitchen) | `quanta_core/strategies/mean_rev_v1.py` | **~30% portable** — the indicator math + regime/onchain/sentiment merges + the threshold-decision logic are clean; everything wrapped in `populate_*` and `custom_stoploss` is freqtrade-shaped | **rewrite** (~6-8 dev-days). Salvage: `_attach_onchain` / `_attach_sentiment` / `_attach_regime` (lines 178-300), threshold params, regime-gating dict (lines 327-356) |
| `user_data/freqaimodels/TFTModel.py` | 829 | **5 imports** (BasePyTorchClassifier, FreqaiDataKitchen, PyTorchDataConvertor) | `quanta_core/models/tft.py` | **~75% portable** — `TemporalFusionTransformer` arch + `_train` loop + `_validate_sharpe` + sliding-window builder are pure PyTorch. Only the `fit()` / `predict()` glue (signatures, `dk` kitchen, `data_dictionary` shape) needs an adapter shim | **adapter shim** (~3-4 dev-days). Replace `BasePyTorchClassifier` parent with our own trainer base; replace `dk` with a thin `FeatureWindow` dataclass |
| `user_data/freqaimodels/tft_pickle.py` | 716 | **6 mentions** (mostly in docstring explaining IResolver re-import workaround) | `quanta_core/models/tft_serde.py` | **~85% portable** — `TFTTrainerWrapper`, `validate_model_zip`, `scan_pair_dictionary_for_quarantine` are all standalone. The `sys.modules["TFTModel"]` legacy serialization shim becomes obsolete (no IResolver in V4) and can be dropped | **directly portable** (~1 dev-day cleanup) |
| `user_data/modules/risk_governor.py` | 759 | **2 mentions** (`freqtrade.enums.RunMode` for runmode extraction — comments only, the class itself is freqtrade-agnostic) | `quanta_core/risk/governor.py` | **~95% portable** — `RiskGovernor`, `RiskConfig`, `RiskDecision`, anchor persistence, Kelly, correlation matrix, circuit breaker are all pure stdlib + numpy + pandas | **directly portable** (~1 dev-day). Just drop the freqtrade.enums import path and accept `runmode: Literal["live","dry","backtest"]` as a plain string. Reuse as-is otherwise |
| `user_data/modules/execution_engine.py` | 664 | **1 mention** (log-file path comment — "separate from the main freqtrade log") | `quanta_core/execution/coinbase.py` | **~98% portable** — already self-contained around `coinbase-advanced-py`. No freqtrade objects touched anywhere | **directly portable** (~0.5 dev-day) — just rename the log handle and the `freqtft-` client_order_id prefix |
| `stocks/shark/execution/exit_manager.py` | 282 | **0** | `quanta_core/execution/exit_manager.py` | **100% portable** — pure stdlib + dict-of-dicts | **directly portable** (~0.5 dev-day) — already vanilla Python |
| `stocks/shark/execution/stops.py` | 216 | **0** (uses `alpaca-py` not freqtrade) | `quanta_core/execution/stops.py` | **100% portable** | **directly portable** (~0.5 dev-day) — just confirm the `_get_client()` factory points to the V4 alpaca-py adapter |
| `stocks/shared/subsystem_ownership.py` | — | — | — | **FILE NOT FOUND** — searched all of `/stocks/`, `/user_data/`, and the worktree. Either renamed or never created. The V4 design's "subsystem-ownership map" probably needs to be **written from scratch** | **rewrite** (~1-2 dev-days) — must be authored against the V4 module taxonomy |

**Summary of reuse:** ~3,600 of 5,598 audited lines (~64%) are directly or near-directly portable. The big rewrite is the freqtrade strategy file itself (2,132 lines, ~30% portable). The TFT model arch + risk governor + execution engines + exit/stops together (~2,800 lines) survive intact with only adapter shims.

**Existing infrastructure NOT in the audit list but worth retaining:**
- `user_data/modules/drl_ensemble.py`, `ensemble_voter.py`, `meta_agent.py` — DRL ensemble + meta-agent (pure PyTorch)
- `user_data/modules/onchain_signals.py`, `sentiment_engine.py`, `regime_detector.py` — feature feeds (pure stdlib + postgres)
- `user_data/modules/unified_risk.py`, `trade_journal.py`, `slack_alerts.py`, `telegram_alerts.py` — already vanilla
- `stocks/wheel/` — entire options-wheel runner is non-freqtrade and ports as-is
- `stocks/shark/` — entire stock-side agentic stack (graph, agents, schemas) is non-freqtrade

Add another **~5,000 lines of free-of-freqtrade code** that flow straight into V4.

---

## 4. Build-time Gantt — concrete dev-days per module

Wall-time is given for **3-agent parallel dispatch** on independent modules (operator's preferred mode per MEMORY.md → "dispatching-parallel-agents" skill).

| Module | Dev-days (single) | Wall-time (3-parallel) | Dependencies | Notes |
|---|---|---|---|---|
| **0. Build env lockdown** (Dockerfile pinning torch cu130 + vLLM + cupy + CUDA 13.0.x + cuDNN 9, CI cache of community wheels) | 3 | **3** | — | sequential; everything else blocks on this |
| **1. Data feed layer** (Alpaca + Coinbase + Polygon market-data adapters, websocket reconnect, sqlite/postgres tick store) | 5 | **2** | 0 | port from `stocks/api/` + `user_data/modules/db.py` |
| **2. Feature pipeline** (onchain + sentiment + regime merges, the `_attach_*` functions promoted to a `FeatureBus` class) | 6 | **2** | 1 | salvage from FreqAIMeanRevV1 lines 178-300 |
| **3. TFT model** (TemporalFusionTransformer + trainer + checkpoint/resume + stable serde wrapper) | 4 | **2** | 0 | ~75% lift from `TFTModel.py` + `tft_pickle.py` |
| **4. DRL ensemble + meta-agent** (PPO/A2C heads + voter + meta-signal) | 7 | **3** | 3 | port `drl_ensemble.py`, `ensemble_voter.py`, `meta_agent.py` |
| **5. LLM inference layer** (vLLM serving + NVFP4-quantized Hermes 70B + Hermes 8B hot + LRU evictor) | **8-12** | **4-5** | 0 | **biggest single risk item.** Includes vLLM source build, quant pipeline, LRU evictor service |
| **6. Risk governor + execution engines** (coinbase + alpaca + bracket orders + slippage gates) | 3 | **1** | 1 | 95%+ portable from existing |
| **7. Exit/stops/wheel/shark agentic stack** (port `stocks/shark` + `stocks/wheel`) | 4 | **2** | 6 | mostly verbatim — replace cron drivers |
| **8. Subsystem-ownership map + module taxonomy** | 2 | **1** | all | new file, written against the V4 design |
| **9. Shadow-mode runner** (parallel-write ledger, trade-by-trade diff, dashboard tile) | 5 | **2** | 1, 6 | see §5 |
| **10. Dashboard + observability** (port the existing 31-table dashboard SPA to read from V4 ledgers) | 6 | **2** | 9 | likely the second-biggest pain after vLLM |
| **11. Backtest harness + offline replay** | 4 | **2** | 1, 2 | reuse `user_data/backtest_results/` + freqtrade hyperopt as ground-truth comparator |
| **12. Migration runbook + cutover script + 2-week shadow + ledger diff acceptance** | 5 | **5** | 9, 10 | sequential — operator must sign off after each milestone |

**Totals:**
- Single-dev sequential: **62-66 dev-days** ≈ **~13 weeks**
- 3-parallel agent dispatch with operator review checkpoints: **~31 wall-days** ≈ **~6 weeks**
- Critical path: 0 → 5 (vLLM) → 9 (shadow) → 12 (cutover) = ~16 wall-days minimum, assuming no vLLM rebuild stalls

The **realistic operator-paced delivery** is **8 weeks wall-time** if vLLM build hiccups twice (one CUDA driver bump, one wheel-index incident — both are routine for this hardware).

---

## 5. Shadow-mode migration safety

**Yes, shadow-mode is feasible — and is the only honest path to cutover.** The current bot has all the seams needed.

### 5.1 Shadow procedure (2-week window)

1. **Both consume the same WebSocket feeds** — easy: Coinbase + Alpaca + Polygon WS clients accept multiple subscribers cheaply. Add a `shadow=true` flag to V4 so it reads but writes to a separate ledger.
2. **Separate ledgers:**
   - Live (truth): freqtrade DB at `user_data/tradesv3.sqlite` + Alpaca paper account
   - Shadow (V4): new sqlite at `quanta_core/state/v4_ledger.sqlite` + a "shadow" Alpaca subaccount OR purely simulated fills against the real WS order book
3. **No real V4 orders during shadow** — V4 makes the decision, journals "would-have-placed BUY 0.01 BTC @ 65000 @ 14:32:01.231Z", but the side-effect ends there. Compares against what freqtrade ACTUALLY did at the same timestamp.
4. **Trade-by-trade diff dashboard tile** — new pane in the SPA listing every divergence: same-direction-different-size, same-side-different-timing, V4-only entries, freqtrade-only entries. Operator scans the diff daily.
5. **Acceptance criteria (operator-set):**
   - V4 PnL ≥ freqtrade PnL on a 10-trading-day rolling window, OR
   - V4 PnL within ±$200 of freqtrade BUT meets the qualitative criteria: faster reaction (median delta < 0), lower drawdown peak, fewer missed-regime trades
   - No "trades that freqtrade declined and V4 took" with > $50 single-trade loss
6. **Rollback** — V4 is `systemctl stop quanta-core` away from off. The shadow ledger stays on disk for forensics. Freqtrade was never paused. Total rollback time: < 60 seconds.
7. **Cutover** — only after 10 consecutive trading days of acceptance criteria. Cutover flips a single env var `QUANTA_LIVE_TRADING=true` and the next signal places a real order. Freqtrade is reduced to paper-mode-only for one more week before final shutdown.

### 5.2 What kills the shadow window

- Free-tier rate-limit collisions on Polygon WS if both subscribers share the same key. **Mitigation:** request a second WS connection slot (Polygon allows up to 5 on standard) or use the Alpaca free-tier WS for shadow.
- Postgres write contention if both write to the same regime/onchain/sentiment tables. **Mitigation:** V4 uses a separate schema (`quanta_core.*`) or a separate DB.

---

## 6. Vendor lock-in assessment

If we go all-in on NVIDIA NIM / vLLM / TensorRT-LLM / NVFP4 on Grace-Blackwell, what's the exit cost?

| Lock-in dimension | Severity | Notes / mitigation |
|---|---|---|
| **vLLM** | LOW | vLLM is OSS Apache-2.0, runs on AMD ROCm + Intel Habana too. Port cost: re-quantize models (NVFP4 → FP8 / INT4). ~1 dev-week if forced. |
| **TensorRT-LLM** | MEDIUM-HIGH | NVIDIA-only. Port cost: rewrite serving layer in vLLM-pure (we'd skip TensorRT-LLM unless vLLM proves insufficient — see §2 recommendation). **Decision: don't adopt TensorRT-LLM in V4 r8 unless vLLM benchmarks < target by > 30%.** |
| **NVFP4 quantization** | MEDIUM | NVFP4 is an NVIDIA-defined format but the underlying math (4-bit log-quant) is portable. Re-quantize to MXFP4 (open standard, supported on AMD MI300X and Intel Gaudi 3) at ~1 day per model. |
| **Grace-Blackwell unified memory** | MEDIUM | Code that assumes "GPU and CPU see the same pointer" (cupy with HMM, torch with `pin_memory=True` against unified mem) needs review when porting to discrete-memory GPUs. Mitigation: write the data-path against torch's `.to(device, non_blocking=True)` standard idiom. We're already doing this in `TFTModel.py:646-648`. |
| **NIM (NVIDIA Inference Microservices)** | HIGH if adopted | Vendor-managed catalog. **Decision: don't adopt NIM in r8 — stick with vLLM-self-hosted to preserve portability.** |
| **CUDA 13** | LOW | CUDA is portable across NVIDIA GPUs. The risk is on the toolchain-availability side (wheels). |
| **DGX Spark form factor** | LOCKED for now | This is the operator's only AI workstation. No exit needed unless hardware is replaced. |

**Net vendor-lock-in posture for V4 r8:** acceptable. We'd be trading one lock-in (freqtrade framework) for a milder one (vLLM + cupy + torch on aarch64+CUDA13 + Grace unified memory). vLLM and cupy are OSS; the unique-to-NVIDIA pieces (NIM, TensorRT-LLM, NVFP4) are explicitly opt-out in this design pass.

**Recommendation:** add a `quanta_core/serving/llm_backend.py` interface with `VLLMBackend`, `OllamaBackend`, and a placeholder `TGIBackend`. All model calls go through the interface, so a future port to discrete-GPU + AMD ROCm is a single-class swap.

---

## 7. Bottom line — FEASIBLE / CONDITIONAL / NOT_FEASIBLE

### Verdict: **CONDITIONAL — FEASIBLE with three required conditions**

The Quanta Core V4 design is **buildable** on the actual DGX Spark hardware in **~8 wall-weeks** with the operator's preferred 3-agent parallel dispatch, AND it can run safely in shadow beside freqtrade for ≥ 2 weeks before any real money moves — **provided the following three conditions are met:**

#### Condition 1 — Memory budget must be revised
The proposed 95 GB resident-models + 30 GB LoRA + 10 GB KV = 135 GB does NOT fit in the live-measured 121 GiB usable (with ~33 GB already burned at idle). The realistic V4 ceiling is **~85-90 GB resident before swap thrashes.** Either:
- Quantize Hermes 3 70B to NVFP4 (~22 GB instead of ~40 GB), AND
- Time-slice the second heavy model (30B coder) with LRU eviction to disk (~25-40 s cold-load off NVMe), AND
- Keep `torch.cuda.set_per_process_memory_fraction(0.3)` (or move it to a central arbiter)

#### Condition 2 — Build environment must be pinned, not pip-installed
3 of the 12 critical libraries (torch cu130 aarch64, vLLM cu130 aarch64, TensorRT-LLM if adopted, flash-attn) have **no official PyPI wheel** for our combination of `aarch64 + CUDA 13 + sm_121`. A reproducible build environment (Dockerfile + pinned `--extra-index-url` for the cypheritai / natolambert community wheels, OR a `nvcr.io/nvidia/pytorch:25.01` base) **must be the first deliverable** — every other module depends on it. Budget: 3 dev-days. Cost of skipping: indefinite stall the first time `pip install --upgrade` runs.

#### Condition 3 — Migration is shadow-first, no flag-day
The freqtrade stack stays running during all V4 buildout. V4 only goes live after **10 consecutive trading days** of shadow-mode meeting acceptance criteria (see §5.1.5). Rollback is `systemctl stop quanta-core` and is < 60 seconds. Freqtrade is reduced to paper-only for ≥ 1 week after V4 goes live, kept warm in case of late regressions.

### Reuse value
**~64% of the audited 5,598 lines port directly or with a thin adapter shim.** The TFT model arch, risk governor, both execution engines, exit_manager, and stops are essentially copy-paste. The only big rewrite is the freqtrade strategy file itself (FreqAIMeanRevV1.py — 2,132 lines, ~30% portable), which is exactly what a freqtrade-decoupling project should look like. **Plus** another ~5,000 lines of non-freqtrade code (DRL ensemble, feature feeds, the entire `stocks/` tree) that flow into V4 free.

### Vendor lock-in
Acceptable as designed (vLLM + cupy + OSS torch). The high-lock-in items (TensorRT-LLM, NIM, raw NVFP4 dependencies) are explicitly out-of-scope for r8; they can be added later if benchmark gaps demand them.

### One-line summary for the next session
**Build it. Pin the wheels first, quantize the 70B second, shadow for 10 days third. Don't touch flag-day cutover before all three are green.** Confidence: high on the architecture, medium on the timeline (vLLM build is the single biggest schedule risk), high on rollback safety.

---

## Sources (Web searches performed 2026-05-12)

- [vLLM GPU installation docs](https://docs.vllm.ai/en/stable/getting_started/installation/gpu/)
- [eelbaz/dgx-spark-vllm-setup — one-command vLLM for GB10](https://github.com/eelbaz/dgx-spark-vllm-setup)
- [lharillo/vllm-blackwell-gb10-spark Docker](https://hub.docker.com/r/lharillo/vllm-blackwell-gb10-spark)
- [NVIDIA Developer Forums — DGX Spark architecture & library compatibility on aarch64](https://forums.developer.nvidia.com/t/architecture-and-library-compatibility-on-aarch64/350389)
- [CuPy v14 release notes — aarch64 + Grace Hopper unified memory](https://docs.cupy.dev/en/stable/install.html)
- [PyTorch Forums — DGX Spark GB10 CUDA 13.0 Python 3.12 sm_121](https://discuss.pytorch.org/t/dgx-spark-gb10-cuda-13-0-python-3-12-sm-121/223744)
- [natolambert/dgx-spark-setup — ML setup for GB10 Blackwell aarch64](https://github.com/natolambert/dgx-spark-setup)
- [cypheritai/pytorch-blackwell — pre-built cu130 aarch64 sm_121 wheels](https://github.com/cypheritai/pytorch-blackwell)
- [Flash Attention on sm_121 — PyTorch compatibility on GB10](https://medium.com/@rakshith.d26/flash-attention-on-sm-121-solving-pytorch-compatibility-on-blackwell-gb10-a83d9ff3cf9b)
- [NVIDIA TensorRT-LLM repo](https://github.com/NVIDIA/TensorRT-LLM)
- [Multi-Node TensorRT-LLM Inference on GB10 Cluster](https://medium.com/@aruna.kolluru/multi-node-llm-inference-on-gb10-cluster-706c748a32b6)
- [Alpaca-py PyPI](https://pypi.org/project/alpaca-py/)
- [Coinbase Advanced API Python SDK](https://github.com/coinbase/coinbase-advanced-py)
- [Polygon (Massive) client-python](https://github.com/polygon-io/client-python)
- [HuggingFace PEFT](https://github.com/huggingface/peft)
- [Arm Learning Path — DGX Spark llama.cpp setup](https://learn.arm.com/learning-paths/laptops-and-desktops/dgx_spark_llamacpp/1a_gb10_setup/)
