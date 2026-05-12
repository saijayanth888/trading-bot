"""Shared fixtures + path setup for live module tests."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow ``import quanta_core...`` without installing the package.
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
