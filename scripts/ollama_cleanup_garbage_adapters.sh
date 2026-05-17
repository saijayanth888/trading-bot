#!/usr/bin/env bash
# ollama_cleanup_garbage_adapters.sh
#
# One-shot cleanup of the 8 garbage LoRA adapters published on 2026-05-17.
# These adapters were trained on 5 stale options-era records each, with 9-second
# training runs. Every eval metric was theatrical (~0.0). They are NOT fit for
# production inference.
#
# Usage:
#   bash scripts/ollama_cleanup_garbage_adapters.sh
#
# The script:
#   1. Deletes the 8 garbage tags via Ollama /api/delete.
#   2. Lists remaining hermes3-* tags to confirm cleanup.
#   3. Verifies shark routing falls back to hermes3:8b for the 4 trading roles.
#
# DO NOT RUN until you have reviewed this script. Operator runs it explicitly.
# The adapters can be re-published from /app/data/adapters/run-*/gen-1/ if needed
# (run adapter.publish_ollama for each run_id), but do NOT do this until the
# training pipeline produces non-garbage adapters.
#
# Precondition: Ollama must be running on localhost:11434.
# Precondition: docker container 'dashboard' must be running for routing verify.

set -euo pipefail

OL="http://localhost:11434"

GARBAGE=(
    "hermes3-8b-reflector-current"
    "hermes3-8b-reflector-v20260517"
    "hermes3-8b-bear-current"
    "hermes3-8b-bear-v20260517"
    "hermes3-8b-bull-current"
    "hermes3-8b-bull-v20260517"
    "hermes3-8b-arbiter-current"
    "hermes3-8b-arbiter-v20260517"
)

echo "=== Deleting garbage adapters from Ollama ==="
for tag in "${GARBAGE[@]}"; do
    echo -n "Deleting ${tag}... "
    response=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "${OL}/api/delete" \
        -H "Content-Type: application/json" \
        -d "{\"name\": \"${tag}\"}")
    if [[ "${response}" == "200" ]]; then
        echo "OK (200)"
    elif [[ "${response}" == "404" ]]; then
        echo "SKIP (404 — already absent)"
    else
        echo "WARN (HTTP ${response})"
    fi
done

echo ""
echo "=== Remaining hermes3-* tags after cleanup ==="
curl -s "${OL}/api/tags" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    tags = sorted(m.get('name', '') for m in data.get('models', []))
    hermes_tags = [t for t in tags if 'hermes3' in t]
    if hermes_tags:
        for t in hermes_tags:
            print(' ', t)
    else:
        print('  (no hermes3 tags remaining)')
except Exception as e:
    print('  ERROR reading tags:', e)
"

echo ""
echo "=== Verifying shark routing falls back to hermes3:8b base ==="
docker exec dashboard python3 -c "
import sys
try:
    from stocks.shark.llm.client import resolve_role_route, _reset_routing_cache
    _reset_routing_cache()
    roles = ['trading-reflector', 'trading-bull', 'trading-bear', 'trading-arbiter']
    all_ok = True
    for role in roles:
        r = resolve_role_route(role)
        model = r.get('model', '?')
        if model == 'hermes3:8b':
            print(f'  OK  {role} -> hermes3:8b (fallback active)')
        else:
            print(f'  WARN {role} still routes to {model}', file=sys.stderr)
            all_ok = False
    if not all_ok:
        print('ERROR: Some roles still route to garbage adapters.', file=sys.stderr)
        sys.exit(1)
    print('All 4 trading roles confirmed on hermes3:8b fallback.')
except ImportError as e:
    print(f'SKIP routing verify: {e}')
    print('(resolve_role_route not available — check dashboard container imports)')
except Exception as e:
    print(f'ERROR in routing verify: {e}', file=sys.stderr)
    sys.exit(1)
" 2>&1

echo ""
echo "=== Cleanup complete ==="
echo "Next steps:"
echo "  1. Run 'ollama list' to confirm garbage adapters are gone."
echo "  2. Run the trading eval suite to confirm hermes3:8b base scores are nonzero."
echo "  3. Do NOT re-run the publish pipeline until commits 7-10 land and the"
echo "     N_MIN gates pass (which requires stock closed trades to accumulate)."
