"""Sanity check: the MIT LICENSE file is present at the repo root."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
LICENSE_PATH = REPO_ROOT / "LICENSE"


def test_license_file_exists() -> None:
    assert LICENSE_PATH.is_file(), f"LICENSE file missing at {LICENSE_PATH}"


def test_license_is_mit() -> None:
    text = LICENSE_PATH.read_text(encoding="utf-8")
    assert "MIT License" in text, "LICENSE does not contain the substring 'MIT License'"
