# Hermes Gateway — runbook

**One source of truth** for the Hermes gateway's lifecycle in this deployment.
Read this before debugging gateway/cron issues.

---

## Architecture

| Service | Scope | What it does | Auto-start at boot? |
|---|---|---|---|
| `hermes-gateway.service` | **USER systemd** (`~/.config/systemd/user/`) | Runs `hermes_cli.main gateway run --replace`. Hosts the cron scheduler, Telegram/Slack adapters, and MCP-tool registry for cron-driven agents. | ✅ Yes (enabled) |
| `hermes-mcp.service` | SYSTEM systemd (`/etc/systemd/system/`) | Runs the trading-bot MCP server (`hermes-mcp/server.py`). Read-only access to trades, regime, portfolio. | ✅ Yes |
| `hermes-dashboard.service` | USER systemd | TUI dashboard at `127.0.0.1:9119`. Browse sessions, configure, MCP tools. | ✅ Yes |
| `hermes-gateway-heartbeat.timer` | USER systemd | Every 30s: writes `is-active` of the gateway/mcp/dashboard into `user_data/state/*.alive` files. Trading-bot dashboard reads these to show service status. | ✅ Yes |

**Critically**: there is **no longer** a `hermes-gateway.service` at the **system** scope. Removed 2026-05-11 because the system-level + user-level pair were running `--replace` against each other every ~5 min, creating a Telegram-spam restart cycle. The user-scope service is the canonical gateway. **Do not reinstall the system one.**

---

## Reverse-proxy WARNING (auth safety)

**Do NOT add a reverse proxy (nginx / caddy / cloudflared / Traefik) in front of the dashboard without re-enabling `Authorization: Bearer` headers on the SPA's `fetch()` calls.**

The dashboard's `require_mcp_key` dependency (in `user_data/dashboard/ops_routes.py`) bypasses auth for same-origin browser POSTs — i.e. requests whose `Origin` header matches the `Host` header. As of B-17 (2026-05-11) this exemption is also gated on `request.client.host in ("127.0.0.1", "::1")` for defense-in-depth.

A reverse proxy breaks both halves of this gate at once:
- The proxy rewrites `Host` to match its own hostname, and the browser will send `Origin` matching that same hostname → same-origin check passes for **every** external request.
- The TCP peer the dashboard sees is the proxy's local IP (`127.0.0.1` if proxy + dashboard share the host, or the proxy's container IP if dockerized) → the loopback check passes too.

Net effect: every external request looks like a local same-origin operator click. **The auth gate silently re-opens.**

If you ever need to put the dashboard behind HTTPS / a reverse proxy:
1. Delete the same-origin bypass block in `require_mcp_key` (the `if host_header and origin and client_host in ("127.0.0.1", "::1")` block), forcing **every** mutating call to carry a Bearer token, AND
2. Restore `Authorization: Bearer ${HERMES_MCP_KEY}` headers on every SPA `fetch()` to mutating endpoints (`/api/ops/pause`, `/api/ops/resume`, `/api/ops/regime_config`, `/api/ops/rebalance`, `/api/mcp/dispatch/*`).

References: `user_data/dashboard/ops_routes.py::require_mcp_key`, `tests/test_ops_dashboard.py::test_pause_*` (the 4 B-17 tests pin the current behaviour).

---

## Daily health check (one command)

```bash
bash scripts/check_hermes_health.sh
```

Output sections:
- Gateway (user) state, PID, NRestarts, uptime
- Confirms the system-level unit file is gone
- Heartbeat file content + age
- MCP + dashboard liveness
- Whether our **local Hermes patches** are still applied
- Telegram shutdown-notification count in the last hour (should be 0)

---

## If `NRestarts` is climbing or you see "gateway is cycling"

```bash
# Step 1 — is the killer back?
ls /etc/systemd/system/hermes-gateway.service  # should say: No such file
systemctl is-active hermes-gateway.service     # should say: inactive
```

If the unit file exists or the system service is active, run:

```bash
sudo systemctl stop hermes-gateway.service
sudo rm /etc/systemd/system/hermes-gateway.service
sudo systemctl daemon-reload
systemctl --user restart hermes-gateway.service
```

```bash
# Step 2 — are local patches present?
bash scripts/check_hermes_health.sh

# If "scheduler worker-pool patch MISSING" or similar:
bash scripts/reapply_hermes_patches.sh
```

---

## After `hermes update`

`hermes update` runs `git pull --ff-only` on `~/.hermes/hermes-agent`, which **wipes our local edits**. We have two local patches that are NOT yet upstreamed:

1. **`cron: dedicated worker-pool dispatch`** — fixes the tick-lock-blocks-stocks-crons bug. Without it, when an LLM cron job (risk_monitor_15min, market_research_30min) makes a 10-30 min hermes3:70b call, ALL other crons (wheel_candles, shark_*, etc.) get silently skipped past their grace windows.

2. **`ops: silence gateway-restart noise + diagnostic stack for clean-exit`** — adds DIAG stack-trace logging to `gateway/run.py:stop()` so we can find what's triggering shutdowns when debugging.

The patches live in `hermes_patches/*.patch`. Re-apply with:

```bash
bash scripts/reapply_hermes_patches.sh
```

This script is **idempotent** — running it when patches are already applied is a no-op. The script verifies syntax + restarts the gateway after applying.

**Add this to your `hermes update` workflow:**

```bash
hermes update
bash /home/saijayanthai/Documents/trading-bot/scripts/reapply_hermes_patches.sh
bash /home/saijayanthai/Documents/trading-bot/scripts/check_hermes_health.sh
```

---

## Config bits in `~/.hermes/config.yaml`

Top-level (not nested!) `platforms:` block must be present:

```yaml
platforms:
  telegram:
    gateway_restart_notification: false
  slack:
    gateway_restart_notification: false
```

This suppresses the per-restart "Gateway offline" / "Gateway online" messages to Telegram + Slack home channels. Operator monitors gateway health via the trading-bot dashboard Services card instead.

If the gateway ever starts spamming Telegram with restart messages, this config block is missing. Re-add at the top level (not under `display.platforms` — that's the wrong nesting; it's a config-loader gotcha).

---

## Logs

| What | Where |
|---|---|
| Gateway internal log (rich detail) | `~/.hermes/logs/agent.log` |
| systemd-user journal for gateway | `journalctl --user -u hermes-gateway.service -f` |
| DIAG stack traces on stop() | grep `~/.hermes/logs/agent.log` for `DIAG stop\(\) invoked` |
| Cron job output (per-job dirs) | `~/.hermes/cron/output/<job-id>/` |
| Cron job registry | `~/.hermes/cron/jobs.json` |
| Heartbeat files (what dashboard reads) | `user_data/state/hermes-*.alive` |

---

## "Why is the gateway/why is it not firing crons" decision tree

```
Q1: Run `bash scripts/check_hermes_health.sh`. Anything red?
    → fix that first

Q2: Is gateway uptime > 5 min? (`systemctl --user show hermes-gateway.service --property=NRestarts --value` should be stable, not climbing)
    → if NOT: see "If NRestarts is climbing" section above
    → if YES: gateway is stable; problem is elsewhere

Q3: Is the cron firing? (check `~/.hermes/cron/jobs.json` for `last_run_at` of the specific job)
    → if last_run_at is NEVER but next_run_at is recent past:
       → patches missing? → reapply
       → or the LLM cron's still holding the lock (worker-pool patch lets others
          run in parallel, but a long workdir-job blocks subsequent workdir-jobs)

Q4: Did the job run but produce nothing useful?
    → check `~/.hermes/cron/output/<job-id>/<timestamp>.md`
    → LLM jobs can hallucinate (we saw risk_monitor_15min report "drawdown -6.5%"
       when reality was -0.06% — be skeptical of LLM-cron output)
```

---

## What we did NOT change (so you know)

- The `--replace` flag in the gateway's startup behavior — kept as designed.
- `RestartSec=60` in the user unit — kept.
- The MCP server (hermes-mcp.service) — completely untouched.
- The TUI dashboard's slash_worker behavior — untouched.
- Cron job definitions (`~/.hermes/cron/jobs.json`) — untouched.

---

Last updated: 2026-05-11. Maintained by the trading-bot operator.
