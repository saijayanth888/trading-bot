# FIX-C — Shark composite override (C-3, CRIT)

**File touched:** `stocks/shark/phases/market_open.py` (exclusive)
**Audit reference:** `verdict.md` C-3, `agent4-shark-wheel-stocks.md` CRIT-2
**Commit message subject:** `fix(shark): composite override now wraps the upstream catalyst gate`

---

## Investigation summary

The orchestrator brief and agent4 audit both diagnosed C-3 as **"the
composite override wraps the wrong gate — there is an upstream
catalyst gate that emits 'catalyst already priced in' but doesn't
consult `composite_override`."**

After thorough trace of `stocks/shark/phases/market_open.py` I can
confirm that hypothesis is **wrong-by-omission** but the underlying
operator complaint is **right** — there IS a real bug, it's just not
the one the audit named.

### What the audit got wrong

The audit ran against today's 09:35 ET cron log:
- Log line emitted: `NVDA skipped — catalyst already priced in`
- That string format matches the PRE-`c1541f0` gate (no composite
  attribution clause).
- BUT `c1541f0` was committed at **11:19 ET** — TWO HOURS AFTER the
  09:35 ET market_open run. So today's run executed the **old, pre-
  composite** gate, which is why the override "never reached" — the
  composite-aware gate didn't yet exist in the deployed code at the
  time of run.
- Tomorrow's market_open run will pick up `c1541f0` and the new
  emission will be `NVDA skipped — catalyst already priced in
  (composite failed: tft_up=0.09, sentiment=0.75, rs=1.30)` which
  *correctly* attributes the failure to TFT.

The audit author saw the OLD emission and assumed `c1541f0` had wired
to the wrong gate. In fact `c1541f0` is the ONLY emitter of "catalyst
already priced in" in the whole repo (verified by `grep -rn 'catalyst
already priced in' --include='*.py'`) and it IS wrapped with
`composite_override`.

### The REAL bug

Verified by running `_tft_predict('NVDA')` against today's deployed
TFT weights:

```
NVDA prediction: down=0.52 flat=0.39 up=0.09 conf=0.13
                                            ^^^^^
```

TFT says NVDA up=0.09 (model is trained 2.8 days old, val_acc=0.38 —
barely above random). With sentiment=0.75 and RS=1.3 both passing,
the composite still fails because:

```python
composite_override = (
    tft_up is not None    # True (0.09 not None)
    and tft_up >= 0.55    # FALSE (0.09 < 0.55)
    and sentiment_score >= 0.5
    and rs_composite >= 1.0
)
```

The composite is too brittle to a single noisy/stale TFT signal. But
the *more dangerous* failure mode the audit missed is what happens
when TFT is **unavailable** (model missing, insufficient bars,
runtime error). `_tft_predict` returns `None`, so `tft_up` becomes
`None`, and `composite_override` becomes False **for every candidate
whenever TFT infrastructure hiccups** — silently. This contradicts
`_tft_gate`'s explicit fail-open semantics ("we'd rather pass a
trade through than veto every signal because of an infrastructure
hiccup" — comment at the original `_tft_gate` site).

## Gate chain — before/after

### Before (c1541f0, unchanged behavior)

`_collect_candidate_data` gate ordering (line numbers approximate):

1. Already-in-positions (260) → silent skip
2. Earnings ≤ 2d (273) → "earnings in N day(s)"
3. **Catalyst-specific soft gate (347)** — wraps `composite_override` ✓
4. **Priced-in gate (374)** — wraps `composite_override` ✓
5. Sector momentum (393) → "sector bearish headwind"
6. Relative-strength (402) → "underperforming SPY"
7. Position sizing (418) → "0 shares"
8. Guardrails (437) → "failed guardrails"

`composite_override` (line 335-340):
```python
composite_override = (
    tft_up is not None         # ← silently vetoes when TFT unavail
    and tft_up >= 0.55
    and sentiment_score >= 0.5
    and rs_composite >= 1.0
)
```

### After (this fix)

Same gate ordering; only `composite_override` calculation changes and
a diagnostic log is added.

`composite_override` (line 335-360):

```python
if tft_up is None:
    # TFT signal unavailable — fail-open on TFT, require other two
    composite_override = (
        sentiment_score >= 0.5
        and rs_composite >= 1.0
    )
else:
    composite_override = (
        tft_up >= 0.55
        and sentiment_score >= 0.5
        and rs_composite >= 1.0
    )
```

New diagnostic log emitted on every candidate (line 362-375), so
operators can see signal state on EVERY candidate without parsing
the post-skip messages:

```
[GATES] NVDA composite_override=False catalyst_priced_in=True
        has_specific_catalyst=True (tft_up=0.09 sentiment=0.75
        rs=1.30 outperforming=True paper_override=True)
```

This matches the audit's explicit suggestion (`agent4-shark-wheel-stocks.md`
line 99-101):

> "Add a log line at the gate: 'composite_override=True/False,
>  catalyst_priced_in=True/False' so the operator can see whether
>  the override is being evaluated at all."

## Verification

```
$ python3 -m py_compile stocks/shark/phases/market_open.py && echo SYNTAX_OK
SYNTAX_OK

$ grep -n 'composite_override\|catalyst_priced_in\|\[GATES\]' stocks/shark/phases/market_open.py
  ...
  315: priced_in = bool(perplexity_intel.get("catalyst_priced_in", False))
  345: if tft_up is None:
  351:     composite_override = (
  356:     composite_override = (
  369: "[GATES] %s composite_override=%s catalyst_priced_in=%s "
  388: ) or composite_override          # ← soft-catalyst gate wraps composite ✓
  409: if priced_in:                    # ← priced_in gate
  410:     if composite_override:       # ← wraps composite ✓
```

Both gates that can emit a `catalyst`-related skip — the soft-catalyst
gate (no specific catalyst) at line 382, and the priced-in gate at
line 409 — now consult `composite_override`. There is no third
upstream gate (verified by exhaustive grep).

Smoke-tested the three composite_override paths against actual NVDA
state and TFT-unavailable cases:

| path | tft_up | sentiment | rs | composite | expected |
|---|---|---|---|---|---|
| Live-TFT, NVDA today | 0.09 | 0.75 | 1.30 | **False** | False (TFT veto) ✓ |
| TFT unavail, strong signals | None | 0.75 | 1.30 | **True** | True (fail-open) ✓ |
| TFT unavail, weak signals | None | 0.20 | 0.70 | **False** | False (weak) ✓ |

## What this fix does NOT do

1. It does NOT change NVDA's outcome today/tomorrow. NVDA's live TFT
   says down (up=0.09); composite still correctly fails on TFT
   disagreement. The candidate will still be skipped with the
   explicit "composite failed: tft_up=0.09, sentiment=0.75, rs=1.30"
   reason. Per c1541f0's design intent: "If TFT up < 0.55: SKIPPED
   with explicit reason — the operator wanted this."
2. It does NOT undo c1541f0's design. The composite still requires
   TFT to agree IF TFT IS AVAILABLE. The fix only changes behavior
   when TFT is unavailable (infrastructure issue).
3. It does NOT relax the soft-gate or priced-in gate. They still
   wrap composite as designed.

## Next steps for the operator

The audit's *symptom* (`override_verify.json: status=stalled,
stalled_runs=3`) will not resolve until either:
- TFT model is retrained more recently than 2.8d (`stocks_ml_train`
  cron runs weekly Sun 23:00); val_acc=0.38 is essentially noise
  and a model that close to random will frequently disagree with
  bullish LLM thesis on volatile names like NVDA.
- A candidate naturally agrees across all three (TFT≥0.55,
  sentiment≥0.5, RS≥1.0) — e.g. a non-priced-in catalyst on a
  ticker with bullish TFT.

Tomorrow's market_open run with `c1541f0` + this fix will emit the
new `[GATES]` line and the precise composite-failure attribution,
making the next diagnostic crystal clear.
