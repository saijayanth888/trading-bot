# FIX-D — NumberRoll null-guard + card 02 honest subtitle

**Agent:** FIX-D
**Scope:** `user_data/dashboard/static/js/dashboard_spa.js`,
`user_data/dashboard/static/js/ops_spa.js`,
`user_data/dashboard/static/js/qc_react.js`,
`user_data/dashboard/templates/dashboard_spa.html`,
`user_data/dashboard/templates/ops_spa.html`.
**Date:** 2026-05-14

---

## C-4 — `<NumberRoll>` digit-alphabet splatter (CRIT)

### Investigation

- The widget cited as `<NumberFlow>` in the audit is actually called
  `NumberRoll` in this codebase. It lives in
  `user_data/dashboard/static/js/qc_react.js` (lines 57-131 pre-fix).
- Per-digit cells use `overflow:hidden 1em` with a `translateY(-Nem)`
  track of all 10 digits stacked vertically. When the CSS/transform
  drifts during a transitional render (e.g. value flips null → number
  mid-mount, or per-digit `prev` refs are out of sync with the digit
  count), the operator sees the raw `0 1 2 3 4 5 6 7 8 9` rolodex
  splatter instead of one digit per cell.
- The original component already had a null guard *inside* `useMemo` —
  it would produce `str = "—"` and run that one character through the
  `Digit` mapper. That collapses to one wide cell *eventually*, but the
  per-digit track structure still mounts and unmounts on every value
  transition, which is the visible splatter on the four stocks rows
  (NVDA / COIN / PLTR / MSTR uPnL) and the first-paint of the topbar
  EQUITY chip.

### Fix (component-level, single source of truth)

`qc_react.js` — split into a wrapper + impl so the early return runs
BEFORE any hooks. Hook order in `_NumberRollImpl` is now stable; null
values short-circuit to a plain dim "—" span and never mount the
digit-track structure at all.

```js
function NumberRoll(props) {
  const value = props && props.value;
  if (value == null || (typeof value === "number" && isNaN(value))) {
    return h(
      "span",
      { className: cls("num", "dim", "mono", props && props.className) },
      "—"
    );
  }
  return h(_NumberRollImpl, props);
}

function _NumberRollImpl({ value, decimals = 2, prefix = "", suffix = "", className = "" }) {
  // ... original rolodex renderer unchanged ...
}
```

Every callsite in both SPAs is now safe because the guard is at the
component boundary. Callsites surveyed (no changes needed — guard moved
upstream):

- `qc_react.js:1639` — topbar EQUITY (already had outer `equity.value != null`
  fallback; the new wrapper double-protects the first-paint flicker case).
- `dashboard_spa.js:213` — DashTopbar EQUITY (already guarded; double-safe now).
- `dashboard_spa.js:556` — Price strip hero (was `px > 0` guarded).
- `ops_spa.js:490` — Card 00 LIVE DAY P&L scoreboard hero (NO previous
  guard — fixed by wrapper).
- `ops_spa.js:1293` — Combined equity hero on /ops (NO previous guard —
  fixed by wrapper).
- `ops_spa.js:2687, 2695, 2700` — LLM "SAVED" + Ollama "calls 24h"
  (passed `Number(undef)` → NaN — fixed by wrapper).
- `ops_spa.js:2798` — Open positions table uPnL cell — the operator-
  visible splatter row. Was `t.pnl_usd != null` guarded at callsite,
  but `Number(...)` could still produce non-finite values for stocks
  rows where pnl_usd is the literal string `"—"` etc. Now fixed by
  the wrapper guard's `isNaN` check.

## H-4 — Card 02 honest subtitle (HIGH)

`dashboard_spa.js:729` — the `Card({ num: "02", … sub: "TFT · meta-agent" })`
subtitle was a Wave-D holdover. The inline label at line 741 already
correctly read "MOMENTUM CLASSIFIER" (derived from `tft.classifier`),
but the subtitle stayed on the deprecated TFT name.

Fixed by deriving the subtitle from the same source as the inline label,
so the two strings can never drift again:

```js
const classifierName = (tft.classifier ? String(tft.classifier) : "momentum")
  .toLowerCase().replace(/_/g, " ");
const cardSub = classifierName + " · meta-agent";
// ...
return h(Card, { num: "02", title: "Model view", sub: cardSub, ... });
```

When `state.tft.classifier` is "momentum_v1" the subtitle reads
"momentum v1 · meta-agent". When the producer is offline it falls
back to "momentum · meta-agent". Either way, no more "TFT" claim.

## Cache-buster bump

- `user_data/dashboard/templates/dashboard_spa.html` — `v4-cutover-015-wave-d`
  → `v4-cutover-016-fixes` (all 4 asset refs: quanta.css, qc_react.js,
  components.js, dashboard_spa.js).
- `user_data/dashboard/templates/ops_spa.html` — `v4-cutover-012` →
  `v4-cutover-016-fixes` (qc_react.js + ops_spa.js refs). Bumped because
  the qc_react.js NumberRoll fix must reach the /ops route too — the
  operator-visible splatter was on /ops card 08.

## Hot-deploy + verification

Syntax check (used node's parse-as-function pattern):

```
node -e "new ...(require('fs').readFileSync('user_data/dashboard/static/js/dashboard_spa.js','utf8')); console.log('OK')"
# → dashboard_spa OK / ops_spa OK / qc_react OK
```

Deploy:

```
docker cp user_data/dashboard/static/js/dashboard_spa.js dashboard:/app/dashboard/static/js/dashboard_spa.js
docker cp user_data/dashboard/static/js/ops_spa.js     dashboard:/app/dashboard/static/js/ops_spa.js
docker cp user_data/dashboard/static/js/qc_react.js    dashboard:/app/dashboard/static/js/qc_react.js
docker cp user_data/dashboard/templates/dashboard_spa.html dashboard:/app/dashboard/templates/dashboard_spa.html
docker cp user_data/dashboard/templates/ops_spa.html       dashboard:/app/dashboard/templates/ops_spa.html
# → HOT-DEPLOY COMPLETE
```

Served-file verification:

```
curl -sS http://127.0.0.1:8081/static/js/qc_react.js | grep -c 'value == null'
# → 2 (wrapper guard + in-impl useMemo)
curl -sS http://127.0.0.1:8081/static/js/qc_react.js | grep -n "_NumberRollImpl\|function NumberRoll"
# → 91:  function NumberRoll(props) {
#    100:    return h(_NumberRollImpl, props);
#    103:  function _NumberRollImpl({ value, decimals = 2, prefix = "", suffix = "", className = "" }) {
curl -sS http://127.0.0.1:8081/static/js/dashboard_spa.js | grep -n "classifierName\|cardSub"
# → 734:    const classifierName = (tft.classifier ? String(tft.classifier) : "momentum")
#    736:    const cardSub = classifierName + " · meta-agent";
#    739:      num: "02", title: "Model view", sub: cardSub,
curl -sS http://127.0.0.1:8081/    | grep -o "v4-cutover-[a-z0-9-]*" | sort -u
# → v4-cutover-016-fixes
curl -sS http://127.0.0.1:8081/ops | grep -o "v4-cutover-[a-z0-9-]*" | sort -u
# → v4-cutover-016-fixes
```

## Operator-visible outcome

- Stocks Open-positions uPnL cells (NVDA / COIN / PLTR / MSTR · SHORT_PUT)
  now render `—` instead of `$ 0 1 2 3 4 5 6 7 8 9 0 1 …` when no
  mark-to-market is available.
- Topbar EQUITY first-paint no longer flickers through the digit
  alphabet — null state collapses to `—`.
- Card 02 subtitle now reads `momentum · meta-agent` (or whatever
  `state.tft.classifier` reports), matching the inline `MOMENTUM
  CLASSIFIER · 5–30 MIN HORIZON` label.

The mark-to-market data drought (C-5) is a separate fix; FIX-D only
makes the empty cells honest instead of theatrically broken.
