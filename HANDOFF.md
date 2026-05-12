# Tier A — Zero-risk frontend fixes — HANDOFF

**Branch:** `fix/frontend-tier-a-zero-risk`
**Worktree:** `/home/saijayanthai/Documents/trading-bot/.claude/worktrees/agent-a16e93b25f2b1262d`
**Source audit:** `FRONTEND_AUDIT_2026-05-12.md` at repo root
**Cache-bust version:** `v=20260512-tier-a-fixes`
**Pushed:** NO (operator-review-before-push policy)
**Container rebuild needed:** NO — `user_data/dashboard/` is a mounted volume; hard-refresh the browser to pick up.

---

## Commit ladder (8 commits, all atomic)

| # | SHA | Subject |
|---|-----|---------|
| 1 | `8ce98c7` | fix(dashboard): replace --c-{up,down,warn} typo with correct tokens |
| 2 | `915c09d` | fix(dashboard): define .warn-strong CSS class for LLM latency 5-15s tier |
| 3 | `8b07194` | chore(dashboard): delete dead app.css (1333 lines, never loaded) |
| 4 | `0ac4854` | perf(dashboard): replace render-blocking @import with `<link rel=stylesheet>` |
| 5 | `463bb27` | fix(dashboard): match .anchor scroll-margin-top to topbar height (80->52px) |
| 6 | `23db199` | fix(dashboard): replace hardcoded LAN IP in sidebar footer with window.location.host |
| 7 | `acd6fa0` | a11y(dashboard): add WCAG 1.4.1 glyphs to GateDot (color + shape) |
| 8 | `9fd0de9` | chore(dashboard): cache-bust to 20260512-tier-a-fixes + lock-in tests |

---

## 1 - P0-1 - `--c-*` token typo (9 sites, all in `ops_spa.js`)

**Before:** `var(--c-up)` / `var(--c-down)` / `var(--c-warn)` - non-existent. Browsers resolved them to inherited color so gate dots, circuit-breaker tinting, backtest gate cells, LLM-modal success/error glyphs rendered uncolored.

**After:** `var(--up)` / `var(--down)` / `var(--warn)` - the actual tokens defined in `quanta.css:156-158`.

**Sites fixed:** lines 1279, 1280, 1336, 1337, 1368, 2768, 2769, 2893, 3129, 3150, 3492 in `user_data/dashboard/static/js/ops_spa.js`.

**Verification:**
```
$ grep -rn 'var(--c-up)\|var(--c-down)\|var(--c-warn)' user_data/dashboard/
(no output - zero matches)
```

No matches in `qc_react.js`, `components.js`, or `dashboard_spa.js` either.

---

## 2 - P0-2 - `.warn-strong` CSS class

**Before:** `ops_spa.js:2941` returned the string `"warn-strong"` for LLM latencies 5-15 s, but no rule existed in `quanta.css`. The cell rendered with inherited color, breaking the green / yellow / orange / red ladder the operator specified.

**After:** Added one rule next to the existing severity rules in `user_data/dashboard/static/css/quanta.css` (line 159):

```css
.warn-strong { color: var(--warn); font-weight: 600; }
```

---

## 3 - P0-3 - Delete dead `app.css`

**Before:** `user_data/dashboard/static/css/app.css` was 1,333 lines / ~41 KB; loaded by zero templates (`grep -r 'app.css' templates/` -> empty). It defined `.mode-pill`, `.ws-pill`, `.kpi-*`, `.hero`, `.ks-grid`, `.tape` etc. None of those classes are referenced by any JS or template. Shared classes (`.dim`, `.up`, `.down`, `.warn`, `.mono`, `.topbar`, `.brand-mark`, `.nav-item`, ...) are already in `quanta.css`.

**After:** `git rm user_data/dashboard/static/css/app.css`. Zero render impact.

---

## 4 - P2 - `@import` -> `<link rel=stylesheet>`

**Before:** `quanta.css:8` had `@import url('https://fonts.googleapis.com/...')`. Even with preconnect hints in the template, `@import` blocks paint until the imported sheet returns.

**After:**
- `quanta.css:8` - `@import` replaced by an explanatory comment.
- `ops_spa.html` and `dashboard_spa.html` - added `<link rel="stylesheet" href="https://fonts.googleapis.com/...">` immediately after the existing preconnect lines.

Fonts now load in parallel with `quanta.css` rather than serially after it.

---

## 5 - P2 - `scroll-margin-top` matches topbar height

**Before:** `quanta.css:404` set `.anchor { scroll-margin-top: 80px; }`; actual `.topbar` `min-height` is 52 px (`quanta.css:188`). Hash-jumps parked sections 28 px under the topbar.

**After:** `scroll-margin-top: 52px` with an inline comment pointing back to `.topbar` so the next refactor keeps them in sync.

---

## 6 - P2 - Hardcoded LAN IP in sidebar footer

**Before:** `qc_react.js:1116` - literal `'local . 192.168.1.49:8081'`. Wrong on every other host (localhost, Tailscale IP, reverse-proxy hostname).

**After:**
```js
"local . " + (typeof window !== "undefined" && window.location ? window.location.host : "")
```

Footer now mirrors the host:port the operator actually connected from.

---

## 7 - WCAG 1.4.1 - check/cross glyphs on `GateDot`

**Before:** `GateDot` (`ops_spa.js:1275`) signaled pass/fail through color alone. Operators with red/green color-vision deficiency could not distinguish passing from blocking gates. Fails WCAG 1.4.1 "Use of Color".

**After:** Color dot retained (still useful), 10 px unicode glyph rendered beside it:
- pass    -> green dot + check glyph
- fail    -> red dot + cross glyph
- unknown -> dim dot + middle-dot glyph

Added `aria-label` on the wrapper, `aria-hidden` on the inner dot+glyph spans, so AT users hear one clean status per gate. Glyph inherits the dot color so it stays coherent across the three themes.

Used at `ops_spa.js:1365` (per-pair gate strip in `EntryGatesLive`) and `ops_spa.js:2864` (backtest gates list) - both get the glyph for free.

`dashboard_spa.js` has no `GateDot`-like component, no changes there.

---

## 8 - Cache-bust + lint test

**Cache-bust:** every `?v=` on JS/CSS references in `ops_spa.html` and `dashboard_spa.html` bumped to `v=20260512-tier-a-fixes`.

**Lint test added:** `tests/test_no_legacy_color_tokens.py` - 2 cheap regression checks (run in <0.1 s; no docker, no browser):

1. `test_no_legacy_c_color_tokens_in_dashboard_js` - greps every dashboard JS file for `var(--c-(up|down|warn))`; fails with a precise file:line list if any match.
2. `test_dead_app_css_is_not_resurrected` - asserts `user_data/dashboard/static/css/app.css` does not exist on disk; tells the future agent to port rules into `quanta.css` instead.

Verified: both tests pass on the current branch.

```
tests/test_no_legacy_color_tokens.py::test_no_legacy_c_color_tokens_in_dashboard_js PASSED
tests/test_no_legacy_color_tokens.py::test_dead_app_css_is_not_resurrected PASSED
2 passed in 0.02s
```

---

## Operator verification checklist (post-merge, after hard-refresh)

1. Open `http://localhost:8081/ops` with cache disabled (Ctrl+Shift+R).
2. **"Entry gates"** card - every gate dot now shows green/red color **and** a check / cross glyph beside it.
3. **"Risk Today" -> Circuit breakers** - tripped rows tinted red, healthy rows uncolored. Border-left bar visible.
4. **"Backtest preflight"** card - gate pass/fail text shows green or red.
5. **LLM activity modal** - open it, copy a prompt; the "copied!" feedback text turns green. Errors render orange.
6. **LLM activity modal** - a call with reported latency 5-15 s now shows latency in bold orange (rather than inherited grey).
7. **Sidebar footer** - bottom-left text reads `local . <whatever-you-typed-in-the-url-bar>`, not the literal `192.168.1.49`.
8. **Hash navigation** - click any anchor link in the SPA; the section's top edge sits flush against the topbar bottom, not buried under it.
9. **DevTools -> Console** - no errors.
10. **DevTools -> Network** - `fonts.googleapis.com` request kicks off in the very first wave (in parallel with `quanta.css`), not after it.

---

## Constraints respected

- Zero behavior changes: no fetch logic, polling, state management, or `fetchOne` wrappers touched (Tier C territory).
- Zero new infra: no new dependencies, no Dockerfile changes, no migration.
- Each of the 8 changes is one atomic commit.
- Branch is local-only. Not pushed.
- `dashboard_spa.js` was inspected for an equivalent `GateDot` pattern; it has none, so it was not modified.

---

## Files touched (full list)

```
user_data/dashboard/static/js/ops_spa.js          (P0-1, WCAG glyphs)
user_data/dashboard/static/js/qc_react.js         (LAN IP)
user_data/dashboard/static/css/quanta.css         (warn-strong, @import -> comment, scroll-margin)
user_data/dashboard/static/css/app.css            (DELETED)
user_data/dashboard/templates/ops_spa.html        (font <link>, cache-bust)
user_data/dashboard/templates/dashboard_spa.html  (font <link>, cache-bust)
tests/test_no_legacy_color_tokens.py              (NEW - regression lint)
HANDOFF.md                                        (this file)
```
