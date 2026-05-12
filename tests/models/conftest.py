"""Shared pytest fixtures for the quanta_core models test-suite."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make ``src/`` importable when pytest is invoked directly without an
# editable install (e.g. ``pytest tests/``). This mirrors what
# ``pythonpath`` in pyproject.toml does for the normal pytest CLI path
# but is also robust to invocations that pre-load conftest.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Force CPU for tests — the suite runs on dev laptops without a GPU and
# the TFT module path branches on ``device.type``.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
