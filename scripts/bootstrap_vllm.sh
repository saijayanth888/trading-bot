#!/usr/bin/env bash
# bootstrap_vllm.sh — idempotently start the vLLM serving plane.
#
# What it does:
#   1. Verifies docker + the compose project root.
#   2. Ensures ./data/lora-adapters exists (mounted into the container).
#   3. If the `vllm` container is already healthy, exits 0 quickly.
#   4. Otherwise: `docker compose --profile vllm up -d vllm` and wait up
#      to VLLM_BOOT_TIMEOUT_S seconds for /health to pass (default 600s —
#      first boot has to pull ~30 GB of weights).
#   5. Probes /v1/models so the operator can see what adapters are
#      registered.
#
# Usage:
#   bash scripts/bootstrap_vllm.sh
#
# Env knobs (all optional):
#   VLLM_BOOT_TIMEOUT_S       max seconds to wait for /health (default 600)
#   VLLM_HEALTH_URL           override health probe (default 127.0.0.1:8090)
#   VLLM_HF_MODEL             override the HF model id (compose default:
#                             Qwen/Qwen3-30B-A3B-Instruct-2507)
#   VLLM_QUANTIZATION         fp8 / fp4 / awq (default fp8)
#
# Exit codes:
#   0  vLLM is healthy
#   1  pre-flight failure (no docker, wrong cwd, etc.)
#   2  container failed to start
#   3  container started but /health never returned 200 within the budget
set -Eeuo pipefail

# ── Resolve project root regardless of where this script was called from ──
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
cd "$PROJECT_ROOT"

# ── Pre-flight ────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
    echo "[bootstrap_vllm] FATAL: docker not on PATH" >&2
    exit 1
fi
if [[ ! -f docker-compose.yml ]]; then
    echo "[bootstrap_vllm] FATAL: docker-compose.yml not found in $PROJECT_ROOT" >&2
    exit 1
fi
if ! grep -q "^  vllm:" docker-compose.yml; then
    echo "[bootstrap_vllm] FATAL: docker-compose.yml has no 'vllm' service block." >&2
    echo "                 Did you check out the stage/vllm-multi-lora-serving branch?" >&2
    exit 1
fi

mkdir -p ./data/lora-adapters

BOOT_TIMEOUT_S="${VLLM_BOOT_TIMEOUT_S:-600}"
HEALTH_URL="${VLLM_HEALTH_URL:-http://127.0.0.1:8090/health}"
MODELS_URL="${VLLM_MODELS_URL:-http://127.0.0.1:8090/v1/models}"

# ── Fast path: already healthy? ───────────────────────────────────────────
if curl -fsS -o /dev/null --max-time 3 "$HEALTH_URL" 2>/dev/null; then
    echo "[bootstrap_vllm] vLLM already healthy at $HEALTH_URL"
    curl -fsS "$MODELS_URL" 2>/dev/null | head -c 2000 || true
    echo
    exit 0
fi

# ── Warn if freqtrade-nfi is also up on 8090 (collision risk) ─────────────
if docker ps --format '{{.Names}}' | grep -qx "freqtrade-nfi"; then
    echo "[bootstrap_vllm] WARNING: freqtrade-nfi is running and also binds 127.0.0.1:8090." >&2
    echo "                  Stop it first or vLLM will fail to bind:" >&2
    echo "                    docker compose --profile nfi stop freqtrade-nfi" >&2
fi

# ── Start vLLM ────────────────────────────────────────────────────────────
echo "[bootstrap_vllm] starting vLLM via 'docker compose --profile vllm up -d vllm' …"
if ! docker compose --profile vllm up -d vllm; then
    echo "[bootstrap_vllm] FATAL: docker compose up failed; check 'docker compose logs vllm'" >&2
    exit 2
fi

# ── Wait for /health ──────────────────────────────────────────────────────
echo "[bootstrap_vllm] waiting up to ${BOOT_TIMEOUT_S}s for $HEALTH_URL …"
deadline=$(( $(date +%s) + BOOT_TIMEOUT_S ))
while (( $(date +%s) < deadline )); do
    if curl -fsS -o /dev/null --max-time 3 "$HEALTH_URL" 2>/dev/null; then
        echo
        echo "[bootstrap_vllm] vLLM is healthy."
        echo "[bootstrap_vllm] /v1/models response:"
        curl -fsS "$MODELS_URL" 2>/dev/null | head -c 2000 || true
        echo
        echo "[bootstrap_vllm] DONE. Trading-bot can now route prose roles via vLLM."
        exit 0
    fi
    # If the container has exited, fail fast instead of waiting the full budget.
    state="$(docker inspect -f '{{.State.Status}}' vllm 2>/dev/null || echo "missing")"
    if [[ "$state" == "exited" || "$state" == "dead" || "$state" == "missing" ]]; then
        echo "[bootstrap_vllm] FATAL: vllm container is in state '$state' — check 'docker compose logs vllm'" >&2
        exit 2
    fi
    sleep 5
    printf '.'
done

echo >&2
echo "[bootstrap_vllm] FATAL: /health never returned 200 within ${BOOT_TIMEOUT_S}s." >&2
echo "                Tail the logs:  docker compose logs --tail=200 vllm" >&2
exit 3
