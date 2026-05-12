# 12 · Weekly Publisher

> **Status:** Design · rev2-r12
> **Branch:** `feat/quanta-core-v4-rev2-r12`
> **Owner:** Quanta Core / Hermes layer
> **Cadence:** Every Friday 16:00 ET (post-close)
> **Output:** `docs/weekly/YYYY-WW.md` (one file per ISO trading week)

---

## 1 · Why this exists

Viral signal in the retail trading-bot space does **not** come from flashy demos, leaderboard screenshots, or selectively shared winners. It comes from **consistent, transparent, structurally identical publishing** over months.

Operator philosophy, verbatim:

> **Money first, viral second. Honesty is the viral.**

The Weekly Publisher enforces this at the system level:

- Every Friday at 16:00 ET, regardless of P&L, regime, or operator mood, a Markdown post is generated and atomic-written to `docs/weekly/YYYY-WW.md`.
- The template is identical for winning weeks, losing weeks, flat weeks, and weeks where the bot was offline.
- The structure makes cherry-picking mechanically impossible — to skip a bad week the operator would have to delete a file, which is a visible git operation.
- Readers compounding over N weeks see the same shape every time. That shape *is* the trust artifact.

This document defines the publisher's contract so that no future change accidentally introduces a "skip if losing" branch.

---

## 2 · Markdown template

The publisher renders the following Jinja2 template into `docs/weekly/YYYY-WW.md`. The shape is fixed; only field values vary week-to-week.

```markdown
# Quanta · Week {{ iso_year }}-{{ iso_week }} ({{ monday_date }} → {{ sunday_date }})

## Headline
- **Net P&L** · ${{ net_pnl }} ({{ net_pnl_pct }}%)
- **Drawdown** · {{ drawdown_pct }}%
- **Open positions** · {{ open_count }}
- **Mode** · {{ run_mode }}   <!-- paper | live -->

## Trades this week ({{ trade_count }})

{% for trade in trades %}
### {{ loop.index }}. {{ trade.pair }} · {{ trade.side }}
- **Entry** ${{ trade.entry_price }} @ {{ trade.entry_ts }}
- **Exit**  ${{ trade.exit_price }} @ {{ trade.exit_ts }}
- **P&L**   ${{ trade.pnl }} ({{ trade.pnl_pct }}%)
- **Hold**  {{ trade.hold_duration }}
- **Strategy** {{ trade.strategy }} · **Regime at entry** {{ trade.regime }}

<details>
<summary>Debate transcript ({{ trade.debate_turns }} turns · {{ trade.debate_consensus }})</summary>

{{ trade.debate_markdown }}

</details>

**Lessons logged by Reflector**
{% for lesson in trade.lessons %}
- {{ lesson }}
{% endfor %}
{% endfor %}

## Closed-loop telemetry
- **Reflector lessons added this week** · {{ lessons_added }}
- **LoRA adapters promoted last Sunday** · {{ adapters_promoted | join(", ") or "none" }}
- **Convergence funnel** · {{ setups_detected }} detected → {{ setups_converged }} converged → {{ setups_traded }} traded
- **Debate participation** · {{ debate_count }} debates · avg {{ avg_debate_turns }} turns · consensus rate {{ consensus_rate }}%

## Open positions
{% if open_positions %}
{% for p in open_positions %}
- **{{ p.pair }}** · {{ p.side }} · entered {{ p.entry_ts }} · {{ p.days_held }}d held
  - Thesis: {{ p.thesis }}
{% endfor %}
{% else %}
_None open at close._
{% endif %}

## Next week's universe state
- **Regime** · {{ next_regime }}
- **Sentiment composite** · {{ next_sentiment }}
- **Scheduled events** · {{ next_events | join("; ") or "none" }}

---
_Generated {{ generated_ts }} ET by `quanta_core.hermes.weekly_publisher`._
_Bot run-mode this week: **{{ run_mode }}**. {{ privacy_footer }}_
```

### Field provenance

| Field | Source |
|---|---|
| `net_pnl`, `drawdown_pct`, `open_count` | Postgres `trades` + `risk_governor_state` |
| `trades[]` (closed in week) | Postgres `trades WHERE close_date BETWEEN monday AND sunday` |
| `trade.debate_markdown` | `decisions.md` debate block, keyed by `trade_id` |
| `trade.lessons` | `reflector_lessons` table, keyed by `trade_id` |
| `lessons_added` | `reflector_lessons WHERE created_at BETWEEN ...` |
| `adapters_promoted` | `adapter_registry` Sunday promotion (last Sunday — see §3 note) |
| `setups_detected/converged/traded` | `convergence_log` table |
| `open_positions` | `trades WHERE is_open = true` snapshot at 16:00 ET Friday |
| `next_regime`, `next_sentiment` | `/api/regime`, `/api/sentiment` (rev2 endpoints) |
| `next_events` | `economic_calendar` table (Mon–Fri of next ISO week) |

---

## 3 · Auto-generation pipeline

**Module:** `quanta_core.hermes.weekly_publisher`
**Trigger:** systemd timer / cron, Friday 16:00 ET (after US equities close, before crypto weekly wind-down)
**Entrypoint:** `python -m quanta_core.hermes.weekly_publisher --week current`

### Steps (in order)

1. **Resolve week boundaries**
   ISO year + ISO week → Monday 00:00 ET, Sunday 23:59 ET. Persist to a state file `state/weekly_publisher.json` so re-runs are idempotent and replay-safe.

2. **Query Postgres ledger** for trades closed in `[monday, sunday]`. Join `decisions` (debate transcripts) and `reflector_lessons` per `trade_id`.

3. **Query Reflector** — count `reflector_lessons` rows created in the week (not necessarily tied to a trade — Reflector also logs ambient lessons).

4. **Query adapter registry** — list LoRA adapters promoted on **last Sunday** (the Sunday inside this same ISO week, i.e. the Sunday at the end of the period being reported on). Note: promotions happen Sunday night; the Friday post reports on the *previous* Sunday's promotion (the one closest before this Friday). The state file records which Sunday is being attributed.

5. **Query rev2 endpoints** for next-week universe state (`/api/regime`, `/api/sentiment`, `/api/calendar?week=next`).

6. **Run quality gates** (§6). On any failure, set `data_integrity_warning = True` and prepend the warning banner — but still continue.

7. **Render** via Jinja2 (`templates/weekly_post.md.j2`).

8. **Atomic-write** to `docs/weekly/YYYY-WW.md`:
   - Write to `docs/weekly/.YYYY-WW.md.tmp`
   - `fsync`
   - `os.rename` → final path
   - Refuse to overwrite if file already exists unless `--force` (re-runs require explicit operator intent).

9. **Slack notification** to `#quanta-weekly` with: ISO week, net P&L line, drawdown line, and a link to the rendered file on GitHub (`https://github.com/<org>/trading-bot/blob/main/docs/weekly/YYYY-WW.md`). If file not yet pushed, link to the local-path fallback.

10. **Append run record** to `state/weekly_publisher.json`:
    ```json
    {
      "iso_week": "2026-19",
      "generated_ts": "2026-05-12T16:00:14-04:00",
      "trade_count": 7,
      "data_integrity_warning": false,
      "gate_results": { "reconciliation": "pass", "reflector_daily": "pass", "risk_anchor": "pass" },
      "output_path": "docs/weekly/2026-19.md"
    }
    ```

---

## 4 · Public publishing rule

- The publisher **only writes the file into the repo working tree**. It does **not** `git add`, `git commit`, or `git push`.
- The operator's existing review workflow picks up `docs/weekly/YYYY-WW.md` as an untracked file and decides whether to include it in the next push.
- **Default expectation:** the file is included in the next push that goes out anyway. The operator does not block existing pushes just because the weekly is sensitive.
- **Hard rule:** the operator may *delay* a push, but may **not** skip a week (see §5).

This keeps the publish action one human-visible commit, which is the right amount of friction: high enough to prevent accidental disclosure, low enough that it actually happens.

---

## 5 · Anti-cherry-pick discipline

The single most corrosive failure mode for transparent publishing is selective omission. The pipeline defends against it:

1. **Mandatory file creation.** The publisher runs every Friday. Failure to produce the file fires a Slack alert and a Hermes failure record.
2. **Losing weeks publish unchanged.** Same template, same fields, same section ordering. The Headline section will simply show negative P&L. No "context paragraph" is added for bad weeks — adding apology context is itself a cherry-pick.
3. **No "skip this week" flag.** There is no CLI flag, no env var, and no config key that suppresses publishing. To skip, the operator would have to (a) disable the timer, (b) delete the file from the working tree, or (c) `.gitignore` the directory — all visible, all auditable, all reversible.
4. **Missed-week detection.** A weekly audit job lists `docs/weekly/*.md` and verifies one file per ISO week back to launch. Gaps trigger a Slack alert *and* an entry in `decisions.md`.
5. **Tone parity.** The Jinja2 template contains no conditional language ("unfortunately", "great week", etc.). Adjective-free reporting.

---

## 6 · Quality gates

Before rendering, the publisher runs three gates. **All three are advisory: failures do not block publishing. They mutate the post.**

| Gate | Check | On fail |
|---|---|---|
| `reconciliation` | Sum of week's `trades.pnl` matches broker statement delta (paper: matches synthetic broker ledger; live: matches Alpaca/IBKR EOD report). Tolerance 1¢. | Banner: ❗ broker reconciliation off by $X |
| `reflector_daily` | `reflector_lessons` has at least one row for every weekday the bot was running. | Banner: ❗ Reflector missed N days |
| `risk_anchor` | `risk_governor_state.anchor` at Friday close matches expected (run-mode-aware: paper anchor = synthetic starting equity, live anchor = real starting equity). | Banner: ❗ risk-governor anchor drift |

If any gate fails, the file is prepended with:

```markdown
> ❗ **Data integrity issue this week.** See banner(s) below before reading numbers.
> - <gate-name>: <one-line description>
```

This is the only conditional content in the template. It exists because **publishing with a warning is more honest than not publishing**.

---

## 7 · Privacy / legal

- **Paper mode (current).** No real PII, no real position sizes that map to real capital. Publish dollar amounts as-is.
- **Live mode (future).** When `run_mode = live`:
  - Replace exact position sizes with percentage-of-portfolio.
  - Replace exact P&L dollar amounts with percentage returns + a single rolled-up dollar headline.
  - Never publish account numbers, broker order IDs, or routing identifiers.
  - The `privacy_footer` Jinja variable resolves to `"Paper mode — all values shown as-is."` in paper, and `"Live mode — sizes shown as % of portfolio."` in live.
- **Debate transcripts** can quote model output verbatim but must not include any user-prompt PII (the operator's name, email, or location). The Reflector already strips these at write time; the publisher does a second-pass regex check before render.

---

## 8 · Build cost

| Item | Estimate |
|---|---|
| Jinja2 template + render code | ~0.5 day |
| Postgres queries (trades / decisions / reflector / convergence / adapter / risk) | ~0.5 day |
| Atomic-write + state file + idempotency | ~0.25 day |
| Quality gates (3) | ~0.25 day |
| Slack notification | ~0.1 day |
| systemd timer + missed-week audit job | ~0.25 day |
| Tests (golden-file render, gate behavior, idempotency, missed-week detector) | ~0.5 day |
| **Total** | **~2 dev-days** |

No new infra. Reuses existing Postgres, existing Slack webhook, existing Jinja2 dep, existing state-file pattern from other Hermes modules.

---

## 9 · Non-goals (out of scope for r12)

- Auto-pushing to GitHub.
- Cross-posting to Twitter/X, Substack, or Discord. The .md file is the canonical artifact; downstream distribution is a separate concern handled by humans or a later publisher.
- Rendering charts/images. Markdown text only. Numbers and transcripts carry the signal.
- Per-trade screenshot generation. Same reason.
- Multi-week aggregation (monthly/quarterly recaps). Future doc.
- Comment/feedback ingestion. Read-only artifact.

---

## 10 · Acceptance criteria

A reviewer can mark r12 done when:

- [ ] Friday 16:00 ET cron/timer is installed and visible in the Hermes cron table.
- [ ] A dry-run on the current week produces a valid Markdown file matching the template.
- [ ] All three quality gates have unit tests covering pass/fail branches.
- [ ] Missed-week audit job runs and alerts on a synthetically-deleted file.
- [ ] State file is appended on each run and survives re-run with `--force`.
- [ ] Slack notification posts on success with the correct link.
- [ ] Operator has confirmed in `decisions.md` that the first real Friday post landed without manual edits.

---

## 11 · References

- `docs/quanta-core-v4-rev2/04-REFLECTOR.md` — lesson schema this doc consumes
- `docs/quanta-core-v4-rev2/06-DEBATE_LOOP.md` — debate transcripts this doc embeds
- `docs/quanta-core-v4-rev2/08-ADAPTER_REGISTRY.md` — Sunday promotion source
- `docs/quanta-core-v4-rev2/09-CONVERGENCE_FUNNEL.md` — funnel metrics source
- `docs/quanta-core-v4-rev2/10-RISK_GOVERNOR.md` — anchor gate
- `docs/HERMES_CRONS_2026-05-11.md` — where the Friday 16:00 ET entry is added
