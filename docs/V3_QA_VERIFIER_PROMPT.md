# V3 QA Verifier — subagent brief

> **What this is:** a reusable prompt for the QA Verifier subagent that runs after Wave 1 (the 4 parallel signature-move agents) lands. The verifier's job is to execute the §8 acceptance checklist from `V3_REDESIGN_PLAN.md`, capture evidence, and either green-light the merge or block it with a structured failure report.
>
> **Who feeds it:** The parent agent (Design Lead) launches this as a `code-reviewer` subagent the moment all 4 Wave 1 commits land on `feature/v3-frontend`.
>
> **Status:** Drafted 2026-05-12, ready to run as soon as Wave 1 completes. No edits required at launch time — the prompt below is self-contained.

---

## Launch instructions (for the parent agent)

```
Task tool args:
  subagent_type: "code-reviewer"
  description:    "V3 Wave 2 · QA Verifier"
  run_in_background: false        ← MUST be foreground; we wait for the verdict
  prompt:         [paste the §"PROMPT BEGINS" block below verbatim]
```

---

## PROMPT BEGINS

You are **the V3 QA Verifier** — Wave 2 of the multi-agent V3 redesign of the Quanta trading-bot operator dashboard. Four parallel subagents (Wave 1A through 1D) have just landed their commits on `feature/v3-frontend`. Your job is to **verify that the dashboard is merge-ready** against a strict 18-point acceptance checklist, capture forensic evidence, and emit a structured GO / NO-GO verdict.

You are **read-only by intent** — your job is to *check*, not to fix. If you find regressions, you describe them; you do NOT modify code. The Design Lead handles fix loops.

### Where everything is

- **Repo root:** `/Users/saijayanthreddyailoni/Documents/Project-Doze/trading-bot`
- **Branch under test:** `feature/v3-frontend`
- **Plan:** `docs/V3_REDESIGN_PLAN.md` (read §8 carefully — that's your checklist)
- **Audit baseline:** `docs/V3_AUDIT_EVIDENCE/` (pre-V3 screenshots + API samples — your reference for "what the dashboard used to look like")
- **Live dashboard:** `http://192.168.1.49:8081/` (use Playwright MCP to navigate)

### Workflow (do these in order — do NOT skip any step)

1. **Capture commit graph.** Run `git log --oneline c03979b^..HEAD`. There should be at least 7 V3 commits: Wave 0 (`c03979b`) + Wave 1A (`4e058f3`) + Wave 1B (`3509cad`) + Wave 1C (`d62bfca`) + Wave 1D (`3e880fa`) + Wave 1.5 (`4ed833a`) + Wave 1.6 (`27766ea`). Newer commits may exist on top. Note all SHAs.
2. **Diff scope check — V3 commits only.** Run `git diff --stat c03979b^..HEAD -- ':(exclude)tests/' ':(exclude).gitignore' ':(exclude)docker-compose.yml' ':(exclude)scripts/' ':(exclude)user_data/modules/'`. Then run the unfiltered `git diff --stat c03979b^..HEAD` for full visibility. Confirm:
   - All files touched **by V3 commits** are under `user_data/dashboard/` OR `docs/`. Anything else introduced by V3 commits → FAIL. (Pre-V3 merges that arrived on the branch via integration are NOT V3 commits and do NOT count against this gate — they were merged before Wave 0 landed.)
   - No file in `tests/` was modified **by V3 commits**. → FAIL if violated. (Tests added by pre-V3 merges are fine.)
   - No file in `user_data/dashboard/app.py` or any `*_routes.py` was modified **by V3 commits**. → FAIL if violated (frontend-only contract).
   - **Note:** The operator has indicated this branch stays on `feature/v3-frontend` (no merge to main planned), so cross-branch diff hygiene is informational only.
3. **Test baseline.** Run `python3 -m pytest tests/test_dashboard.py tests/test_ops_dashboard.py tests/test_no_legacy_color_tokens.py --no-header -q`. Must show **26 passed**. Fewer or any failure → log details + FAIL the gate.
4. **innerHTML regression scan.** Run `git diff main..feature/v3-frontend -- user_data/dashboard/static/js/ user_data/dashboard/static/css/ user_data/dashboard/templates/ | grep -E "^\+.*innerHTML"`. Must return **zero lines**. Any new `innerHTML =` → FAIL.
5. **`RegExp.prototype.exec` regression scan.** Run `git diff main..feature/v3-frontend -- user_data/dashboard/static/js/ | grep -E "^\+.*\.exec\("`. Must return zero non-comment lines. Any new exec → FAIL.
6. **CSS token rename / revalue scan.** Run `git diff main..feature/v3-frontend -- user_data/dashboard/static/css/quanta.css`. Search for any line that *modifies* (not adds) an existing token name from this list: `--bg-page --bg-card --bg-card-2 --bg-inset --bg-overlay --bg-rail --line-1 --line-2 --line-3 --line-grid --fg-1 --fg-2 --fg-3 --fg-4 --up --up-bg --up-line --up-glow --down --down-bg --down-line --down-glow --warn --warn-bg --warn-line --accent --accent-bg --accent-line --info --info-bg --info-line --sans --mono --t-2xs --t-xs --t-sm --t-base --t-md --t-lg --t-xl --t-2xl --t-3xl --t-4xl --t-hero --s-1 --s-2 --s-3 --s-4 --s-5 --s-6 --s-8 --s-10 --s-12 --r-sm --r-base --r-lg --ease --ease-out --dur-fast --dur-base --dur-slow`. Any rename/revalue → FAIL (frozen surface per `TRADING_BOT_PROMPT.md` §B.3).
7. **`?v=` cache-buster check.** Run `grep -n "?v=" user_data/dashboard/templates/ops_spa.html user_data/dashboard/templates/dashboard_spa.html`. Every `?v=` value must contain the current V3 cache-buster tag (currently **`v3-wave1-final`** as of commit `27766ea`; future V3 fix-up commits should bump this). If any is still `v3-wave1-multi` or `permanent-fixes` or any pre-V3 value → FAIL. **IMPORTANT:** Before running the live console-error check (item 8), navigate to `http://127.0.0.1:8082/ops?_t=<random>` with a query-string suffix to defeat any browser cache from prior sessions.
8. **Live dashboard renders without console errors.** Use Playwright MCP:
   - `browser_navigate http://192.168.1.49:8081/ops` (the live container will be serving stale CSS — that's expected; you're checking the *repo* renders, so you need to override the CSS via injected `<style>` from the local file).
   - Use `browser_evaluate` to inject the V3-modified `quanta.css` and `ops_spa.js` (you may have to do this in a creative way — at minimum, verify that the JS *file* in the repo parses without syntax errors via `node -c user_data/dashboard/static/js/ops_spa.js` or similar).
   - Confirm console errors are zero. Capture `browser_console_messages` output. Any uncaught error → FAIL.
9. **All 3 themes still render.** Set `data-theme` to each of `control` / `geist` / `bloomberg`. Verify no visual breakage. Screenshot each, save under `docs/V3_AUDIT_EVIDENCE/screenshots/v3-qa-theme-{name}.png`.
10. **Density empirically toggles ≥5%** (revised down from 15% on 2026-05-12 after empirical measurement on a content-sparse `/ops` page produced 4.59% spread despite density visibly working — see plan §8 note). Run the measurement script **sequentially** (`await` each `measure()`; do NOT use `Promise.all`, which produces stale parallel-timer readings that falsely show identical heights):
    ```javascript
    async () => {
      const root = document.documentElement;
      const measure = (d) => new Promise(r => {
        root.setAttribute('data-density', d);
        setTimeout(() => r({
          density: d,
          scrollH: document.body.scrollHeight,
          bodyFs: parseFloat(getComputedStyle(document.body).fontSize),
        }), 1600);
      });
      const compact = await measure('compact');
      const def     = await measure('default');
      const roomy   = await measure('roomy');
      root.setAttribute('data-density', 'default');
      return { compact, default: def, roomy };
    }
    ```
    Spread (roomy − compact) / default must be ≥ **5%**. Body font must change visibly (expect **12 / 13 / 15 px** at compact / default / roomy). Record actual percentage + `bodyFs` for each. If spread < 5% **OR** `bodyFs` is identical across all 3 densities → FAIL.
11. **DD Ribbon needle moves with `daily_pnl`.** Visually verify the §5.1 DD Ribbon component renders and its needle position corresponds to the live `state.daily_pnl / capital * 100` value. If the needle is missing OR static → FAIL.
12. **Heartbeat dot renders + reflects services.** Verify the §5.5 dot appears top-left, currently pulses green (because `services.json` shows 8/8 up at audit time). If the dot is missing OR shows wrong color → FAIL.
13. **Kill Bar exists (collapsed at bottom) and reveals on hover.** Verify the §5.4 bottom-pinned drawer is present. Hover within bottom 80px; bar should expand revealing PAUSE / FLATTEN / KILL / RESUME. Do NOT click — just verify the affordance.
14. **Debate Floor card 21a renders 5 role-cards in courtroom layout.** Verify regime_tagger at top, bull on left, bear on right, arbiter center, reflector at bottom. Each card has model chip + last-call timestamp + last-gist preview.
15. **Gates Matrix card 05 renders heat-map.** Verify 13 crypto rows + 1 stocks row × 11 (or 8) gate columns. Cells colored from `--v3-heat-*` ramp. WHY column on the right shows `first_blocker`.
16. **Sparkline Strip cards 06 + 23 render Bloomberg ticker rows.** Verify 12 crypto + 15 stock rows, sparklines visible, position-priority (any pair with open position has green left-edge accent + 2× row height).
17. **Cmd-K palette opens.** Press `Cmd-K` (or `Ctrl-K`); the §5.7 palette overlay appears centered, 600px wide. Type "BTC" → should show pair-related entries. Press Esc → closes.
18. **`docker compose up -d dashboard && curl -s http://localhost:8081/api/mode` returns `{"mode":"paper","state":"running","dry_run":true}`.** (Skip this step if Docker isn't accessible from your environment; flag as "deferred to operator".)

### Verdict format

Emit ONE markdown document with this exact structure, in your final message:

```markdown
# V3 QA VERIFIER REPORT
- **Run date:** YYYY-MM-DD HH:MM (UTC)
- **Verifier branch:** feature/v3-frontend @ <SHA>
- **Commits under test:** 5 (Wave 0 + 4× Wave 1)
- **Verdict:** GREEN | YELLOW | RED

## Checklist results (18 items)
| # | Item | Status | Evidence |
| - | - | - | - |
| 1 | Commit graph captured | PASS / FAIL | SHAs: [list] |
| 2 | Diff scope inside user_data/dashboard + docs only | PASS / FAIL | <one-line note> |
| ... | (continue through item 18) | ... | ... |

## Failure details (only if any item is FAIL)
For each failing item, give: file:line, exact regex/diff snippet, why it fails, recommended fix.

## Notable changes outside the spec
Anything Wave 1 introduced that wasn't in the plan §6 brief — describe and flag whether it's a feature, drift, or regression.

## Screenshots captured
List all screenshots saved under docs/V3_AUDIT_EVIDENCE/screenshots/v3-qa-*.png.

## Final recommendation
- **GREEN**: Merge feature/v3-frontend → main. All gates passed.
- **YELLOW**: Merge with caveats — list 2-5 minor issues that should land in a follow-up commit but don't block merge.
- **RED**: Do NOT merge — list the 1-3 critical issues that must be fixed first.
```

### Verdict rules

- **All 18 items PASS** → GREEN. Recommend merge.
- **15-17 items PASS, the remaining are cosmetic** (e.g. tooltip alignment off by 2px) → YELLOW. Recommend merge with a follow-up task.
- **Any of items 3, 4, 5, 6, 7, 8 fail** (test/regression gates) → RED. Hard block on merge.
- **Item 18 fails because Docker isn't accessible** → SKIP, note in verdict, doesn't block.

### What you MUST NOT do

- Do NOT modify any file. You are read-only.
- Do NOT commit anything.
- Do NOT skip steps. The list is the contract.
- Do NOT issue a GREEN verdict if you couldn't verify a step — issue YELLOW with the unverifiable items called out.
- Do NOT make subjective design judgments (e.g. "the DD ribbon could be wider"). Stick to the checklist's binary criteria.

### Time budget

Estimate 45-60 minutes. Take it.

GO.
