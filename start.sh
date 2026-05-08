#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

CONTAINER=freqtrade

echo "==> docker compose up -d"
docker compose up -d

echo "==> Waiting for container '$CONTAINER' to become healthy…"
for i in $(seq 1 60); do
    status=$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "missing")
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER" 2>/dev/null || echo "missing")

    if [[ "$status" != "running" ]]; then
        printf "  [%02d/60] status=%s, retrying…\n" "$i" "$status"
        sleep 2
        continue
    fi

    if [[ "$health" == "healthy" || "$health" == "none" ]]; then
        printf "  [%02d/60] status=%s health=%s — ready.\n" "$i" "$status" "$health"
        break
    fi
    printf "  [%02d/60] status=%s health=%s\n" "$i" "$status" "$health"
    sleep 2
done

echo
echo "==> Web UI:  http://localhost:8080"
echo "==> Tailing logs (Ctrl-C to detach; container keeps running)"
echo
exec docker compose logs -f --tail=100 "$CONTAINER"
