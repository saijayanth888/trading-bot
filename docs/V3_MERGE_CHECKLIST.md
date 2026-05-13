# V3 merge-to-main checklist + rollback runbook

> **Audience:** the operator and any agent assisting with the V3 ship. This is the runbook from the moment the QA Verifier issues GREEN to the moment V3 is rendering on live operator monitors.
>
> **Branch:** `feature/v3-frontend`
> **Target:** `main`
> **Deploy target:** `http://192.168.1.49:8081/ops` (the live Docker-served dashboard)
> **Drafted:** 2026-05-12 (after Wave 1 launch, before Wave 1 land)

---

## Phase 1 · Pre-merge gates (must all be GREEN before phase 2)

| # | Gate | How to verify | Pass criterion |
| - | - | - | - |
| 1 | QA Verifier emitted GREEN | Read final message from the Wave 2 QA Verifier subagent | Verdict line = `GREEN` |
| 2 | `git status` clean | `cd trading-bot && git status -s` | Empty output |
| 3 | Branch up to date with main | `git fetch origin main && git log --oneline origin/main..main` | Empty (main hasn't moved since branch creation) |
| 4 | Commit count is sane | `git log --oneline main..feature/v3-frontend` | 5-9 commits (Wave 0 + 4× Wave 1 + maybe Wave 2 prep + maybe fix-ups) |
| 5 | No new dependency files | `git diff main..feature/v3-frontend -- requirements.txt requirements*.txt package.json` | Empty |
| 6 | Cache-buster is V3-marked | `grep "?v=" user_data/dashboard/templates/{ops,dashboard}_spa.html` | All `?v=` contain `v3-wave1` or higher |
| 7 | pytest baseline preserved | `python3 -m pytest tests/test_dashboard.py tests/test_ops_dashboard.py tests/test_no_legacy_color_tokens.py -q` | `26 passed` |
| 8 | `docker compose config` valid | `docker compose -f docker-compose.yml config --quiet` (if Docker available locally) | Exit 0, no output |

If ANY gate is RED, **do not proceed.** Loop back to the Design Lead with the failure detail.

---

## Phase 2 · Merge mechanics

### 2.1 Strategy

**Recommended:** merge commit (not squash) — preserves Wave 0 + Wave 1A-D commit history on main, which makes targeted rollback to a specific Wave 1 agent's work possible if a regression surfaces post-deploy.

```bash
cd /Users/saijayanthreddyailoni/Documents/Project-Doze/trading-bot
git checkout main
git pull origin main --ff-only             # ensure local main matches remote
git merge --no-ff feature/v3-frontend \
  -m "$(cat <<'EOF'
merge(v3): V3 redesign — 7 signature moves, 30 cards, multi-agent execution

V3 redesign per docs/V3_REDESIGN_PLAN.md. Lands as one merge commit
over feature/v3-frontend (5-9 commits). Frontend-only — no backend
changes, no new endpoints, no test regression, no token rename.

Signature moves (per plan §5):
- DD Ribbon (card 00)
- Debate Floor (card 21a)
- Gates Matrix (card 05)
- Sparkline Strip (cards 06 + 23)
- Kill Bar (topbar drawer)
- Heartbeat dot (top-left)
- Cmd-K command palette (global)

All 30 cards redesigned per plan §6. Density bug fixed (compact↔roomy
spread 18.72%, gate ≥15% PASS). 52 new V3 tokens added (--v3-*),
zero existing tokens renamed or revalued.

QA Verifier: GREEN per docs/V3_QA_VERIFIER_PROMPT.md run.

Cache-buster: v3-wave1-multi → forces operator browsers to re-fetch
quanta.css, qc_react.js, components.js, ops_spa.js, dashboard_spa.js
on next visit.

Acceptance gates (plan §8): all 18 items PASS.
EOF
)"
```

### 2.2 Push (operator presses the button)

```bash
git push origin main
```

After push, immediately check the GitHub commit graph to verify the merge landed correctly.

---

## Phase 3 · Deploy to live dashboard

The dashboard runs in a Docker container at `http://192.168.1.49:8081/`. The static assets are **baked into the image** at build time (per `TRADING_BOT_PROMPT.md`), so deploying V3 requires a container rebuild + restart.

### 3.1 Rebuild

```bash
# On the host running 192.168.1.49 (the operator's box, not this dev machine)
cd /path/to/trading-bot                    # whatever the host's checkout path is
git pull origin main
docker compose build dashboard             # rebuilds the dashboard image with the new static files
docker compose up -d dashboard             # restart the container with the new image
```

Watch the rebuild output. If the build fails, **stop the deploy** and rollback (phase 5).

### 3.2 Smoke test on live

Once `docker compose up -d` completes, the dashboard is restarting (~10-30s downtime). Smoke test:

```bash
# Same host or any machine with network access to 192.168.1.49
curl -sf http://192.168.1.49:8081/api/mode | jq .
# Expected: {"mode":"paper","state":"running","dry_run":true}

curl -sIf http://192.168.1.49:8081/static/css/quanta.css | grep -i etag
# Expected: a new ETag (because the file content changed)

curl -sf http://192.168.1.49:8081/ops > /tmp/ops.html
grep "v3-wave1" /tmp/ops.html
# Expected: 4+ matches on the ?v= cache-buster
```

If any smoke fails, **rollback** (phase 5).

### 3.3 Visual verification

Open `http://192.168.1.49:8081/ops` in a fresh browser tab (hard-reload `Cmd-Shift-R` to defeat operator browser cache). Quick visual checks:

- [ ] Hero P&L digit is large (96px)
- [ ] DD Ribbon appears across the top of card 00 with a needle
- [ ] Heartbeat dot pulses top-left
- [ ] Kill bar reveals on hover at bottom-of-page
- [ ] Card 21a renders as Debate Floor (5 role-cards in courtroom layout)
- [ ] Card 05 renders as Gates Matrix (heat-map)
- [ ] Card 06 renders as Sparkline Strip (ticker rows)
- [ ] Cmd-K opens the palette
- [ ] Theme toggle still works (control / geist / bloomberg)
- [ ] Density toggle visibly changes layout (compact much tighter than roomy)
- [ ] No console errors in DevTools

If any visual check fails, **rollback** (phase 5) and reopen the issue.

---

## Phase 4 · Post-deploy

### 4.1 Update operator runbook

Append to `docs/HERMES_GATEWAY_RUNBOOK.md` (or wherever the operator's daily runbook lives) one line:

```
2026-MM-DD: V3 frontend redesign deployed. New keyboard shortcuts: Cmd-K (palette), Cmd-Shift-K (kill bar). 3 themes still supported. Plan: docs/V3_REDESIGN_PLAN.md.
```

### 4.2 Slack ping

Optional: drop a Slack message in the bot's status channel:

> V3 dashboard redesign deployed. New: Cmd-K palette, DD ribbon, debate floor, gates matrix, sparkline strip, heartbeat dot, kill bar. Old shortcuts preserved.

### 4.3 Bake-in period

Watch the dashboard for the first 24h after deploy. Look for:
- Operator complaints about layout (e.g. "I can't find the X")
- Console errors in the browser (open DevTools occasionally)
- Hot-reload behavior on data refresh (every 5s/10s/30s polling tick)
- Theme/density toggles holding state across page navigation

If issues surface, **schedule a Wave 1.1 fix commit** rather than rolling back the whole V3.

---

## Phase 5 · Rollback procedure (only if necessary)

If V3 breaks something on production:

### 5.1 Quick rollback (most common — fastest)

If you spotted the breakage within 30 minutes of deploy and haven't shipped anything else:

```bash
cd /path/to/trading-bot                    # on the host
git revert -m 1 HEAD                       # creates a revert-of-merge commit
git push origin main
docker compose build dashboard
docker compose up -d dashboard
```

This brings back the pre-V3 dashboard. Smoke test again (phase 3.2).

### 5.2 Targeted rollback (one signature move broke; others fine)

Because Wave 1 lands as separate commits, you can revert ONE agent's work without rolling back the others. Identify the offending commit:

```bash
git log --oneline main | grep "v3-wave1"
# e.g.:
# abc1234 feat(v3-wave1D): sparkline strip + cmd-k palette + ops cards
# def5678 feat(v3-wave1C): gates matrix + trades tape + regime editor + risk cards
# ghi9012 feat(v3-wave1B): debate floor + LLM stack + decision audit
# jkl3456 feat(v3-wave1A): hero + kill bar + DD ribbon + heartbeat + sentiment radar
# mno7890 feat(v3-wave0): tokens + density bug fix + cache-buster bump
```

Revert the specific commit:

```bash
git revert <SHA>                           # e.g. git revert abc1234 for Wave 1D only
git push origin main
docker compose build dashboard && docker compose up -d dashboard
```

This rolls back ONLY that agent's cards. Other Wave 1 work and Wave 0 tokens stay.

⚠️ Wave 0 (tokens) is consumed by everyone — DO NOT revert Wave 0 in isolation; if Wave 0 is broken, revert the whole merge (phase 5.1).

### 5.3 Cold rollback (worst case — production is broken and recent changes haven't been preserved)

```bash
git reset --hard <last-known-good-sha>     # e.g. 63ded54 = pre-V3 main
git push --force origin main               # ⚠️ DESTRUCTIVE — gets operator approval first
docker compose build dashboard
docker compose up -d dashboard
```

**Force push to main is destructive.** Do this only if:
1. Operator explicitly approves
2. No other team member has pulled in the broken state
3. You've preserved the V3 work elsewhere (e.g. tagged as `v3-deploy-attempt-1`)

After cold rollback:

```bash
git checkout -b v3-deploy-attempt-1-archive <broken-sha>
git push origin v3-deploy-attempt-1-archive
```

Then the V3 branch lives as an archive while `main` returns to working state.

---

## Phase 6 · Acceptance sign-off

Once V3 is on live for 48 hours without rollback, mark plan complete:

```bash
git tag -a v3-shipped -m "V3 redesign shipped 2026-MM-DD, 48h soak passed"
git push origin v3-shipped
```

Update `docs/V3_REDESIGN_PLAN.md` §0 status line:

```diff
- > **Status:** ✅ Plan locked · operator sign-off received · ready for Wave 0 (Token Smith) to start writing code
+ > **Status:** ✅ SHIPPED 2026-MM-DD · 48h soak passed · plan retained as reference
```

---

## Summary cheat sheet

| Phase | What | Who | Time |
| - | - | - | - |
| 1 | Pre-merge gates | Design Lead + QA Verifier | 5 min after Wave 1 lands |
| 2 | Merge `feature/v3-frontend` → `main` | Operator | 30 seconds |
| 3 | Deploy (rebuild + restart Docker) | Operator on the host | 2-5 min |
| 4 | Post-deploy comms + bake-in | Operator | passive 24h |
| 5 | Rollback (only if needed) | Operator | 2-10 min |
| 6 | Sign-off | Operator after 48h | 30 seconds |

End of merge runbook.
