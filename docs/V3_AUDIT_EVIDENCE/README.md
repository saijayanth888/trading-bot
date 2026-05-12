# V3 Audit Evidence — 2026-05-12

Forensic snapshot of the live dashboard at the moment the V3 redesign plan was written. Preserved so any future agent / operator can reproduce or verify the audit claims in `docs/V3_REDESIGN_PLAN.md`.

## Contents

```
V3_AUDIT_EVIDENCE/
├── README.md           — this file
├── api-samples/        — 31 captured /api/ops/* + /api/* responses (286 KB)
│   ├── _mode.json
│   ├── _pairs.json
│   ├── _state.json
│   ├── _universe.json
│   ├── backtest_gates.json
│   ├── circuit_breakers.json
│   ├── combined_portfolio.json
│   ├── config.json
│   ├── gates.json                  ← largest, 14 KB — 13 crypto + 1 stocks pair × 11 gates each
│   ├── live_trades.json
│   ├── llm_calls.json              ← 4.9 KB — 10 calls + summary + by-role-detail
│   ├── llm_stats.json
│   ├── market_hours.json
│   ├── mcp.json
│   ├── ollama_health.json
│   ├── readiness.json
│   ├── regime.json
│   ├── risk_gates.json             ← operator-editable thresholds + defaults
│   ├── sentiment.json
│   ├── services.json
│   ├── shark_briefing.json
│   ├── shark_override_health.json
│   ├── slack_preview.json
│   ├── stock_regime.json
│   ├── stocks_ml.json
│   ├── stocks.json
│   ├── trades_risk.json
│   ├── training.json
│   ├── training_health.json
│   ├── uptime.json
│   └── weekly_training.json        ← 6 LoRA tracks (all currently no-data, pipeline spinning up)
└── screenshots/        — 12 PNGs captured via Playwright
    ├── v3-research-01-pair-default.png       — / (pair dashboard) at default
    ├── v3-research-02-ops-fullpage.png       — /ops full-page (1912 × 8130 px) — every card
    ├── v3-research-03-ops-zone-A-top.png     — viewport at scroll 0 (hero + scoreboard + LoRA)
    ├── v3-research-04-ops-zone-B-agent-llm.png — viewport at scroll 2200 (agent flow + LLM activity)
    ├── v3-research-05-ops-zone-C-telemetry.png — viewport at scroll 4400 (pair telemetry strips)
    ├── v3-research-06-ops-zone-D-bottom.png  — viewport at scroll 6500 (controls + tools)
    ├── v3-research-07-theme-geist.png        — control viewport in Geist theme
    ├── v3-research-08-theme-bloomberg.png    — control viewport in Bloomberg theme
    ├── v3-research-09-density-compact.png    — control viewport at data-density=compact (BUG: doesn't actually compact)
    ├── v3-research-10-docs-page.png          — /docs page
    ├── v3-research-11-pair-fullpage.png      — / (pair) full-page
    └── v3-research-12-topbar-detail.png      — topbar zoom showing kill switch + refresh selector
```

## How the audit was performed

1. **Browser:** Playwright via the `plugin-playwright-playwright` MCP server.
2. **Viewport:** 1920 × 1200 (typical operator monitor).
3. **Network:** Captured with `curl -sf` against `http://192.168.1.49:8081/api/...`. All endpoints responded 200.
4. **Date/time:** 2026-05-12 ~14:55-15:01 ET (timestamps embedded in each JSON's `checked_at`).
5. **Market state at capture:** Real losing day. Day P&L `-$81.36`. BTC regime `trending_down`. TFT `up=0.369` vs threshold `0.77`. 12 of 13 crypto pairs hard-blocked on regime. SOFI wheel clear, 0 open contracts. ModelForge training pipeline still spinning up (0 of 6 tracks trained).
6. **Operator account context:** `paper / dry-run`, mode `paper`, bot uptime `1d 7h 48m`.

## Key signal preserved for the redesign

- **`gates.json`** is the canonical reality. Each pair's `first_blocker` field is the *exact* operator question the gates-matrix card has to answer.
- **`llm_calls.json` → `summary.by_role_detail`** has the live by-role aggregates and the `last_gist` field per role — this is the data the Debate Floor card (§5.2 of plan) consumes.
- **`weekly_training.json`** has the 6 LoRA `track_id`s with their `headline_metric` per role — this is the data the LoRA tracks grid (card 00c) consumes.
- **`services.json`** has the heartbeat truth — 8/8 up at capture time.
- **`risk_gates.json`** has the operator-editable thresholds — this is what the kill-bar (§5.4) and DD ribbon (§5.1) hit when they fire.

## Reproducing the snapshot

```bash
mkdir -p /tmp/audit && cd /tmp/audit
for ep in services uptime training training_health regime sentiment mcp \
          trades_risk risk_gates config readiness gates market_hours \
          live_trades ollama_health circuit_breakers llm_stats \
          combined_portfolio shark_briefing stocks_ml stock_regime \
          shark_override_health backtest_gates weekly_training llm_calls \
          slack_preview stocks; do
  curl -sf "http://192.168.1.49:8081/api/ops/${ep}" -o "${ep}.json"
done
for ep in mode pairs universe state; do
  curl -sf "http://192.168.1.49:8081/api/${ep}" -o "_${ep}.json"
done

# diff against this folder to see what's changed since the audit
diff -r . <PATH_TO_THIS_FOLDER>/api-samples/
```

End of evidence README.
