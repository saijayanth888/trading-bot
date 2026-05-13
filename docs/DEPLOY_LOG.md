# Dashboard deploy log

Append-only log of dashboard image rebuilds + recycles. One line per
deploy: timestamp · image SHA prefix · what changed · freqtrade status.

The reference here is to remind future-me that dashboard rebuilds use
`docker compose up -d --no-deps dashboard` to avoid recycling
freqtrade — see [[reference-dashboard-deploy]] in memory.

---

- 2026-05-13T01:36Z · image 3541bd2bf76f · V4Buffer wired into
  /api/v4/{debate/history,parity,montecarlo} with mock fallback ·
  freqtrade untouched (StartedAt 01:23:51Z unchanged) · 8 /api/v4/*
  + 7 /api/ops/* probes all 200.
