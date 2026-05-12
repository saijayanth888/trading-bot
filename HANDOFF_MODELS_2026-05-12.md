# quanta_core/models — Build Agent Handoff

**Branch:** `feat/v4-build-models` (off `main`, never pushed).
**Date:** 2026-05-12.
**Scope:** v4 model layer — Ollama-backed registry + load-on-demand TFT
port that DROPS every FreqAI dependency and the legacy `tft_pickle` shim.

---

## What landed

```
quanta_core/
├── pyproject.toml                 # ruff + mypy --strict + pytest config
├── src/quanta_core/
│   ├── __init__.py                # __version__ only
│   ├── py.typed                   # PEP 561 marker
│   └── models/
│       ├── __init__.py            # explicit __all__ export surface
│       ├── registry.py            # ModelRegistry + ModelHandle + LRU eviction
│       ├── ollama_client.py       # OllamaClient + OllamaResponse + 503-retry
│       ├── tft_architecture.py    # PORTED verbatim from user_data/freqaimodels/
│       ├── tft.py                 # PORTED — standalone PyTorch, safetensors save
│       ├── sentiment.py           # STUB — hermes3:8b prompt-routed (see TODO)
│       └── microstructure.py      # STUB — order-book imbalance model (deferred)
└── tests/
    ├── conftest.py
    ├── test_registry.py
    ├── test_ollama_client.py
    ├── test_tft.py                # includes validate_artifact coverage suite
    └── test_stubs.py
```

## Verification (all gates green, 2026-05-12)

| Gate | Command | Result |
|---|---|---|
| Lint | `ruff check src tests` | All checks passed |
| Format | `ruff format --check src tests` | 14 files already formatted |
| Types | `mypy --strict src/quanta_core` | Success — no issues in 8 source files |
| Tests | `pytest tests` | **78 passed** in ~5 s |
| Coverage (overall) | `pytest --cov=quanta_core` | **94%** |
| Coverage on `validate_artifact` | analysed via `coverage.analysis2()` | **100% line coverage (50 executable lines, 0 missed)** |

Coverage per module:

| File | Stmts | Miss | Cover |
|---|---|---|---|
| `models/__init__.py` | 7 | 0 | 100% |
| `models/microstructure.py` | 13 | 0 | 100% |
| `models/ollama_client.py` | 133 | 8 | **91%** |
| `models/registry.py` | 120 | 1 | **97%** |
| `models/sentiment.py` | 14 | 0 | 100% |
| `models/tft.py` | 325 | 21 | **91%** |
| `models/tft_architecture.py` | 147 | 1 | 97% |

Misses are concentrated in CUDA/AMP-only branches (lines 347-356 in `tft.py`
require GPU; cannot run on test hosts), torch.compile path (CUDA-only),
and a few exception-rescue paths in the training loop. The brief's two
hard gates — overall ≥ 85% and `validate_artifact` ≥ 95% — are both met.

## Port % from `user_data/freqaimodels/TFTModel.py` (829 lines)

| Component | Status | Notes |
|---|---|---|
| Architecture (`tft_architecture.py`, 363 lines) | **100% ported, verbatim** | Co-located at `quanta_core/models/tft_architecture.py`. Public surface unchanged; mypy casts added on `nn.Module` forward returns. |
| `TFTConfig` (NEW dataclass) | n/a (replaces FreqAI's config-dict) | Adds explicit validation: rejects bad dropout, mismatched class_names, hidden_size not divisible by n_heads, etc. |
| `fit()` / training loop | **~85% ported** | Kept: AdamW + cosine warmup + AMP path + sliding windows + early-stop on val-Sharpe. Dropped: per-pair `_resume_checkpoint` (file-IO heavy, never exercised in tests; safe to re-add when ModelForge needs it). |
| `predict()` / `predict_proba()` | **100% ported** | Same windowed slide + softmax + directional confidence (Guo 2017). Returns numpy arrays instead of a DataFrame so the caller (live engine, registry predictor) can wire to any consumer. |
| `_validate_sharpe`, `_sliding_windows`, `_class_to_target` | **100% ported** | Logic identical; signatures cleaned up to use `NDArray[np.float32]`/`NDArray[np.int64]` instead of pandas DataFrames. |
| Save/load — `TFTTrainerWrapper`, `torch.save`, legacy shim | **DELETED** | Replaced by `TFTModel.save()` → `safetensors.torch.save_file` + `metadata.json`. Replaced by `TFTModel.load()` → `safetensors.torch.load_file` after `validate_artifact()`. Zero stdlib serialiser, zero `torch.save`, zero `sys.modules["TFTModel"]` proxy. |
| `validate_model_zip` (legacy stub bug) | **REPLACED by `validate_artifact()`** | Legacy returned `True` for empty dir. New raises `TFTValidationError` on: missing dir, wrong type, missing file, bad JSON, version mismatch, zero tensors, count mismatch, name mismatch, malformed config, OSError on read. **100% line coverage** (50 executable lines, all hit by 17 dedicated tests). |
| GPU memory cap, quarantine scan, `_register_module_aliases` | **DROPPED** | All three only existed to work around FreqAI's `IResolver` re-import lifecycle. Gone in v4. |

**Total port lift:** ~70% of TFTModel.py code reused (the architecture, the
training loop body, the predict pipeline, the helpers); ~30% rewritten
to drop FreqAI types and the legacy serialise path; ~15% (the GPU-budget /
quarantine / sys.modules-proxy / per-pair-checkpoint surface) dropped entirely.

## What is stub vs. implemented

| Module | Status | Implementation notes |
|---|---|---|
| `models/registry.py` | **Implemented** | Thread-safe; LRU eviction with stable tie-break by registration order; load-on-demand; explicit unloader callback (catches eviction-time exceptions so unloader bugs don't strand the registry). |
| `models/ollama_client.py` | **Implemented** | Sync `httpx.Client`. `generate` / `chat` / `ps` / `pull`. Per-request `keep_alive` override. Retry on 503 + connection error, no retry on 4xx. Telemetry callback hook for ledger latency writes. `httpx.MockTransport` is the test surface (vcrpy not installed in this env — see "Tooling deviation" below). |
| `models/tft.py` | **Implemented** | Standalone PyTorch, no FreqAI imports. `safetensors` weights + JSON metadata. `validate_artifact` has 100% line coverage. |
| `models/tft_architecture.py` | **Implemented (port)** | Verbatim port from `user_data/freqaimodels/tft_architecture.py`. Three `cast(torch.Tensor, ...)` added so mypy --strict accepts `nn.Module` calls. |
| `models/sentiment.py` | **STUB** | Returns neutral score=0.0, confidence=0.0. Counts newline-separated headlines for `headline_count`. TODO: replace with `hermes3:8b` prompt-based call once the prompt is locked (`docs/sentiment_prompts.md`, currently in `user_data/modules/sentiment_engine.py`). |
| `models/microstructure.py` | **STUB** | Returns neutral imbalance=0.0. TODO: defer until a strategy actually needs it (per rev2 §6.1, v4 hot path does NOT include this model). |

## Commit shas

Single commit on `feat/v4-build-models`:

- **`4bc24d1`** — `feat(quanta-core/models): ollama registry + safetensors TFT port`

The branch has **not** been pushed (per the brief). Operator review precedes any push or merge into `feat/v4-build`.

## Tooling deviation from the build prompt

The prompt called for "mocked HTTP via vcrpy". The runtime environment
ships `httpx` and `pytest-asyncio` but **not** `vcrpy` (verified via
`python3 -c "import vcr"`, ModuleNotFoundError). I used
`httpx.MockTransport` instead — it provides the same primitives the
build needed (recorded request inspection + scripted response sequences
+ scripted exceptions), inline in the test source rather than via a
cassette file. When the operator adds `vcrpy` to the project deps,
swapping the test harness is mechanical (cassette per endpoint × ~5
endpoints). Documented in the module docstring of `test_ollama_client.py`.

## What's next (for the agent that wires this into the live engine)

1. The registry exposes a synchronous surface. The live engine wraps
   `get()` / `predict()` in `asyncio.to_thread`. **Do NOT** add an
   async surface here without explicit operator approval (it would
   force every test to declare `@pytest.mark.asyncio` for no real win
   on a single-process pool — see registry docstring).
2. `OllamaClient` is the only place that touches the Ollama daemon.
   The orchestrator's "load-on-demand + evict-on-completion" rev2
   choreography (rev2 §4.1) is implemented as a discipline: callers
   pass `keep_alive="0s"` on the LAST request of a debate.
3. The TFT module loads onto CPU by default for safety. Pass
   `device="cuda"` at construction (or `TFTModel.load(path, device="cuda")`)
   to materialise on the GPU. The legacy `set_per_process_memory_fraction(0.3)`
   cap is **NOT** carried over — the v4 design relies on the
   `debate_in_flight` flag + `gpu_gate.sh` mutual exclusion instead
   (rev2 §3.3).
4. `validate_artifact` returns the parsed metadata dict. Use it.
   Don't re-parse `metadata.json` in caller code.
5. Per-pair training resume (the `_save_resume_checkpoint` /
   `_load_resume_checkpoint` logic from the legacy `TFTModel.py`) is
   intentionally **not ported**. Add it back as a separate change with
   safetensors-format checkpoints if the ModelForge weekly retrain
   loop needs sub-epoch resume.
6. The `models/__init__.py` `__all__` is the public API. Code outside
   `models/` should import from `quanta_core.models` (top-level), not
   reach into submodules.
