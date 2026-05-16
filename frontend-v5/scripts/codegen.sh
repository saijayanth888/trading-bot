#!/usr/bin/env bash
# owner: builder-C
# Generate TypeScript types from FastAPI /openapi.json — closes B4→B15 chain
# per frontend-debate G7. If the backend isn't running, write an empty shim
# so the build doesn't break.
set -euo pipefail

OPENAPI_URL="${OPENAPI_URL:-http://localhost:8081/openapi.json}"
OUT="src/types/api.ts"

mkdir -p "$(dirname "$OUT")"

if curl -sf --max-time 5 -o /tmp/v5-openapi-probe.json "$OPENAPI_URL"; then
  echo "[codegen] fetched $OPENAPI_URL — generating $OUT"
  npx --no-install openapi-typescript "$OPENAPI_URL" -o "$OUT"
else
  echo "[codegen] backend at $OPENAPI_URL not reachable — writing shim to $OUT"
  cat > "$OUT" <<'EOF'
// AUTO-GENERATED SHIM — backend was unreachable at codegen time.
// `npm run codegen` again once the dashboard is up to populate real types.
export type paths = Record<string, never>;
export type components = { schemas: Record<string, never> };
export type operations = Record<string, never>;
EOF
fi
