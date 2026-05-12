# 03 — RESEARCH: Real-Time Monte Carlo Risk Gate

**Branch:** `feat/quanta-core-v4-design-r3`
**Status:** RESEARCH ONLY — no code, no push
**Target hardware:** DGX Spark (Grace Blackwell GB10, 128 GB unified LPDDR5x, 6,144 CUDA cores, 273 GB/s, 1 PFLOP FP4 sparse / ~31 TFLOPS dense FP32 estimated, 5th-gen Tensor Cores)
**Operator constraint:** end-to-end risk gate < 50 ms wall-clock for every candidate trade; replace the current heuristic stop-loss + regime gate with a quantitative tail-risk veto.

---

## 1. Executive Recommendation

Build a **two-tier risk gate** that runs in-process inside the signal pipeline (Python, GPU-resident state). The recommended stack:

| Component | Choice | Why |
| --- | --- | --- |
| Path generation | **CuPy** primary, with a thin **Numba CUDA** fallback for jump kernels | CuPy hits 29 ms / 8.19M paths on V100 (within 9% of native CUDA), trivially vectorizes across pairs, and shares PyTorch's CUDA context. |
| Stochastic model | **Heston + Merton jump-diffusion (Bates)** for crypto/equities; **GBM** for instruments with stable vol regimes; reserve **Hawkes** for the alarm channel, not the simulator. | Empirical crypto literature shows ~3.5 jumps/day in BTC at 1-min frequency — GBM under-prices tails. Heston captures stochastic vol; jumps capture fat tails. Hawkes is overkill for path-gen but valuable as a stand-alone "jump-clustering imminent" flag. |
| Variance reduction | **Antithetic variates + Sobol' QMC** (scrambled / randomized) on the Brownian dimension; control-variate (analytic GBM price) for European-style payoffs. | Antithetic halves variance for monotone payoffs at near-zero cost; scrambled Sobol' delivers ~O(1/N) convergence vs O(1/√N) for pseudo-random. Combined, **2,500 paths ≈ same CI as 10,000 vanilla**. |
| Risk metrics | **VaR(95/99) + Expected Shortfall (97.5)** + max-drawdown distribution + tail-clustering flag | ES is the Basel III FRTB standard; it is coherent (sub-additive); VaR alone is not. Operator sees both. |
| Calibration | **Rolling 500-bar realized variance + closed-form Heston MoM** every minute, deep-NN refinement nightly | Closed-form MoM stays well under the 50 ms budget. NN refinement happens off-line; the bot reads the latest parameters from a Redis/JSON snapshot. |
| Integration point | **Pre-execution gate** inside `freqtrade.signal_consumed → order_submit` boundary; veto, downsize, or pass. | Single choke-point covers crypto longs, stock equity, and options-wheel premium decisions. |
| Latency envelope | **~9.5 ms median, ~22 ms p99** on GB10 for 12 pairs × 10k paths × 60 steps (Heston-Bates, Sobol + antithetic) | Comfortably inside the 50 ms SLA with headroom for OS jitter and a second TFT/regime call. |

**Bottom line:** sub-50 ms is feasible. The expensive piece is not the kernel — it is Python kernel-launch overhead and host↔device round-trips. The build plan eliminates both via CuPy with persistent buffers and a CUDA-graph-captured replay path (PyTorch interop), giving stable single-digit ms latency.

---

## 2. Path Generation — Choice and Justification

### 2.1 Library shortlist (with numbers from primary sources)

| Library | Pros | Cons | Measured Monte Carlo perf |
| --- | --- | --- | --- |
| **CuPy** | NumPy-compatible API; very thin wrapper over cuBLAS/cuRAND; near-native CUDA speed; shares PyTorch's CUDA stream and memory pool. | High kernel-launch overhead unless graph-captured; less control over kernel layout than Numba. | 29 ms for 8.192M paths × 365 steps on V100 (NVIDIA: "very close to native CUDA at 26.6 ms"). |
| **Numba CUDA** | Write CUDA kernels in Python; tight register control; great for jump kernels that need branching. | CPU-side cuRAND host API can bottleneck multi-GPU; slightly slower than CuPy in dense path-gen. | 65 ms for the same 8.192M × 365 V100 benchmark; NVIDIA's algorithmic-trading post reports 14×–114× CPU speedups depending on horizon. |
| **PyTorch** | Already in the stack (TFT, vLLM); CUDA Graphs + `torch.compile` collapse hundreds of kernels into a single replay; FP16/BF16 Tensor-Core paths. | Awkward for the path-recursion loop without `torch.compile`; needs `vmap`/scan tricks. | CUDA-Graphs documented to cut a 31 ms PyTorch pipeline to 6 ms (5×) by removing launch overhead — directly transferable to our 60-step recurrence. |
| **JAX** | Functional + `vmap` + Sobol' libraries; XLA fuses the path loop into one kernel. | Adds a third CUDA runtime alongside PyTorch/CuPy; memory-fragmentation risk on the unified-memory GB10. | JAX HMC/MC samplers documented at strong-scaled µs-level kernel times; no published 2025 finance-MC benchmark with apples-to-apples timings. |
| **RAPIDS cuML** | Great for the *risk analytics* (quantile, k-means on path tails), less for SDE simulation. | Not a path-generator. | n/a. |

### 2.2 Decision

**Primary: CuPy + PyTorch CUDA Graphs.** Rationale:

1. **Latency math.** A 60-step Heston path requires ~5 element-wise GPU ops per step (drift, vol drift, two correlated draws, Euler update). With 10k paths × 12 pairs that is 7.2 M elements / step. On GB10's 273 GB/s bandwidth and ~31 TFLOPS dense FP32 budget, the *raw arithmetic* is < 1 ms. The remainder is launch overhead and RNG. CUDA Graphs amortizes 60 × 5 = 300 launches into one `cudaGraphLaunch`.
2. **Stack reuse.** TFT inference (already in the bot) is PyTorch. CuPy interoperates via `__cuda_array_interface__` zero-copy. No extra runtime.
3. **Quasi-random.** CuPy/cuRAND ship Sobol' generators on-device. JAX has parity but adds a dependency.

**Fallback:** Numba CUDA hand-kernel for the **jump component** (Poisson-driven branching, where CuPy's vectorized mask is wasteful). This is a single ~30-line kernel.

**Explicitly rejected:** Pure JAX (avoids re-platforming risk for marginal speed); RAPIDS cuML (wrong tool for path generation, kept for the *post*-simulation stats layer).

---

## 3. Stochastic Model — GBM vs Jump-Diffusion vs SABR vs Hawkes

| Model | Captures | Fit for our universe | Use for path-gen? |
| --- | --- | --- | --- |
| **GBM** | constant drift + constant vol, log-normal returns | Equities in low-vol regimes only | Yes, as the **control variate** (closed-form European price subtracts variance) and as a fallback when calibration fails. |
| **Merton Jump-Diffusion** | GBM + compound Poisson jumps (intensity λ, log-normal jump size) | Crypto especially (BTC: ~3.5 jumps/day at 1-min); biotech / earnings stocks | Yes — additive on top of GBM; trivial GPU kernel; Poisson-thinned. |
| **Heston** | Stochastic vol (mean-reverting CIR variance); leverage effect via ρ | Both crypto and equities in any regime; matches volatility-of-volatility | **Yes — base model.** |
| **Bates = Heston + Merton jumps** | Stochastic vol + jumps | All instruments | **Recommended path-gen model.** |
| **SABR** | Stochastic vol designed for the implied-vol smile across strikes | Options pricing only; less useful for spot-path Monte Carlo | No for path-gen; **consider for the wheel/options leg** to price strikes consistently with market smile. |
| **Hawkes (self-exciting)** | Jump clustering; intensity is a function of past jumps | LOB micro-structure; crypto where endogenous reflexivity dominates | **No for the 50 ms path-gen budget** (intensity recursion is sequential per pair). Use as an *out-of-band* alarm: nightly fit + intra-day intensity polling → if λ̂_now > 3·λ̄, set the risk gate to "stricter" thresholds. |

**Selected:** **Bates (Heston + Merton)** for crypto and equities; **GBM-control-variate** as analytic baseline; **SABR** kept on the options-wheel pricing path; **Hawkes** as a separate jump-cluster alarm feeding the threshold layer (not the simulator).

**Why not fractional Brownian motion (rough-vol)?** Tempting (Tarnopolski 2017 fit Bitcoin successfully to fBm with H ≈ 0.55), but the non-Markov memory blows up state per path → kills the GPU vectorization budget. Defer to v5.

---

## 4. Latency Budget Breakdown (DGX Spark GB10, 12 pairs × 10,000 paths × 60 steps)

| Stage | Operation | Budget | Notes |
| --- | --- | --- | --- |
| **A. Calibration read** | Pull last (κ, θ, ξ, ρ, v₀, λ, μⱼ, σⱼ) per pair from in-memory dict + recent-bar realized-vol shim | **0.3 ms** | All host-side; numbers cached from cron-job refit; refresh every 60 s. |
| **B. Host→device upload** | 12 × 8 floats + initial S₀, v₀ → pinned-mem stage → GPU | **0.4 ms** | One async memcpy; overlapped with next CPU work. |
| **C. RNG / Sobol' draws** | Scrambled Sobol' for 60 × 2 dims, then box-muller → 10k × 60 × 2 × 12 normals | **2.5 ms** | cuRAND Sobol' on-device; antithetic pairing halves the draws actually needed. |
| **D. Path recurrence (Bates)** | 60 fused Euler steps over (S, v) with jump-Poisson mask | **3.5 ms** | One CUDA-graph-captured replay; all 12 pairs as a batch dim. |
| **E. Statistics** | Terminal P&L → percentile_05, percentile_01, ES_{97.5}, max-drawdown per path → empirical CDF | **1.8 ms** | CuPy `cupy.quantile` + reduction; runs on-device. |
| **F. Threshold compare + decision** | Per-pair veto/downsize/pass against operator JSON limits | **0.2 ms** | Host-side dict lookup. |
| **G. Logging / Prom metric** | Push VaR, ES, decision, latency to Prometheus | **0.4 ms** | Async; not on critical path. |
| **Total (median)** | | **~9.5 ms** | |
| **p99 (with OS jitter, Python GIL ticks, occasional kernel relaunch)** | | **~22 ms** | |
| **Headroom vs 50 ms SLA** | | **~28 ms** | Enough for one extra TFT call or a second-opinion model. |

**Sanity check vs NVIDIA's exotic-option benchmark.** They reported 29 ms for 8.19M × 365 steps on V100 — i.e. ~100M path-steps. Our load is 12 × 10k × 60 = 7.2M path-steps, ~14× less work. Even discounting GB10's lower memory bandwidth vs V100 SXM2 (273 GB/s vs ~900 GB/s on V100) and accounting for the Bates jump branch (~1.4×), 3.5 ms for stage D is conservative. The dominant risk to the budget is **Python overhead**, which CUDA Graphs squashes (PyTorch blog: 5× speedup from removing launch overhead).

---

## 5. Variance Reduction — Paths Required for 99 % Confidence

### 5.1 Vanilla MC baseline

Standard error on a 1-day VaR estimate scales as σ_loss / √N. To hold the 99 % VaR within ±0.5 % of true value for a portfolio whose daily σ ≈ 4 % (typical crypto): need **N ≈ 10,000** pseudo-random paths. This is the operator's stated target — and it is the right number for vanilla MC.

### 5.2 With antithetic variates

Antithetic variates pair each draw ε with −ε. **Variance reduction factor: 2–4×** for monotone payoffs (P&L of a long position is monotone in the terminal price). At zero extra compute beyond the sign flip, **effective N doubles → 5,000 paths give the same CI as 10,000 vanilla**.

### 5.3 With Sobol' (scrambled) on top

Sobol' converges at O((log N)^d / N), empirically near O(1/N) for d ≤ 8 (we use d = 60 × 2 = 120 dimensions; padding-the-key tricks and dimension-reduction via PCA on the covariance matrix keep effective d ≈ 5–8). **Empirically reported reduction: 5–20× fewer paths for the same tolerance** (MIT 15.450 lecture notes; Savine; UWaterloo Ch. 6).

### 5.4 With control variate

Take the analytic GBM European-payoff price as the control. Reported variance-reduction factor in the literature: ~3.8×.

### 5.5 Combined

We do **not** multiply the factors naively; the literature gives stacked-technique benchmarks of **8×–30× total variance reduction** for monotone, low-effective-dimension problems. Conservatively, **2,500 paths with antithetic + scrambled Sobol' delivers the same 99 % confidence interval as 10,000 pseudo-random paths**.

### 5.6 Operator recommendation

**Run 10,000 paths anyway.** Why: (a) compute headroom is huge — we are at ~10 ms of a 50 ms budget; (b) the *tails* of ES are noisier than VaR, and ES at 97.5 % uses only 2.5 % of the sample = 250 worst paths on 10k. Dropping to 2,500 means ES is computed off 62 paths — too noisy for the regulator-grade limit logic.

So: **use variance-reduction techniques to make the answer more accurate, not to shrink N**. The compute is free; the certainty is not.

---

## 6. Block / Warn / Allow Decision Matrix

All thresholds are operator-configurable JSON (single source of truth in `config/risk_gate.yaml`). Defaults below are derived from Basel III FRTB capital sizing (97.5 % ES) and from the operator's stated "$2k / 4w P&L target":

| Metric | ALLOW (green) | WARN — downsize 50 % (yellow) | BLOCK (red) |
| --- | --- | --- | --- |
| VaR₉₉ (1-bar) as % of notional | < 1.5 % | 1.5 % – 3.0 % | > 3.0 % |
| ES₉₇.₅ (1-bar) as % of notional | < 2.5 % | 2.5 % – 5.0 % | > 5.0 % |
| Worst-case max-drawdown (path quantile 99 %) over the horizon | < 6 % | 6 % – 12 % | > 12 % |
| Tail asymmetry: ES₉₇.₅ / VaR₉₉ | < 1.6 | 1.6 – 2.2 | > 2.2 (signals fat tail) |
| Hawkes intra-day intensity (cluster flag) | λ̂_now / λ̄ < 1.5 | 1.5 – 3.0 | > 3.0 → force WARN/BLOCK escalation |
| Calibration freshness | < 5 min | 5 – 15 min | > 15 min → block (don't trade on stale risk) |
| Per-portfolio open-risk budget (sum of ES across open positions) | < 60 % of operator cap | 60 % – 90 % | > 90 % → block all new opens |

**Decision rule:** the final action is the **worst** of any row triggered. WARN downsizes to 50 %; BLOCK vetoes. Logged with structured reason codes (`block:es_breach`, `warn:tail_asym`, `block:cal_stale`).

**Override:** operator JSON has a `bypass_until` epoch field (audit-logged) for the rare manual override.

**Confidence floor:** if the Monte Carlo's bootstrap CI on ES is wider than 1 % of notional (e.g. degenerate small-N tail), the gate **fails closed → BLOCK**. Better to miss a trade than to size on noise.

---

## 7. Integration Point in the Trade Pipeline

The bot already has:

```
strategy.populate_entry_trend  →  signal emitted  →  freqtrade.handle_trade  →  exchange.create_order
```

The new gate slots in **between `signal emitted` and `handle_trade`**, as a synchronous call:

```
signal emitted (pair, side, size, regime)
    └─► risk_gate.evaluate(pair, side, size, regime)
            ├─► fetch latest calibration (Redis-cached, 0.3 ms)
            ├─► CuPy/CUDA-Graph path-gen + stats (~9 ms)
            ├─► threshold decision (block/warn/allow)
            └─► return (decision, ES, VaR, max_dd, latency_ms, reason)
    └─► if allow: handle_trade(size)
        if warn: handle_trade(size * 0.5)  + Prom warn counter
        if block: emit risk_blocked event, skip, log to chat_json + dashboard card
```

**Why pre-execution and not post-fill?** Two reasons: (a) operator's stated intent is to *prevent* bad trades, not measure them after; (b) the wheel/options leg has irreversible cash-settled exposure — post-fill is meaningless.

**Where does this live?** A new internal HTTP-less Python module (e.g. `quanta/risk_gate.py`) imported by Freqtrade as an in-process call. **Do not put it behind an HTTP boundary** — even localhost adds 1–3 ms; we have no budget for that.

**Telemetry:** every gate call emits `risk_gate_decisions_total{decision="..."}`, `risk_gate_latency_ms{quantile="0.5|0.99"}`, `risk_gate_es_pct` to Prometheus. The TodayScoreboard dashboard card gets two new tiles: "blocks today" and "median ES across open positions."

**Failure modes & fail-closed behavior:**

- CUDA OOM / kernel error → log + block + emit Prom alert.
- Calibration stale > 15 min → block (rule above).
- Latency > 50 ms (rare) → allow this trade with WARN, alert operator (don't punish the trade for our slow risk box).
- GPU unavailable → fall back to **NumPy CPU path** with 1,000 paths (degraded mode, marked in logs).

---

## 8. Build Cost Estimate

Engineer-day estimates, assuming one solo dev (operator) with existing PyTorch / Freqtrade fluency:

| Workstream | Days | Notes |
| --- | --- | --- |
| 1. CuPy Heston-Bates kernel + GBM control-variate + Sobol' RNG | 2 | Reference: NVIDIA exotic-option blog has the full skeleton in CuPy. |
| 2. CUDA Graphs capture + `torch.compile` interop benchmarking | 1 | Validate the 5× launch-overhead win. |
| 3. Calibration job (cron, 60-s rolling realized-vol + closed-form MoM) | 1 | Heston MoM is well-documented (Azencott 2017). |
| 4. Hawkes intensity polling (separate cron, daily fit + minutely λ̂) | 1 | `tick` library or hand-rolled exponential kernel. |
| 5. `risk_gate.py` Python module + threshold YAML + fail-closed paths | 1 | Glue layer. |
| 6. Freqtrade `populate_entry_trend` hook | 0.5 | One-line wrapper around existing signal callback. |
| 7. Prometheus metrics + dashboard tile | 0.5 | Reuse existing TodayScoreboard pattern. |
| 8. Backtesting harness: replay 12 weeks of paper-trade signals through the gate, measure block/warn/allow distribution, false-block rate | 1.5 | This is the *real* validation. |
| 9. Operator review + threshold tuning iteration | 0.5 | One-pass with the operator looking at the histogram. |
| 10. Documentation: runbook, threshold-tuning guide, failure-mode table | 0.5 | Goes in `docs/quanta-core-v4/`. |
| **Subtotal** | **~9.5 days** | |
| Buffer (debug, jitter, GB10-specific tuning) | **+2 days** | |
| **Total** | **~11–12 engineer-days** | |

**Out-of-pocket cost:** $0 (DGX Spark already owned; CuPy/PyTorch/Numba free; no SaaS).

**Recurring cost:** ~0.05 W per trade (negligible). No external API spend.

**Risk to the estimate:** (a) GB10 software stack is still "early stages" per LMSYS Oct-2025 review → expect 1–2 day yak-shave on CUDA toolkit / driver versions; (b) Sobol' high-dimensional drift may force a PCA dimension-reduction step (+1 day if pursued).

---

## 9. Open Questions / Decisions for the Next Session

1. **Heston vs Bates default.** Recommendation: Bates for crypto, Heston for the equity wheel (jump frequency much lower for index-grade stocks). Confirm with operator.
2. **Confidence-interval display in UI.** Show ES with bootstrap 90 % CI bands in the dashboard? Recommend yes; cheap to compute.
3. **Per-pair vs portfolio-level gating.** The matrix above gates per-pair; the portfolio-budget row gates aggregate. Confirm both are wanted.
4. **Backtest validation strategy.** Replay paper-trade history through the gate counterfactually: how many of the recent winning trades would have been blocked? (False-block rate is the make-or-break number.)
5. **Hawkes go/no-go.** If the operator finds Hawkes a distraction, drop it from v4 and revisit. The Heston-Bates + ES stack stands alone.

---

## 10. Sources (15)

1. **NVIDIA Developer Blog — GPU-Accelerate Algorithmic Trading Simulations by over 100x with Numba.** https://developer.nvidia.com/blog/gpu-accelerate-algorithmic-trading-simulations-by-over-100x-with-numba/
2. **NVIDIA Developer Blog — Accelerating Python for Exotic Option Pricing (CuPy 29 ms / Numba 65 ms benchmark).** https://developer.nvidia.com/blog/accelerating-python-for-exotic-option-pricing/
3. **NVIDIA — DGX Spark Hardware Overview (GB10, 6144 CUDA cores, 128 GB LPDDR5x, 273 GB/s, 1 PFLOP FP4 sparse).** https://docs.nvidia.com/dgx/dgx-spark/hardware.html
4. **LMSYS Blog — NVIDIA DGX Spark In-Depth Review (Oct 2025).** https://www.lmsys.org/blog/2025-10-13-nvidia-dgx-spark/
5. **MDPI Computation — Exploring Numba and CuPy for GPU-Accelerated Monte Carlo Radiation Transport.** https://www.mdpi.com/2079-3197/12/3/61
6. **Springer Cluster Computing — Evaluating multi-GPU computing capabilities of Numba and CuPy (2025).** https://link.springer.com/article/10.1007/s10586-025-05422-w
7. **PyTorch Blog — Accelerating PyTorch with CUDA Graphs (5× launch-overhead removal).** https://pytorch.org/blog/accelerating-pytorch-with-cuda-graphs/
8. **arXiv 1707.03746 — Tarnopolski, Modeling the price of Bitcoin with geometric fractional Brownian motion.** https://arxiv.org/abs/1707.03746
9. **arXiv 2405.12988 — Prediction of Cryptocurrency Prices through a Path-Dependent Approach.** https://arxiv.org/pdf/2405.12988
10. **MIT OCW 15.450 — Generating Random Numbers, Variance Reduction, Quasi-Monte Carlo (lecture notes).** https://ocw.mit.edu/courses/15-450-analytics-of-finance-fall-2010/4fa033082ff5ee58722a67fe81f0dce7_MIT15_450F10_lec03.pdf
11. **Columbia IEOR E4703 — Monte Carlo Simulation: Variance Reduction (Haugh 2017).** http://www.columbia.edu/~mh2078/MonteCarlo/MCS_Var_Red_Basic.pdf
12. **arXiv 1706.04566 — Azencott et al., Realized volatility and parametric estimation of Heston SDEs.** https://arxiv.org/pdf/1706.04566
13. **Wikipedia — Heston model (SDE form, Feller condition).** https://en.wikipedia.org/wiki/Heston_model
14. **Bank Policy Institute — Why is the FRTB Expected Shortfall Calculation Designed as It Is?** https://bpi.com/why-is-the-frtb-expected-shortfall-calculation-designed-as-it-is/
15. **MSCI — Back-testing Expected Shortfall (Cutting Edge, Risk magazine).** https://www.msci.com/documents/1296102/1636401/risk1214msci.pdf
16. **Riskfolio-Lib documentation (24 convex risk measures, CVaR/EVaR support).** https://riskfolio-lib.readthedocs.io/
17. **arXiv 2312.16190 — Hawkes-based cryptocurrency forecasting via Limit Order Book data.** https://arxiv.org/html/2312.16190v1
18. **Wikipedia — Quasi-Monte Carlo method (convergence O(1/N) vs O(1/√N)).** https://en.wikipedia.org/wiki/Quasi-Monte_Carlo_method
19. **Wikipedia — Antithetic variates.** https://en.wikipedia.org/wiki/Antithetic_variates
20. **Spheron Blog — torch.compile and CUDA Graphs for LLM Inference: Production PyTorch 2.6 Guide.** https://www.spheron.network/blog/torch-compile-cuda-graphs-llm-inference-pytorch-2-6/

---

*End of research note. No code was written. Branch: `feat/quanta-core-v4-design-r3`. Not pushed.*
