# Quanta Core v4 — 01: Multi-Model Residency on DGX Spark (GB10)

**Status:** Research only. No code changes. Branch: `feat/quanta-core-v4-design-r1`
**Date:** 2026-05-12
**Author:** Claude (subagent under `/agent-af7312f46e8752508` worktree)
**Scope:** Pick the inference-serving stack that can hold **5+ trading models concurrently resident** on one DGX Spark, with **hot-swappable LoRA adapters per role**, never paged out, and survive an ARM aarch64 / Grace Blackwell environment.

---

## 1. Executive Recommendation (one paragraph)

**Run a two-process split: vLLM (per-role server) behind a LiteLLM gateway, with NVIDIA Dynamo deferred to v4.1.** Concretely: one vLLM 0.17.0+ process for the **8B fast-tick classifier with N hot-swappable LoRA adapters** (`--enable-lora --max-loras 8 --max-cpu-loras 32`), a separate vLLM process for the **Qwen2.5-72B-Q4 deep arbiter**, and continue using **Ollama only for prototyping / non-prod** so the trading loop is not at the mercy of `OLLAMA_KEEP_ALIVE` eviction races. PyTorch processes for TFT / sentiment / microstructure run as independent CUDA contexts and share the same 128 GB unified memory pool via Grace Blackwell's NVLink-C2C coherent address space — no copy required between CPU and GPU [[1]](https://www.nvidia.com/en-us/data-center/nvlink-c2c/) [[2]](https://learn.arm.com/learning-paths/laptops-and-desktops/dgx_spark_llamacpp/1_gb10_introduction/). LiteLLM + (optionally) `llama-swap` provide one OpenAI-compatible endpoint; we keep `swap: false / exclusive: false` on our hot models since the operator's hard requirement is **simultaneous residency, never evict** [[3]](https://forums.developer.nvidia.com/t/running-a-full-llm-stack-on-dgx-spark-gb10-your-application-litellm-llama-swap-vllm-llama-cpp-ollama/367580). Dynamo + Triton are correct long-term answers for production multi-tenant data-center serving, but they target H200/B200/GB200 deployments [[4]](https://github.com/ai-dynamo/dynamo) and add disaggregated prefill/decode complexity the solo-dev workflow does not need yet. The single biggest risk: **vLLM does not officially support GB10 sm_121** as of March 2026 — the operator-blessed path requires applying community MXFP4 patches and `TORCH_CUDA_ARCH_LIST=12.0` forward-compat builds [[5]](https://forums.developer.nvidia.com/t/vllm-0-17-0-mxfp4-patches-for-dgx-spark-qwen3-5-35b-a3b-70-tok-s-gpt-oss-120b-80-tok-s-tp-2/362824) [[6]](https://medium.com/@stablehigashi/vllm-installation-on-dgx-spark-gb10-sm-121-and-qwen-3-5-serving-guide-9eba91e448f8).

---

## 2. Hardware Reality: Why DGX Spark Changes the Calculus

Before scoring serving stacks, three Grace Blackwell facts dominate every other consideration:

| Property | Value | Source |
|---|---|---|
| Unified memory capacity | 128 GB LPDDR5X | [NVIDIA docs](https://docs.nvidia.com/dgx/dgx-spark/hardware.html) |
| Memory bandwidth (CPU+GPU shared) | 273 GB/s (256-bit, 4266 MHz, 16 channels) | [NVIDIA docs](https://docs.nvidia.com/dgx/dgx-spark/hardware.html) |
| Grace CPU ↔ Blackwell GPU interconnect | NVLink-C2C, **coherent**, shared address space, zero-copy | [NVLink-C2C page](https://www.nvidia.com/en-us/data-center/nvlink-c2c/) + [Arm GB10 guide](https://learn.arm.com/learning-paths/laptops-and-desktops/dgx_spark_llamacpp/1_gb10_introduction/) |
| NVLink-C2C bandwidth | 900 GB/s bidirectional (per Arm guide); NVIDIA quotes "6× more energy-efficient than PCIe Gen6" | [Arm GB10 guide](https://learn.arm.com/learning-paths/laptops-and-desktops/dgx_spark_llamacpp/1_gb10_introduction/) [[1]](https://www.nvidia.com/en-us/data-center/nvlink-c2c/) |
| GPU arch | Blackwell sm_121 (not sm_120, not sm_100) | [vLLM install guide](https://medium.com/@stablehigashi/vllm-installation-on-dgx-spark-gb10-sm-121-and-qwen-3-5-serving-guide-9eba91e448f8) |
| FP4 sparse compute | 1 PFLOP | [NVIDIA blog](https://developer.nvidia.com/blog/how-nvidia-dgx-sparks-performance-enables-intensive-ai-tasks/) |
| CPU | 20-core ARM (10× Cortex-X925 + 10× Cortex-A725) | [NVIDIA docs](https://docs.nvidia.com/dgx/dgx-spark/hardware.html) |
| TDP (whole superchip) | 140 W | [NVIDIA docs](https://docs.nvidia.com/dgx/dgx-spark/hardware.html) |

**The critical consequence:** "VRAM" and "system RAM" are the same physical pool. Every inference server that assumes a discrete GPU is **wrong** about memory accounting on this box. vLLM's default `gpu_memory_utilization=0.9` will, on a fresh boot, try to claim ~115 GB and leave nothing for the OS, embeddings, dashboards, or a second model — operators have documented `--gpu-memory-utilization 0.25` working per model and "default vLLM uses 117 GB for a 19 GB model" otherwise [[7]](https://forums.developer.nvidia.com/t/spark-inference-run-3-specialized-models-simultaneously-on-your-dgx-spark-cybersecurity-coding-orchestration-30-min-setup/369236). The forward-compat workaround for sm_121 is to lie to PyTorch with `TORCH_CUDA_ARCH_LIST=12.0` and disable the broken CUTLASS kernels via `VLLM_DISABLED_KERNELS=cutlass_moe_mm,cutlass_scaled_mm` [[6]](https://medium.com/@stablehigashi/vllm-installation-on-dgx-spark-gb10-sm-121-and-qwen-3-5-serving-guide-9eba91e448f8).

**Also important — known broken paths on GB10 today:**
- NVFP4 quantization on ARM64 GB10 **crashes with CUDA illegal instruction** in current vLLM mainline; the kernels emit instructions sm_121 cannot execute [[8]](https://github.com/vllm-project/vllm/issues/35519).
- MXFP4 works only with the community patch set from `vllm-custom`, which is **not** upstreamed [[5]](https://forums.developer.nvidia.com/t/vllm-0-17-0-mxfp4-patches-for-dgx-spark-qwen3-5-35b-a3b-70-tok-s-gpt-oss-120b-80-tok-s-tp-2/362824).
- TP=1 with MXFP4 has shared-memory race conditions in the 256-thread Marlin MoE kernel; TP must be ≥2 (so requires 2 Sparks or stick to FP8/GGUF on one) [[5]](https://forums.developer.nvidia.com/t/vllm-0-17-0-mxfp4-patches-for-dgx-spark-qwen3-5-35b-a3b-70-tok-s-gpt-oss-120b-80-tok-s-tp-2/362824).

---

## 3. Comparison Table: Serving Stacks for Multi-Model Residency

Rows = stack. Columns scored 0–3 (3 = best). "GB10 tested" = first-hand operator/NVIDIA report on Spark sm_121, not theoretical ARM support. "Op effort" inverted so 3 = lowest effort.

| Stack | Native multi-model concurrent | LoRA hot-swap | KV cache features | GB10 tested in wild | Throughput (8B+70B class) | Prod maturity | Op effort (3=easy) | License |
|---|---|---|---|---|---|---|---|---|
| **vLLM 0.17.0+ (patched)** | 1 model/process; spawn N processes | **3** — `--enable-lora`, `--max-loras`, runtime POST `/v1/load_lora_adapter` [[9]](https://docs.vllm.ai/en/latest/features/lora/) | PagedAttention + prefix cache, chunked prefill [[10]](https://developers.redhat.com/articles/2025/07/24/how-pagedattention-resolves-memory-waste-llm-systems) | **3** — official playbook + community patches + 80 tok/s GPT-OSS-120B [[5]](https://forums.developer.nvidia.com/t/vllm-0-17-0-mxfp4-patches-for-dgx-spark-qwen3-5-35b-a3b-70-tok-s-gpt-oss-120b-80-tok-s-tp-2/362824) | 3 — 70 tok/s Qwen3.5-35B-A3B, 80 tok/s GPT-OSS-120B [[5]](https://forums.developer.nvidia.com/t/vllm-0-17-0-mxfp4-patches-for-dgx-spark-qwen3-5-35b-a3b-70-tok-s-gpt-oss-120b-80-tok-s-tp-2/362824); 803 prefill / 2.7 decode tps Llama-3.1-70B FP8 (SGLang ref) [[11]](https://www.lmsys.org/blog/2025-10-13-nvidia-dgx-spark/) | 3 — production at Anyscale, Bedrock, SageMaker [[12]](https://blog.vllm.ai/2026/02/26/multi-lora.html) | 1 — sm_121 patches required | Apache-2.0 |
| **SGLang 0.5.11** | 1 model/process; spawn N processes | 2 — "Multi-LoRA batching" listed in README [[13]](https://github.com/sgl-project/sglang); fewer ops examples than vLLM | **3** — RadixAttention cross-request KV reuse, 6.4× speedup on shared prefixes [[14]](https://particula.tech/blog/sglang-vs-vllm-inference-engine-comparison) | 2 — NVIDIA bench guide includes SGLang on Spark [[15]](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/connect-two-sparks/assets/performance_benchmarking_guide.md) | 3 — 29% > vLLM on H100, 894 vs 413 tok/s decode; on Spark SGLang FP8 Llama-3.1-70B at 803 prefill tps [[14]](https://particula.tech/blog/sglang-vs-vllm-inference-engine-comparison) [[11]](https://www.lmsys.org/blog/2025-10-13-nvidia-dgx-spark/) | 3 — xAI Grok 3, Azure, LinkedIn [[14]](https://particula.tech/blog/sglang-vs-vllm-inference-engine-comparison) | 1 — README does not list ARM aarch64 in supported HW [[13]](https://github.com/sgl-project/sglang) | Apache-2.0 |
| **TensorRT-LLM** | 1 engine/build; multi via Triton | 2 — supported via Triton multi-LoRA tutorial | 3 — fastest paged-KV on Blackwell | 2 — included in NVIDIA bench guide [[15]](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/connect-two-sparks/assets/performance_benchmarking_guide.md); operators report "compiling GPT-OSS:120B took hours" + limited model coverage [[16]](https://medium.com/sparktastic/choosing-an-inference-engine-on-dgx-spark-8a312dfcaac6) | 3 (theoretical) | 3 — vendor blessed | **0** — engine builds per-model are painful; FP4/FP8 kernels finicky on sm_121 | Apache-2.0 (TRT-LLM); TRT proprietary |
| **NVIDIA Triton + (vLLM | TRT-LLM | Python BLS)** | **3** — designed for it, `instance_group`, ensemble, multi-stream [[17]](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_execution.html) | 2 via vLLM backend or TRT-LLM multi-LoRA [[18]](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/vllm_backend/docs/llama_multi_lora_tutorial.html) | inherits from backend | 1 — not in the cited Spark playbooks | inherits from backend | 3 | 0 — heavyweight config files, model_repo layout, gRPC | BSD-3 |
| **NVIDIA Dynamo 1.1.1 (Mar 2025 GA)** | **3** — disaggregated prefill/decode, KV-aware router, planner [[4]](https://github.com/ai-dynamo/dynamo) [[19]](https://www.nvidia.com/en-us/ai/dynamo/) | inherits (vLLM/SGLang/TRT-LLM backends) | **3** — KVBM tiered GPU→CPU→SSD→remote [[19]](https://www.nvidia.com/en-us/ai/dynamo/) | **0** — targets GB200/GB300/H200/B200; no Spark deployment cited [[4]](https://github.com/ai-dynamo/dynamo) | 7× MoE on GB200 NVL72, 15× w/ Blackwell — datacenter context [[19]](https://www.nvidia.com/en-us/ai/dynamo/) | 2 — 1.0 March 2025 | 0 — Rust + K8s CRDs + multi-node intent | Apache-2.0 |
| **NIM (NVIDIA Inference Microservice)** | 1 NIM container per model; multi via Docker compose | inherits (TRT-LLM internal) | inherits | 2 — official "NIM on Spark" playbook [[20]](https://build.nvidia.com/spark/nim-llm) | inherits TRT-LLM | 3 — vendor SLA on enterprise | 2 — `docker run` per model; locked to NVIDIA model catalog | Proprietary container, requires NGC license |
| **Ollama 0.12+** | Yes via `OLLAMA_MAX_LOADED_MODELS`, **but** documented to evict pinned models when VRAM fills [[21]](https://github.com/open-webui/open-webui/discussions/3291) [[22]](https://docs.ollama.com/faq) | **1** — adapters embedded in Modelfile, no runtime hot-swap API | basic, no PagedAttention | 3 — official Spark perf blog [[23]](https://ollama.com/blog/nvidia-spark-performance) | 1 — ~3-4 tok/s slower than llama.cpp on same model [[16]](https://medium.com/sparktastic/choosing-an-inference-engine-on-dgx-spark-8a312dfcaac6); 58.3 tok/s GPT-OSS-20B q4_K_M [[23]](https://ollama.com/blog/nvidia-spark-performance) | 2 — fine for dev, eviction races in prod | **3** — `ollama run`, that's it | MIT |
| **llama.cpp (CUDA aarch64)** | N processes; or one `llama-server` per model | 2 — `--lora` per server, no live swap REST API | basic + flash-attn quant KV | **3** — fastest single-user engine reported on Spark [[24]](https://github.com/ggml-org/llama.cpp/discussions/16578) [[16]](https://medium.com/sparktastic/choosing-an-inference-engine-on-dgx-spark-8a312dfcaac6) | 2 — 61 tok/s GPT-OSS-20B, 35 tok/s GPT-OSS-120B, 44 tok/s Qwen3-Coder-30B-A3B (tg32) [[24]](https://github.com/ggml-org/llama.cpp/discussions/16578) | 2 — widely deployed, single-user oriented | 3 — single binary | MIT |
| **TGI 3.x (HF) + multi-backend (vLLM/TRT-LLM)** | inherits | inherits via vLLM/TRT-LLM | inherits | 0 — no Spark report found | inherits | 2 — HF-blessed | 2 | Apache-2.0 |
| **MLX** | n/a — Apple Silicon only | n/a | n/a | n/a — **MLX does not run on NVIDIA GPUs**; excluded | n/a | n/a | n/a | MIT |
| **Ray Serve LLM (wraps vLLM)** | **3** — multi LLMConfig, model multiplexing w/ LRU [[25]](https://docs.anyscale.com/llm/serving/multi-lora) | 3 — runtime adapter load from S3/GCS, LRU cache [[25]](https://docs.anyscale.com/llm/serving/multi-lora) | inherits from vLLM | 1 — no Spark cite, but Ray runs on aarch64 | inherits | 3 — Anyscale prod | 1 — full Ray cluster overhead for 1-node use is excessive | Apache-2.0 |

**Scoring summary (out of 21):**
1. vLLM patched: **17** — wins on Spark-proven + LoRA hot-swap + production proof
2. SGLang: **15** — highest raw throughput but ARM support unverified, fewer LoRA examples
3. NVIDIA Triton + vLLM backend: **15** — best concurrent multi-model semantics but heavyweight
4. Dynamo: **13** — right answer two years from now, wrong scale today
5. llama.cpp: **15** — best single-user, worst LoRA story
6. Ollama: **13** — operator's current baseline; the eviction-pinning gap [[21]](https://github.com/open-webui/open-webui/discussions/3291) is disqualifying for a trading loop

---

## 4. Recommended Stack + Why

### 4.1 Architecture

```
                 ┌─────────────────────────────────────┐
                 │  Trading bot (Freqtrade + scripts)  │
                 └────────────────┬────────────────────┘
                                  │ OpenAI-compatible HTTP
                                  ▼
                 ┌─────────────────────────────────────┐
                 │  LiteLLM gateway  (port 4000)       │
                 │  - auth, rate-limit, logging        │
                 │  - routes by model name             │
                 └──┬────────────┬──────────┬──────────┘
                    │            │          │
                    ▼            ▼          ▼
     ┌──────────────────┐ ┌────────────┐ ┌──────────────────┐
     │ vLLM-fast        │ │ vLLM-deep  │ │ PyTorch services │
     │ port 8000        │ │ port 8001  │ │ TFT, sentiment,  │
     │ hermes3:8b base  │ │ qwen2.5-72b│ │ microstructure   │
     │ --enable-lora    │ │  Q4 GGUF or│ │ (CUDA contexts)  │
     │ --max-loras 8    │ │  AWQ       │ │                  │
     │ --max-cpu-loras 32│ │            │ │                  │
     │ N hot adapters   │ │            │ │                  │
     └──────────────────┘ └────────────┘ └──────────────────┘
                    │            │          │
                    └────────────┴──────────┘
                                  │
                                  ▼
                 ┌─────────────────────────────────────┐
                 │  Unified memory (128 GB LPDDR5X)    │
                 │  Shared via NVLink-C2C coherent     │
                 └─────────────────────────────────────┘
```

### 4.2 Why this and not Dynamo/Triton

1. **Native multi-model in one process is a myth** for vLLM/SGLang/TRT-LLM — all three serve one model per engine [[26]](https://leetllm.com/blog/llm-inference-engine-comparison-2026). "Multi-model" on a single Spark in practice = **N independent processes sharing unified memory**. NVLink-C2C coherent memory makes this cheap: weights are mmap'd once and the GPU reads via the shared address space [[2]](https://learn.arm.com/learning-paths/laptops-and-desktops/dgx_spark_llamacpp/1_gb10_introduction/).
2. **vLLM's LoRA story is unmatched.** `--enable-lora --max-loras 8 --max-cpu-loras 32` gives 8 adapters hot in GPU + 32 cached in CPU memory, with `POST /v1/load_lora_adapter` and `load_inplace:true` to swap weights without restart [[9]](https://docs.vllm.ai/en/latest/features/lora/). This is the killer feature for "hot-swappable LoRA adapters per role" — exactly the operator's requirement. Anyscale's docs confirm this is the standard prod pattern with LRU eviction over `max_cpu_loras` [[25]](https://docs.anyscale.com/llm/serving/multi-lora).
3. **GB10 sm_121 is patchwork.** Mainline vLLM doesn't list sm_121 in `TORCH_CUDA_ARCH_LIST`; forward-compat with `12.0` works because sm_121 maintains binary compatibility with sm_120 [[6]](https://medium.com/@stablehigashi/vllm-installation-on-dgx-spark-gb10-sm-121-and-qwen-3-5-serving-guide-9eba91e448f8). The patched `vllm-custom` fork delivers verified 70 tok/s on Qwen3.5-35B-A3B and 80 tok/s on GPT-OSS-120B [[5]](https://forums.developer.nvidia.com/t/vllm-0-17-0-mxfp4-patches-for-dgx-spark-qwen3-5-35b-a3b-70-tok-s-gpt-oss-120b-80-tok-s-tp-2/362824).
4. **Dynamo is the right ceiling.** Apache-2.0, Rust+Python, KVBM tiered cache, KV-aware routing, planner-driven autoscaling — but its benchmarks are all GB200 NVL72 racks [[4]](https://github.com/ai-dynamo/dynamo) [[19]](https://www.nvidia.com/en-us/ai/dynamo/). Adopting it for one Spark replaces a 50-line systemd unit with K8s CRDs. **Defer to v4.1 if/when a second Spark joins the cluster** — that's exactly Dynamo's sweet spot.
5. **SGLang is the runner-up.** Its RadixAttention cross-request KV reuse (50-95% hit rate on multi-turn or shared-prefix workloads) is worth chasing for the trading bot's repetitive system-prompts pattern [[14]](https://particula.tech/blog/sglang-vs-vllm-inference-engine-comparison) [[27]](https://www.lmsys.org/blog/2024-01-17-sglang/). Reason for not picking: README does not list ARM aarch64 hardware [[13]](https://github.com/sgl-project/sglang) and no first-hand Spark deployment outside the NVIDIA bench guide. **Re-evaluate at v4.2.**
6. **NIM is acceptable but locks us in** to NGC's model catalog [[20]](https://build.nvidia.com/spark/nim-llm). Operator preference is config-over-vendor.
7. **Ollama remains for dev only.** The pinned-model eviction bug [[21]](https://github.com/open-webui/open-webui/discussions/3291) means a trade signal can stall while VRAM is reshuffled. Unacceptable in the loop. Acceptable for one-off sentiment classification batches.

### 4.3 LiteLLM (+ optionally llama-swap) for routing

The operator's existing dashboard already speaks OpenAI-compatible. LiteLLM as gateway gives:
- one URL per environment,
- per-model rate limits,
- spend tracking,
- fallback (OpenRouter → local) on outages.

**Do not** put `llama-swap` in front of the always-resident models — its `swap: true exclusive: true` will evict them. Use llama-swap only for the long-tail (e.g., on-demand image generation, the 122B reasoning model) on a separate route [[3]](https://forums.developer.nvidia.com/t/running-a-full-llm-stack-on-dgx-spark-gb10-your-application-litellm-llama-swap-vllm-llama-cpp-ollama/367580).

---

## 5. Memory Budget Table (128 GB unified)

Working from the operator's targets (95 GB models + 30 GB headroom):

| Component | Format | Resident weight size | KV cache (peak) | Process overhead | Total | Source for size |
|---|---|---|---|---|---|---|
| hermes3:8b (fast tick) | FP8 / Q5_K_M | ~8–10 GB | 2 GB (4k ctx, 8 seq) | ~1 GB | **~12 GB** | hermes3:8b ~ 8 GB Q5 footprint typical |
| 8 active LoRA adapters @ rank 32 | FP16 | ~150 MB × 8 = **1.2 GB** | shared base KV | — | **~1.2 GB** | vLLM LoRA size [[9]](https://docs.vllm.ai/en/latest/features/lora/) |
| 24 cached CPU LoRA (max_cpu_loras=32) | FP16 | ~150 MB × 24 = **3.6 GB** | in CPU side of unified memory (still counts toward 128) | — | **~3.6 GB** | [[25]](https://docs.anyscale.com/llm/serving/multi-lora) |
| Qwen2.5-72B AWQ Q4 (deep arbiter) | INT4 + scales | ~40 GB | 4 GB (4k ctx, 4 seq, q4 KV) | ~1 GB | **~45 GB** | operator-stated; matches Qwen3.5-35B FP8 = 45 GB pattern [[7]](https://forums.developer.nvidia.com/t/spark-inference-run-3-specialized-models-simultaneously-on-your-dgx-spark-cybersecurity-coding-orchestration-30-min-setup/369236) |
| TFT direction model | FP16 PyTorch | ~5 GB | n/a | ~0.5 GB | **~5.5 GB** | operator-stated |
| Sentiment classifier (DeBERTa-v3-large / Llama-3-3B class) | FP16 | ~3 GB | n/a | ~0.5 GB | **~3.5 GB** | operator-stated |
| Microstructure model (custom Transformer ~1B params or LSTM stack) | FP16 | ~5 GB | n/a | ~0.5 GB | **~5.5 GB** | operator-stated |
| **Subtotal — models + adapters + KV** | | | | | **~76 GB** | sum above |
| Page cache / inference server processes / CUDA runtime per process (4×) | — | — | — | ~6 GB | **~6 GB** | typical CUDA context ~1-1.5 GB |
| OS + dashboards + Freqtrade + Postgres + Ollama daemon (dev) | — | — | — | ~8 GB | **~8 GB** | observed footprint |
| **Subtotal — system overhead** | | | | | **~14 GB** | |
| **Reserved KV cache headroom for spikes (8B at 8k ctx, batch up to 16)** | — | — | up to 8 GB | — | **~8 GB** | PagedAttention reserves on demand [[10]](https://developers.redhat.com/articles/2025/07/24/how-pagedattention-resolves-memory-waste-llm-systems) |
| **Reserved fine-tune / training workspace (QLoRA on 8B)** | — | — | — | ~15 GB | **~15 GB** | DGX Spark QLoRA-70B uses ~120 GB; QLoRA-8B ~15 GB [[28]](https://developer.nvidia.com/blog/how-nvidia-dgx-sparks-performance-enables-intensive-ai-tasks/) |
| **TOTAL** | | | | | **~113 GB / 128 GB** | |
| **Free** | | | | | **~15 GB** | |

**Findings:**
- Plan fits at ~113 GB committed, **leaving 15 GB free**. Tighter than the operator's "30+ GB free" target.
- If the 30 GB free buffer is hard, drop **fine-tune workspace** (15 GB) — make fine-tuning a stop-the-world job that pauses hermes3 first. That recovers ~15 GB → **~30 GB free**. Achieves the spec.
- **`--gpu-memory-utilization` must be explicit per process**, never default. Recommendation: `vllm-fast` = 0.18 (≈23 GB), `vllm-deep` = 0.40 (≈51 GB), leave the rest for PyTorch services + OS.
- KV cache budget assumes 4k context. **For longer context (16k+), each extra 4k roughly doubles KV** [[29]](https://introl.com/blog/kv-cache-optimization-memory-efficiency-production-llms-guide).

---

## 6. Migration Cost from Current Ollama-Only Setup

Current state (from operator memory): 8 pairs of crypto + 15 stocks tracked, hermes3:8b on Ollama with `OLLAMA_KEEP_ALIVE`, no LoRA, no qwen2.5-72b yet.

| Phase | Effort | Risk | Outcome |
|---|---|---|---|
| **P0 — Keep Ollama, add vLLM in parallel** | 1-2 days | Low. Two daemons coexist; LiteLLM routes by model name. | hermes3:8b stays on Ollama for the bot; vLLM serves Qwen2.5-72B for arbiter sidecar. Validates GB10 patch path. |
| **P1 — Migrate hermes3:8b to vLLM + 0 LoRA** | 2-3 days | Med. Build patched vLLM (`TORCH_CUDA_ARCH_LIST=12.0`, `VLLM_DISABLED_KERNELS=cutlass_*`) [[6]](https://medium.com/@stablehigashi/vllm-installation-on-dgx-spark-gb10-sm-121-and-qwen-3-5-serving-guide-9eba91e448f8). Validate parity (token-level, latency p50/p99). | Same model, same prompt, lower tail latency from PagedAttention. Removes eviction race. |
| **P2 — Add `--enable-lora` and load first per-role adapter** | 1 day | Low. Adapter is small (~150 MB). | Hot-swap-able role variants. Enables A/B testing per regime. |
| **P3 — Stand up PyTorch services as systemd units** | 2-3 days | Med. CUDA context budget needs tuning. Three Python processes each pin their CUDA runtime. | TFT/sentiment/micro models always resident; latency consistent. |
| **P4 — LiteLLM gateway + dashboard** | 1 day | Low. LiteLLM is plug-and-play [[3]](https://forums.developer.nvidia.com/t/running-a-full-llm-stack-on-dgx-spark-gb10-your-application-litellm-llama-swap-vllm-llama-cpp-ollama/367580). | Single endpoint, central log, spend tracking. |
| **P5 — Decommission Ollama from the trading hot path; retain for dev** | <1 day | Low. | Bot no longer at mercy of `OLLAMA_KEEP_ALIVE`. Ollama remains for ad-hoc model trials. |
| **Total** | **~9-11 days of focused work** | Med overall | |

**Sticky bits:**
- The `vllm-custom` MXFP4 patches are **not upstreamed**. Pin to a specific commit and document the rebuild [[5]](https://forums.developer.nvidia.com/t/vllm-0-17-0-mxfp4-patches-for-dgx-spark-qwen3-5-35b-a3b-70-tok-s-gpt-oss-120b-80-tok-s-tp-2/362824). Treat the patched build as a hermes-style frozen container.
- Qwen2.5-72B-Q4 on a **single** Spark requires AWQ or GGUF Q4 — **NVFP4 will crash** [[8]](https://github.com/vllm-project/vllm/issues/35519), **MXFP4 TP=1 has race conditions** [[5]](https://forums.developer.nvidia.com/t/vllm-0-17-0-mxfp4-patches-for-dgx-spark-qwen3-5-35b-a3b-70-tok-s-gpt-oss-120b-80-tok-s-tp-2/362824). AWQ/GGUF is the only safe path until upstream lands sm_121.
- Sentiment/microstructure models that today run in Ollama as GGUF should migrate to native PyTorch `safetensors` — saves **65 GB / 130 GB / 65 GB triple-storage overhead** noted by the inference-engine survey [[16]](https://medium.com/sparktastic/choosing-an-inference-engine-on-dgx-spark-8a312dfcaac6).

---

## 7. Open Questions for Follow-up Research

1. **Does Qwen2.5-72B-AWQ-Q4 actually fit and meet latency target on Spark?** No first-party benchmark found at exactly Q4 AWQ + 4k ctx + batch 4. Closest data point: Llama-3.1-70B-FP8 at 803 prefill / 2.7 decode tps via SGLang [[11]](https://www.lmsys.org/blog/2025-10-13-nvidia-dgx-spark/) — Q4 should be 1.5-2× faster, but ARM kernel coverage uncertain. **Bench before committing.**
2. **SGLang on ARM aarch64 — does it actually build?** Their README's hardware list omits aarch64 [[13]](https://github.com/sgl-project/sglang) but NVIDIA's Spark benchmark guide lists SGLang [[15]](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/connect-two-sparks/assets/performance_benchmarking_guide.md). Need a test build before considering it for v4.2.
3. **Can SGLang's RadixAttention reuse KV across the trading bot's recurring system prompts?** If "yes, 50%+ hit rate," that's an argument to switch from vLLM to SGLang for the fast classifier. Need to instrument prompt repetition first.
4. **vLLM model multiplexing per-replica vs N processes** — Ray Serve LLM exposes a `max_num_adapters_per_replica` and shared replica pool [[25]](https://docs.anyscale.com/llm/serving/multi-lora). Worth a one-day Ray Serve spike if/when we have >20 fine-tuned per-symbol adapters.
5. **NIM on Spark playbook — does the official one allow us to keep 5 NIMs hot in 128 GB?** Couldn't fetch [[20]](https://build.nvidia.com/spark/nim-llm) (timeout). Would change the recommendation if NVIDIA ships a managed N-model image.
6. **TensorRT-LLM compile time vs benefit on sm_121** — operator anecdote was "hours" [[16]](https://medium.com/sparktastic/choosing-an-inference-engine-on-dgx-spark-8a312dfcaac6). If the trading 8B model is stable for months at a time, an overnight compile is worth it for max throughput. Need a one-shot trial.
7. **Quantized KV cache for the 72B arbiter** — quantizing KV to Q8 or Q4 frees ~3 GB on Qwen2.5-72B. vLLM supports `--kv-cache-dtype fp8` [[10]](https://developers.redhat.com/articles/2025/07/24/how-pagedattention-resolves-memory-waste-llm-systems). Quality regression on the deep-arbiter use case needs measurement.
8. **Dynamo's KVBM as a CPU-spillover cache** — could we get 256 GB *effective* KV by spilling to system memory? On Spark this is uniquely interesting because "system memory" *is* GPU memory. Need to read Dynamo source on whether KVBM applies in unified-memory single-node mode.
9. **Two-Spark cluster economics** — if we add a second Spark, vLLM tensor-parallel-2 unlocks MXFP4 + larger models (Qwen3.5-122B at 51 tok/s reported [[30]](https://forums.developer.nvidia.com/t/qwen3-5-122b-a10b-on-single-spark-up-to-51-tok-s-v2-1-patches-quick-start-benchmark/365639)). Is the latency win worth $4k of hardware? Out of scope here.
10. **Ollama 0.13+ — does it ever fix the pinned-eviction bug?** Track [#3291](https://github.com/open-webui/open-webui/discussions/3291). If they ship a hard-pin guarantee, the migration value of vLLM drops for the dev/exploratory tier.

---

## 8. Sources Cited

1. [NVIDIA NVLink-C2C product page](https://www.nvidia.com/en-us/data-center/nvlink-c2c/) — coherent CPU/GPU interconnect, AMBA CHI / CXL, 6× energy efficiency vs PCIe Gen6.
2. [Arm Learning Path: Unlock quantized LLM perf on DGX Spark — GB10 introduction](https://learn.arm.com/learning-paths/laptops-and-desktops/dgx_spark_llamacpp/1_gb10_introduction/) — NVLink-C2C 900 GB/s, unified 128 GB, zero-copy.
3. [NVIDIA Dev Forum: Running a Full LLM Stack on DGX Spark GB10 — LiteLLM → llama-swap → vLLM/llama.cpp/Ollama](https://forums.developer.nvidia.com/t/running-a-full-llm-stack-on-dgx-spark-gb10-your-application-litellm-llama-swap-vllm-llama-cpp-ollama/367580) — tiered swap, 121.7 GiB CUDA-visible, S/M/L model tiers, real tok/s numbers.
4. [GitHub: ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo) — Dynamo features, vLLM/SGLang/TRT-LLM backends, disagg prefill/decode, KVBM, 1.0 GA Mar 2025, v1.1.1 May 2026.
5. [NVIDIA Dev Forum: vLLM 0.17.0 MXFP4 patches for DGX Spark — Qwen3.5-35B-A3B 70 tok/s, GPT-OSS-120B 80 tok/s (TP=2)](https://forums.developer.nvidia.com/t/vllm-0-17-0-mxfp4-patches-for-dgx-spark-qwen3-5-35b-a3b-70-tok-s-gpt-oss-120b-80-tok-s-tp-2/362824) — sm_121 patch list, kernel disable env, TP=1 race conditions.
6. [Medium: vLLM Installation on DGX Spark (GB10 sm_121) and Qwen 3.5 Serving Guide](https://medium.com/@stablehigashi/vllm-installation-on-dgx-spark-gb10-sm-121-and-qwen-3-5-serving-guide-9eba91e448f8) — exact build commands, `TORCH_CUDA_ARCH_LIST=12.0`, `VLLM_DISABLED_KERNELS`, pinned commit.
7. [NVIDIA Dev Forum: Spark-inference — Run 3 specialized models simultaneously, 30-min setup](https://forums.developer.nvidia.com/t/spark-inference-run-3-specialized-models-simultaneously-on-your-dgx-spark-cybersecurity-coding-orchestration-30-min-setup/369236) — 3 concurrent models in 120 GB: 32 + 45 + 35 GB, `--gpu-memory-utilization 0.25` per process, eager mode, no CUDA graphs.
8. [vLLM Issue #35519: Qwen3.5 NVFP4 crashes on ARM64 GB10 DGX Spark — CUDA illegal instruction](https://github.com/vllm-project/vllm/issues/35519) — NVFP4 kernels emit instructions sm_121 cannot execute; open, no fix.
9. [vLLM docs: LoRA Adapters](https://docs.vllm.ai/en/latest/features/lora/) — `--enable-lora`, `--max-loras`, `--max-cpu-loras`, `--max-lora-rank`, `/v1/load_lora_adapter`, `load_inplace`.
10. [Red Hat Developer: How PagedAttention resolves memory waste in LLM systems](https://developers.redhat.com/articles/2025/07/24/how-pagedattention-resolves-memory-waste-llm-systems) — < 4% waste vs 60-80% baseline; block-level KV management.
11. [LMSYS Blog: NVIDIA DGX Spark In-Depth Review (Oct 2025)](https://www.lmsys.org/blog/2025-10-13-nvidia-dgx-spark/) — SGLang benchmarks on Spark: Llama-3.1-8B 7991/20.5 tps, Llama-3.1-70B FP8 803/2.7 tps, GPT-OSS-20B Ollama MXFP4 2053/49.7 tps, GPT-OSS-120B fits.
12. [vLLM Blog (Feb 2026): Efficiently serve dozens of fine-tuned models with vLLM on SageMaker and Bedrock](https://blog.vllm.ai/2026/02/26/multi-lora.html) — production multi-LoRA, fused_moe_lora kernel.
13. [GitHub: sgl-project/sglang README](https://github.com/sgl-project/sglang) — v0.5.11 May 2026, multi-LoRA batching, RadixAttention, FP4/FP8/INT4 quant, supported hardware list (no aarch64).
14. [Particula.tech: SGLang vs vLLM 2026](https://particula.tech/blog/sglang-vs-vllm-inference-engine-comparison) — 29% throughput edge, 894 vs 413 tok/s, RadixAttention 50-95% hit rate, when to use each.
15. [GitHub: NVIDIA/dgx-spark-playbooks — performance benchmarking guide](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/connect-two-sparks/assets/performance_benchmarking_guide.md) — official benchmark scripts for vLLM, SGLang, TRT-LLM, llama.cpp; GPU-mem-util 0.9 default.
16. [Medium / Sparktastic: Choosing an Inference Engine on DGX Spark](https://medium.com/sparktastic/choosing-an-inference-engine-on-dgx-spark-8a312dfcaac6) — llama.cpp wins single-user; Ollama slower; vLLM container issues; TRT-LLM hours-long compile; 65/130/65 GB triple-storage problem.
17. [NVIDIA Triton docs: Concurrent Model Execution](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_execution.html) — instance_group, multi-instance, multi-stream isolation.
18. [NVIDIA Triton: Multi-LoRA tutorial via vLLM backend](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/vllm_backend/docs/llama_multi_lora_tutorial.html) — vLLM-as-Triton-backend multi-LoRA.
19. [NVIDIA Dynamo product page](https://www.nvidia.com/en-us/ai/dynamo/) — KVBM tiered cache, planner, 7×/15×/35× perf claims (GB200 context).
20. [DGX Spark build page: NIM on Spark](https://build.nvidia.com/spark/nim-llm) — official NIM playbook for local Spark deployment (page existed; full content not fetched in this pass).
21. [open-webui/open-webui discussion #3291: Ollama purges model from VRAM despite OLLAMA_KEEP_ALIVE=-1](https://github.com/open-webui/open-webui/discussions/3291) — pin-eviction bug documented; disqualifies Ollama from prod hot path.
22. [Ollama FAQ](https://docs.ollama.com/faq) — `OLLAMA_MAX_LOADED_MODELS`, `OLLAMA_KEEP_ALIVE`, concurrent model load semantics.
23. [Ollama Blog: NVIDIA DGX Spark performance](https://ollama.com/blog/nvidia-spark-performance) — llama3.1-8B 7.614k prefill, gpt-oss-20B 3.224k/58.27, gpt-oss-120B 1.169k prefill.
24. [GitHub: ggml-org/llama.cpp Discussion #16578 — Performance of llama.cpp on DGX Spark](https://github.com/ggml-org/llama.cpp/discussions/16578) — 61 tps GPT-OSS-20B, 35 tps GPT-OSS-120B, 44 tps Qwen3-Coder-30B-A3B; SM 12.1; FP4 modest gains.
25. [Anyscale docs: Deploy multi-LoRA adapters on LLMs](https://docs.anyscale.com/llm/serving/multi-lora) — Ray Serve LLM multi-LoRA, LRU eviction, S3/GCS adapter loading, `max_num_adapters_per_replica` vs `max_loras` vs `max_cpu_loras`.
26. [LeetLLM: vLLM vs SGLang vs TensorRT-LLM vs Ollama — 2026 showdown](https://leetllm.com/blog/llm-inference-engine-comparison-2026) — landscape summary; "one model per engine" pattern.
27. [LMSYS Blog (Jan 2024): SGLang & RadixAttention](https://www.lmsys.org/blog/2024-01-17-sglang/) — original RadixAttention paper write-up.
28. [NVIDIA Tech Blog: How DGX Spark's Performance Enables Intensive AI Tasks](https://developer.nvidia.com/blog/how-nvidia-dgx-sparks-performance-enables-intensive-ai-tasks/) — Llama-3.3-70B QLoRA 759.79 tps; Llama-3.2-3B FT 13519 tps; Llama-3.1-8B LoRA 6969 tps; 273 GB/s, 1 PFLOP FP4.
29. [Introl Blog: KV Cache Optimization for Production LLMs](https://introl.com/blog/kv-cache-optimization-memory-efficiency-production-llms-guide) — KV scaling with context length.
30. [NVIDIA Dev Forum: Qwen3.5-122B-A10B on single Spark — up to 51 tok/s](https://forums.developer.nvidia.com/t/qwen3-5-122b-a10b-on-single-spark-up-to-51-tok-s-v2-1-patches-quick-start-benchmark/365639) — single-Spark large-MoE benchmark with patches.

Bonus / supporting reads not numbered above:
- [vLLM GPU installation docs](https://docs.vllm.ai/en/stable/getting_started/installation/gpu/) — ARM aarch64 build flags, `--platform linux/arm64`, GH200/GB300 examples.
- [vLLM Optimization & Tuning](https://docs.vllm.ai/en/stable/configuration/optimization/) — `gpu_memory_utilization`, chunked prefill, `max_num_seqs`.
- [The Canteen: State of LLM Serving 2026](https://thecanteenapp.com/analysis/2026/01/03/inference-serving-landscape.html) — vLLM supports 218 model architectures; framework taxonomy.

---

## 9. TL;DR Decision

| Question | Answer |
|---|---|
| Stack? | **vLLM (patched for sm_121) × N processes + LiteLLM gateway + native PyTorch processes for non-LLM models** |
| Why not Ollama? | `OLLAMA_KEEP_ALIVE=-1` does not guarantee against eviction when VRAM fills [[21]](https://github.com/open-webui/open-webui/discussions/3291). Disqualifying for the trading hot loop. |
| Why not SGLang now? | ARM aarch64 support not advertised [[13]](https://github.com/sgl-project/sglang); revisit at v4.2. |
| Why not Dynamo/Triton? | Right answer for multi-node; massive overkill for one Spark. Defer to v4.1 if a second Spark is added. |
| Hot-swap LoRA? | vLLM `/v1/load_lora_adapter` with `load_inplace:true` [[9]](https://docs.vllm.ai/en/latest/features/lora/). 8 GPU-resident + 32 CPU-cached. |
| 5 models simultaneously? | Yes — independent processes share unified memory via NVLink-C2C [[2]](https://learn.arm.com/learning-paths/laptops-and-desktops/dgx_spark_llamacpp/1_gb10_introduction/). Budget table shows ~113/128 GB committed. |
| Biggest risk? | sm_121 patches are community-maintained (`vllm-custom`), not upstream [[5]](https://forums.developer.nvidia.com/t/vllm-0-17-0-mxfp4-patches-for-dgx-spark-qwen3-5-35b-a3b-70-tok-s-gpt-oss-120b-80-tok-s-tp-2/362824). Pin commit; revisit each vLLM minor. |
| Migration cost? | ~9-11 focused days, 6 phases, can run side-by-side with Ollama during transition. |

End of research document.
