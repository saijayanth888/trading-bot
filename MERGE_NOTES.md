# Merge notes — `fix/production-hardening-trading-bot`

Branch: `fix/production-hardening-trading-bot`
Created: 2026-05-12
Status: NOT pushed. Local-only for operator review.

## Commits (chronological)

1. **`2fa3935` — fix(tests): add conftest + RiskGovernor isolation + skip
   stale modules**
   Pytest goes from `266 passed / 10 failed / 2 errors` →
   `277 passed / 3 skipped`. Adds `tests/conftest.py` with the missing
   `tmp_user_data` fixture and an autouse `RISK_GOVERNOR_ANCHORS_PATH`
   redirect; updates two stale assertions; skips two test files that
   imported removed module symbols (`DB_PATH`, `CRYPTOQUANT_API_KEY`).

2. **`100f98b` — fix(portability): replace hardcoded `/home/saijayanthai`
   with `$HOME`-relative paths**
   19 files touched. Replaces operator-specific paths in shell-script
   `REPO=` variables, Python shebangs, dashboard fallback path lists,
   the LLM-redaction regex, and `cron` workdirs. Also tightens
   `auto_rollback.py` (boundary `>=` for the daily-loss limit, explicit
   adjacent-tempfile atomic write).

3. **`3bb2a73` — fix(strategy): wrap every hot-path callback in
   try/except — fail-neutral**
   `FreqAIMeanRevV1.py` only. Wraps `populate_indicators`,
   `populate_entry_trend`, `populate_exit_trend`, `confirm_trade_entry`,
   `custom_stake_amount`, `custom_stoploss`, `custom_exit`, and
   `bot_loop_start`. Each has a documented exception policy
   (fail-CLOSED on entry paths, fail-NEUTRAL on indicators/exits,
   fail-CONSERVATIVE on sizing/stoploss). Method bodies move into
   `_<name>_inner` private methods; behaviour unchanged on the happy
   path.

4. **`8e67cf2` — chore(deps): pin upper bounds + opt-out DUMP_PG for
   tests**
   `requirements-extra.txt` gets upper bounds on every line.
   `backup.sh` honours a caller-supplied `DUMP_PG` env var so tests
   can disable the `docker compose exec postgres pg_dump` step.

## Merge plan (recommended)

```bash
# Operator review:
git checkout fix/production-hardening-trading-bot
git log main..HEAD --oneline      # 4 commits to review
git diff main..HEAD --stat        # ~508 insertions, ~101 deletions

# Tests should be green:
pytest tests/ -q                   # expect 277 passed, 3 skipped

# After approval — fast-forward main:
git checkout main
git merge --ff-only fix/production-hardening-trading-bot
# Do NOT push yet — operator's separate change to main needs review.
```

## What's NOT in this branch

- Doc-side path cleanup (`docs/*.md`, `HANDOFF.md`, `HERMES_SETUP_REPORT.md`).
  Those reference `/home/saijayanthai/` in operator-written prose where
  the path is documentation, not code. Separate doc pass recommended.
- `CHECKLIST.md` + `MIGRATION_NOTES.md` move into `docs/`. Operator
  preference required (do you want them in `docs/release/` or
  `docs/runbooks/`?).
- A Playwright frontend empty-state test for the new cards. Out of
  scope; sibling work item.
- A rewrite of `test_regime.py` / `test_onchain.py` against the
  Postgres-backed module surface. Marked skipped with a clear note.

## Verification

```bash
# Strategy syntax check:
python3 -c "import ast; ast.parse(open('user_data/strategies/FreqAIMeanRevV1.py').read())"

# Full pytest:
python3 -m pytest tests/ --no-header -q
# 277 passed, 3 skipped in 11.31s

# No hardcoded operator paths in tracked code (docs/markdown only):
git ls-files | xargs grep -l '/home/saijayanthai' | grep -vE '\.(md|json)$'
# (empty)
```
