"""Regression tests for the Tier-A frontend audit (2026-05-12).

Two cheap greppy invariants that lock in the P0 fixes so a future agent
cannot silently re-introduce them:

  1. The 11 sites referencing ``var(--c-up/--c-down/--c-warn)`` were a
     typo bug: the real tokens are ``var(--up/--down/--warn)``. The
     wrong tokens resolve to nothing in CSS, leaving gate dots,
     circuit-breaker rows, backtest gate cells and LLM-modal success
     glyphs uncolored.

  2. ``user_data/dashboard/static/css/app.css`` was a 1333-line dead
     file (never linked from any template, classes never referenced in
     JS). Removing it cut ~41 KB of shipped CSS and dropped an extra
     render-blocking ``@import`` for Google Fonts. Assert it stays gone.

Both checks are pure filesystem reads — no docker, no browser.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = REPO_ROOT / "user_data" / "dashboard"
JS_DIR = DASHBOARD_DIR / "static" / "js"
CSS_DIR = DASHBOARD_DIR / "static" / "css"

# Matches var(--c-up), var(--c-down), var(--c-warn). The audit showed
# 11 sites in ops_spa.js; the assertion is now zero across all
# dashboard JS so the typo cannot regress in any file.
_LEGACY_TOKEN_RE = re.compile(r"var\(--c-(?:up|down|warn)\)")


def _iter_js_files() -> list[Path]:
    assert JS_DIR.is_dir(), f"missing dashboard JS dir: {JS_DIR}"
    return sorted(p for p in JS_DIR.glob("*.js") if p.is_file())


def test_no_legacy_c_color_tokens_in_dashboard_js() -> None:
    """No JS file may reference the legacy ``--c-*`` color aliases."""
    offenders: list[str] = []
    for path in _iter_js_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _LEGACY_TOKEN_RE.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Legacy --c-{up,down,warn} CSS tokens reintroduced. "
        "Use --up / --down / --warn (defined in quanta.css). Sites:\n  "
        + "\n  ".join(offenders)
    )


def test_dead_app_css_is_not_resurrected() -> None:
    """``app.css`` was deleted in Tier A; do not recreate it."""
    dead = CSS_DIR / "app.css"
    assert not dead.exists(), (
        f"{dead.relative_to(REPO_ROOT)} reappeared on disk. "
        "It was deleted in the Tier-A frontend audit cleanup because "
        "no template links to it and no JS references its classes. "
        "If you really need its rules, port them into quanta.css "
        "instead of resurrecting app.css."
    )
