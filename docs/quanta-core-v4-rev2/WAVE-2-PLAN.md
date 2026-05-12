# V4 Build Wave 2 — Overnight Coding + QA Sprint

**Started 2026-05-12 ~23:15 ET** by operator. Goal: finish remaining V4 code,
QA all of wave 1 + wave 2, regression test legacy, end the night ready for
morning merge + push.

## 10 agents dispatched in parallel

### Code builders (5 agents)
- #A reconciliation — unify 4 root + 2 nested branches → single `feat/v4-build`
- #B backtest engine — port the parity-oracle (same Strategy class, swapped clock/venue)
- #C hermes Layer 8 — 7 cron modules per rev2 doc #11
- #D agents/debate — 30s parallel deliberation per rev2 doc #5
- #E ledger + observability — Postgres schema + structlog metrics

### Quality + integration (5 agents)
- #F quality engineer — run tests across ALL branches, coverage report, mypy/ruff verify
- #G frontend reviewer — locate feature/v3-frontend (try every reasonable path), if found review; else audit current dashboard for any wave-1 regressions
- #H regression engineer — verify legacy `user_data/` + `stocks/` tests pass; freqtrade dashboard untouched
- #I integration engineer — wait for #A through #E, then write end-to-end smoke (live engine + strategy + risk + execution) on `feat/v4-build`
- #J coordinator — poll all other agents, write `WAVE-2-PROGRESS.md` every 15 min, surface blockers

## Known v3-frontend gap

Operator mentioned `feature/v3-frontend` branch with V3 frontend work "pushed".
Branch not found in:
- Local branches (`git branch -a`)
- Origin (`git ls-remote origin`)
- model-forge repo
- Filesystem (no v3-frontend artifacts)

Either pushed to a different remote, branch name differs, or push hasn't
completed yet. Agent #G investigates further; if found later, integrate then.

## Scope rules (apply to ALL agents)

1. NO push to remote
2. NO restart of freqtrade or dashboard
3. NO touching `user_data/` or `stocks/` files (read-only audit OK)
4. Layout: root-level `src/quanta_core/` per doc #10 (matches 4 of 6 wave-1 agents)
5. Each agent owns its own worktree branch `feat/v4-wave2-{role}`
6. ONE atomic commit per concern + final HANDOFF
7. mypy --strict + ruff + 85% coverage min (95% on risk + ledger + execution)
8. Honor DESIGN-LOCK.md + the 17 rev1+rev2 design docs
9. The coordinator (#J) is the ONLY agent that writes to `WAVE-2-PROGRESS.md`

## Morning merge sequence (operator runs)

```bash
git checkout feat/v4-build
# 1. Apply reconciliation work from agent #A (already merged on feat/v4-build)
# 2. Apply wave 2 modules in order:
git merge --no-ff feat/v4-wave2-ledger        # foundational — others import
git merge --no-ff feat/v4-wave2-hermes
git merge --no-ff feat/v4-wave2-agents
git merge --no-ff feat/v4-wave2-backtest
# 3. Apply integration smoke
git merge --no-ff feat/v4-wave2-integration
# 4. Run full test suite — expect ~700 tests
pytest src/quanta_core/ tests/ -v
# 5. If all green, push:
git push origin feat/v4-build
```

— claude (sprint dispatched 2026-05-12 23:15 ET)
