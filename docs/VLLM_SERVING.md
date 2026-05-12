# vLLM Multi-LoRA Serving — Operator Runbook

> Branch: `stage/vllm-multi-lora-serving` · Status: profile-gated, OFF by
> default. Activate explicitly per the steps below.

## Why two serving planes

| Plane          | Port  | Roles served                         | Adapters? |
| -------------- | ----- | ------------------------------------ | --------- |
| **Ollama**     | 11434 | JSON-only (regime tagger, indicator selector) — sub-second | No  |
| **vLLM 0.5+**  | 8090  | Prose (Reflector, Bull, Bear, Arbiter) | Yes — hot-swap per request |

Ollama doesn't support LoRA hot-swap. vLLM 0.5+ does — ~5-10 ms PCIe-fetch
per adapter, so the four prose roles can each ride their own
ModelForge-trained head on the same `qwen3:30b` base without 4× the VRAM.

Ollama stays because:
1. JSON-mode latency on `hermes3:8b` is already where we want it.
2. vLLM cold-start is slow (3-5 min); a single warm Ollama is a reliable
   fallback target.
3. The non-prose roles don't benefit from custom adapters.

## DGX VRAM math

```
qwen3:30b base (fp8)              ~22 GB
4-6 × LoRA adapters (rank 32)      ~0.9 GB  (~150 MB each)
KV cache (max-model-len 8192)      ~6 GB    (gpu_memory_utilization 0.45)
                                  ─────────
                                   ~29 GB resident
```

Well under DGX Spark's 128 GB unified pool. Ollama (hermes3:8b ~6 GB,
hermes3:70b ~42 GB) runs in parallel on the remaining capacity.

## Bootstrap (one command)

```bash
bash scripts/bootstrap_vllm.sh
```

Idempotent. On first run, expect 3-5 min while HF downloads the weights
(`Qwen/Qwen3-30B-A3B-Instruct-2507`, ~30 GB). On subsequent runs, exits
in <1 s if the container is already healthy.

Environment overrides:

| Variable                | Default                                     | Meaning                                          |
| ----------------------- | ------------------------------------------- | ------------------------------------------------ |
| `HF_TOKEN`              | empty                                       | Required only if the HF model is gated.          |
| `VLLM_HF_MODEL`         | `Qwen/Qwen3-30B-A3B-Instruct-2507`          | Upstream HF id vLLM pulls.                       |
| `VLLM_BASE_MODEL`       | `qwen3:30b`                                 | Name the trading-bot calls it (must match `model_tiers.json`). |
| `VLLM_QUANTIZATION`     | `fp8`                                       | `fp8`, `awq`, `fp4` etc.                         |
| `VLLM_GPU_MEM_UTIL`     | `0.45`                                      | Fraction of GPU mem vLLM may use.                |
| `VLLM_MAX_MODEL_LEN`    | `8192`                                      | Max context length.                              |
| `VLLM_BOOT_TIMEOUT_S`   | `600`                                       | How long the bootstrap waits for /health.        |
| `VLLM_BASE_URL`         | `http://localhost:8090`                     | What the trading-bot client posts to.            |
| `VLLM_TIMEOUT_S`        | `180`                                       | Per-request timeout.                             |

## Verify

```bash
# Health check
curl -fsS http://127.0.0.1:8090/health

# Should list base + 4 preloaded adapters
curl -fsS http://127.0.0.1:8090/v1/models | jq

# Smoke test: send a chat call routed through the bull adapter
curl -fsS http://127.0.0.1:8090/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "bull",
    "messages": [
      {"role": "system", "content": "You are a bull analyst."},
      {"role": "user", "content": "Make a one-line bull case for AAPL."}
    ],
    "max_tokens": 64
  }' | jq
```

## Adding a new adapter at runtime (no restart)

ModelForge writes promoted champions to `./data/lora-adapters/<run_id>/`
on the host (mounted read-only at `/lora` inside the container). After a
promotion, register the new path with vLLM:

```bash
curl -fsS -X POST http://127.0.0.1:8090/v1/load_lora_adapter \
  -H 'Content-Type: application/json' \
  -d '{
    "lora_name": "reflector-2026-05-12",
    "lora_path": "/lora/reflector-2026-05-12"
  }'
```

Or from Python:

```python
from shark.llm.vllm_client import register_adapter
register_adapter("reflector-2026-05-12", "/lora/reflector-2026-05-12")
```

To start routing the trading-bot's `trading-reflector` role to the new
champion, point the symlink:

```bash
ln -sfn ./reflector-2026-05-12 ./data/lora-adapters/reflector-current
```

The adapter `reflector` is preloaded against this symlink path, so the
swap is picked up automatically on the next call.

## Fallback behavior (degraded mode is fine)

When vLLM is unreachable (cold-starting, OOM, crashed, network hiccup),
`shark.llm.client.chat_by_role()` catches `VLLMUnavailableError` and
transparently falls back to Ollama with the base model and **no
adapter**. Trading does not stop.

Symptoms in the log:
```
WARNING shark.llm.client: vLLM unavailable for role=trading-bull
  (vLLM returned 503: …) — falling back to Ollama base model
```

Callers don't need to handle this. The contract is the same string-tuple
shape (`content, usage, served_model`) — only the served model name
changes (no adapter suffix).

## Port collision warning

`freqtrade-nfi` (profile `nfi`, OFF by default) and vLLM (profile `vllm`)
both bind to host `127.0.0.1:8090`. Run only one at a time:

```bash
# Switch from NFI → vLLM
docker compose --profile nfi stop freqtrade-nfi
bash scripts/bootstrap_vllm.sh

# Switch back
docker compose --profile vllm stop vllm
docker compose --profile nfi up -d freqtrade-nfi
```

If both profiles are needed simultaneously, change one host-side port —
`freqtrade-nfi` is the easier mover because the trading-bot doesn't
target it from Python (operator-only REST API).

## Cold-start expectations

* **First boot**: 3-5 min (HF download + weight load + JIT compile).
  The bootstrap script's default 600 s timeout handles this.
* **Restart with warm cache**: 30-90 s (weight load only).
* **First request after boot**: 1-3 s (model-load amortisation).
* **Steady-state**: prose role ~500 ms-2 s depending on `max_tokens`.
  Adapter swap between consecutive calls: 5-10 ms.

## Stopping vLLM

```bash
docker compose --profile vllm stop vllm
# or, full removal:
docker compose --profile vllm down vllm
```

The HF cache survives via the `vllm_cache` named volume.

## Training-window throttle (planned)

Sunday 02:00 ET cron will shrink vLLM's KV cache (`VLLM_GPU_MEM_UTIL` 
lowered to ~0.20) while ModelForge runs the weekly evolution. Document
TBD once the cron lands; for now, manual:

```bash
# Pause vLLM to free VRAM during training
docker compose --profile vllm stop vllm
# … run training …
bash scripts/bootstrap_vllm.sh
```

## Test command

```bash
cd stocks
pytest tests/test_vllm_client.py -v
```
