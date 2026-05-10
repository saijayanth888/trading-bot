"""
wheel — cash-secured-put + covered-call income strategy on Alpaca options.

Sister to shark/ inside trading-bot/stocks/. Lives outside the shark package
because shark's CLAUDE.md hard-bans options ("NO OPTIONS. EVER.") — wheel
keeps that constraint by being a separate module.

The income thesis:
    On a 30-day pilot, sell 30-delta CSPs on liquid mid-IV stocks, profit-take
    at 50% of premium, accept assignment if it happens, then sell 30-delta
    covered calls until called away. Repeat. Realistic APR per the research:
    15-30% on deployed wheel capital. Reference: CBOE PUT Index = 9-18%/yr.

Pilot: SOFI only, $5K paper, weekly 30-delta CSPs at 7-10 DTE.
"""

__all__ = ["__version__"]
__version__ = "0.1.0-pilot"


# ── Load env from trading-bot/.env on import ─────────────────────────────
# Same shim shark.run uses: any `from wheel import ...` immediately loads
# the unified .env so ALPACA_API_KEY etc. are available without a wrapper.
def _load_unified_env() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return
    from pathlib import Path
    candidate = Path(__file__).resolve().parents[2] / ".env"
    if candidate.is_file():
        load_dotenv(candidate, override=False)


_load_unified_env()
del _load_unified_env
