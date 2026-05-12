# HANDOFF — stage/vllm-multi-lora-serving

**Status**: code complete, profile-gated, DO NOT merge. Operator runs
`scripts/bootstrap_vllm.sh` after review.

## What this branch ships

Two-plane LLM serving on the DGX Spark:

```
Ollama :11434  (existing — UNCHANGED)
  └── hermes3:8b, hermes3:70b, qwen3:30b
  └── serves JSON-only roles (regime tagger, indicator selector)
  └── NO hot-swap, NO adapter management
  └── Also: vLLM-fallback target when vLLM is unreachable

vLLM   :8090   (NEW — profile-gated OFF)
  └── qwen3:30b base + 4 preloaded LoRA adapters
       reflector / bull / bear / arbiter
  └── /v1/chat/completions selects adapter via the `model` field
  └── /v1/load_lora_adapter registers new adapters at runtime
  └── serves prose roles
```

## Files changed / added

| Path | Action | Purpose |
| ---- | ------ | ------- |
| `stocks/shark/llm/vllm_client.py`       | NEW    | OpenAI-compatible client with adapter selection; raises `VLLMUnavailableError` on 5xx/timeout. |
| `stocks/shark/llm/client.py`            | MODIFY | Registers `vllm` provider, adds `resolve_role_route()` + `chat_by_role()` that reads `model_tiers.json` and falls back to Ollama on vLLM error. |
| `stocks/shark/llm/__init__.py`          | MODIFY | Re-exports `chat_by_role`, `resolve_role_route`. |
| `stocks/shark/model_tiers.json`         | MODIFY | Adds a `"routing"` block with per-role backend + adapter map. Existing flat keys untouched, so `shark.graph._load_model_tiers()` keeps working. |
| `stocks/tests/test_vllm_client.py`      | NEW    | Mocks `requests.post`; covers request shape, response parsing, 5xx fallback, adapter swap, role router, env override. |
| `scripts/bootstrap_vllm.sh`             | NEW    | Idempotent: noop if healthy, else `docker compose --profile vllm up -d vllm`, waits up to 600 s for `/health`, then probes `/v1/models`. |
| `docker-compose.yml`                    | MODIFY | Adds `vllm` service (profile `vllm`), `vllm_cache` named volume. |
| `docs/VLLM_SERVING.md`                  | NEW    | Operator runbook: bootstrap, verify, register adapter, fallback, cold-start, port collision. |

Nothing in the running freqtrade or dashboard paths was touched.

## How to bootstrap (one command)

```bash
bash scripts/bootstrap_vllm.sh
```

First boot: 3-5 min (pulls ~30 GB Qwen weights). Subsequent boots: <1 s.

## How to verify

```bash
curl -fsS http://127.0.0.1:8090/health
curl -fsS http://127.0.0.1:8090/v1/models | jq
pytest stocks/tests/test_vllm_client.py -v
```

Expected `/v1/models` payload: base `qwen3:30b` + four adapters
`reflector`, `bull`, `bear`, `arbiter`.

## How to add a new adapter at runtime

```bash
curl -X POST http://127.0.0.1:8090/v1/load_lora_adapter \
  -H 'Content-Type: application/json' \
  -d '{"lora_name": "reflector-2026-05-12",
       "lora_path": "/lora/reflector-2026-05-12"}'
```

Or `from shark.llm.vllm_client import register_adapter`. No vLLM
restart required.

## DGX VRAM math

```
qwen3:30b base (fp8)         ~22 GB
4-6 LoRA adapters (rank 32)   ~0.9 GB
KV cache @ 0.45 gpu-mem-util  ~6 GB
                              ──────
                               ~29 GB resident   (out of 128 GB unified)
```

Ollama's 70b coexists comfortably.

## Cold-start behavior

The first request after `up -d vllm` is 1-3 s slower while weights are
loaded into VRAM. The bootstrap script's `/health` wait absorbs this
before returning; the trading-bot only sees post-warm latency.

## Fallback behavior — degraded mode is fine

When vLLM is unavailable (cold-starting, OOM, crashed, 5xx, timeout),
`chat_by_role()` catches `VLLMUnavailableError` and silently routes the
prose call to Ollama with the base `qwen3:30b` and **no adapter**.

```
WARNING shark.llm.client: vLLM unavailable for role=trading-bull
  (vLLM returned 503: …) — falling back to Ollama base model
```

Trading does NOT stop. The caller sees the same `(content, usage,
served_model)` tuple it always sees — just with a non-adapter model
name. This is intentional: on the operator's "$2k/4w P&L target"
timeline we cannot afford prose roles to halt the pipeline because vLLM
took 30 s to recover from a JIT recompile.

## Operator decisions baked in (vs the original spec)

1. **HF model id**: spec said `--model qwen3:30b`. The vLLM/OpenAI image
   wants an HF id, not an Ollama tag, so the compose `command` passes
   `--model=Qwen/Qwen3-30B-A3B-Instruct-2507` and
   `--served-model-name=qwen3:30b` (env-overridable via `VLLM_HF_MODEL`
   and `VLLM_BASE_MODEL`). The trading-bot still calls it
   `qwen3:30b`. The Ollama-imported model is NOT shared via volume mount
   — vLLM caches HF weights independently in the `vllm_cache` volume,
   because Ollama stores GGUF blobs (incompatible with vLLM's PyTorch
   loader).

2. **Port 8090 collision**: spec asked for 8090, but `freqtrade-nfi`
   (profile `nfi`, OFF by default) already binds 127.0.0.1:8090. Kept
   8090 here because both services are profile-gated and only one runs
   at a time in normal operation. Documented in
   `docs/VLLM_SERVING.md` ("Port collision warning") with the
   stop/start commands.

3. **Quantization**: spec said `--quantization fp4`. Defaulted to `fp8`
   (`VLLM_QUANTIZATION=fp8`) because fp4 requires a custom vLLM build
   on Blackwell (`haven-jeon/unsloth-vllm-gb10`) and the operator hasn't
   greenlit that path yet. Env-overridable in one place when ready.

4. **Adapter selection mechanism**: vLLM 0.5+'s canonical mechanism is
   to pass the adapter name as the OpenAI `model` field, not
   `extra_body.adapter_name`. The client passes BOTH for forward
   compatibility — older builds honour `extra_body`, newer ones honour
   `model`. Either alone is sufficient.

5. **Bootstrap port note**: when starting, the script warns if
   `freqtrade-nfi` is up; it does NOT auto-stop it. Operator decides.

## Cron note (planned)

Per the 4-week plan, Sunday 02:00 ET will shrink vLLM's KV cache (or
stop vLLM entirely) during the ModelForge training window to free VRAM
for adapter evolution. The cron itself is out of scope for this branch
— current degraded-mode path (vLLM down → Ollama base) means it can be
landed as a simple `docker compose stop vllm` without code changes.

## Test command

```bash
cd stocks
pytest tests/test_vllm_client.py -v
```

All tests use `unittest.mock.patch` against `shark.llm.vllm_client.requests`
— no live vLLM server required.

## Open follow-ups (NOT in this branch)

- Wire `chat_by_role` into the actual prose-role call sites (Reflector,
  Bull, Bear, Arbiter agents). Right now those still use `chat_json` /
  `chat_structured`. This branch ships the routing primitive; a
  follow-up branch flips the call sites once the operator has confirmed
  vLLM behaviour on the live DGX.
- Slack alert on vLLM-fallback (mirrors the existing Anthropic-fallback
  alert in `client.py:_maybe_alert_fallback_active`). Useful to know
  when adapters silently aren't serving.
- Tracker dashboard card: surface "% prose calls served by vLLM vs
  Ollama-fallback" on the existing LLM-stats panel.
