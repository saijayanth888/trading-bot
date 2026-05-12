# Decisions log — append-only

One line per decision. Status flips from `pending` → realized after the trade closes (handled by stage/12-reflector cron).

Format:
[date | ticker | rating | pending]
[date | ticker | rating | +X.X% | +Y.Y% alpha | <holding>]
DECISION: <thesis 1-2 lines>
REFLECTION: <2-4 sentences, filled in after close>
---
