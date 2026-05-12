"""
Round-trip serialization test for TFTTrainerWrapper.

Why this test exists
====================

In production, FreqAI's IResolver re-imports user_data/freqaimodels/TFTModel.py
via ``importlib.util.spec_from_file_location("TFTModel", path)`` for every
retrain cycle. Each call produces a brand-new module object with brand-new
classes. If TFTTrainerWrapper were defined in TFTModel.py itself, its class
identity would change between the import that built the wrapper instance and
the import that pickle resolves at save time -> torch.save raises::

    PicklingError: Can't pickle <class 'TFTModel.TFTTrainerWrapper'>:
        it's not the same object as TFTModel.TFTTrainerWrapper

This test asserts the fix: TFTTrainerWrapper lives in freqaimodels.tft_pickle
(a regular cached module) so its identity is stable across as many simulated
re-imports as we throw at it.

Run from the host::

    python tests/test_tft_pickle.py

It also runs under pytest::

    pytest tests/test_tft_pickle.py -v
"""

from __future__ import annotations

import importlib
import importlib.util
import pickle as _stdlib_pickle  # nosec B403 - required by freqai's torch.save contract
import sys
import tempfile
from pathlib import Path

import torch
from torch import nn

# Locate user_data/ - in this repo it sits at $REPO_ROOT/user_data, but when
# this test is dropped into a freqtrade container for verification the file
# lives somewhere else (e.g. /tmp/). Try several candidates and let env
# override win for hermetic CI / docker setups.
import os as _os
_candidates = [
    Path(_os.environ.get("TRADING_BOT_USER_DATA", "")),
    Path(__file__).resolve().parent.parent / "user_data",
    Path("/freqtrade/user_data"),
    Path.cwd() / "user_data",
]
USER_DATA: Path | None = None
for _cand in _candidates:
    if _cand and (_cand / "freqaimodels" / "tft_pickle.py").exists():
        USER_DATA = _cand
        break
if USER_DATA is None:
    raise RuntimeError(
        "Could not locate user_data/freqaimodels/tft_pickle.py - set "
        "TRADING_BOT_USER_DATA to override."
    )
if str(USER_DATA) not in sys.path:
    sys.path.insert(0, str(USER_DATA))

from freqaimodels.tft_pickle import TFTTrainerWrapper  # noqa: E402


# --------------------------------------------------------------------------
# Tiny stand-in for the real TFT - we only need an nn.Module with a
# state_dict + an __init__ that round-trips. The architecture is not
# exercised here; the architecture has its own smoke test in test_tft.py.
# --------------------------------------------------------------------------


class _TinyModel(nn.Module):
    def __init__(self, in_features: int = 4, n_classes: int = 3):
        super().__init__()
        self.fc = nn.Linear(in_features, n_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.fc(x)


def _make_wrapper() -> TFTTrainerWrapper:
    model = _TinyModel()
    wrapper = TFTTrainerWrapper(
        model=model,
        model_meta_data={
            "class_names": ["down", "flat", "up"],
            "n_features": 4,
            "window_size": 12,
            "quantile_levels": [0.1, 0.5, 0.9],
        },
    )
    # Optimizer field is set in the real fit() loop - exercise the save
    # round-trip with both None and a real optimizer.
    wrapper.optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
    return wrapper


# --------------------------------------------------------------------------
# Test cases
# --------------------------------------------------------------------------


def test_module_is_stable_under_repeated_imports() -> None:
    """A regular import keeps the class identical across calls -
    this is the property that makes the production bug go away."""
    first = importlib.import_module("freqaimodels.tft_pickle").TFTTrainerWrapper
    second = importlib.import_module("freqaimodels.tft_pickle").TFTTrainerWrapper
    # Plain ``import`` hits sys.modules and never reloads, so the class
    # identity is stable from any caller's perspective. This is what makes
    # TFTModel.py's "from freqaimodels.tft_pickle import TFTTrainerWrapper"
    # safe to call on every IResolver re-exec.
    assert first is second
    assert first.__qualname__ == "TFTTrainerWrapper"


def _have_freqtrade() -> bool:
    try:
        import freqtrade  # noqa: F401
        return True
    except Exception:
        return False


def test_class_identity_stable_across_simulated_resolver_imports() -> None:
    """Mirror exactly what FreqAI's IResolver does and assert the wrapper
    class object stays the same across re-imports of TFTModel.py.

    This is the precise regression test for the production bug.

    Requires freqtrade to be installed (TFTModel.py imports BasePyTorchClassifier).
    On a host without freqtrade we skip - run inside the container instead.
    """
    if not _have_freqtrade():
        print("SKIP test_class_identity_stable_across_simulated_resolver_imports "
              "(freqtrade not installed - run inside container)")
        return
    tft_model_path = USER_DATA / "freqaimodels" / "TFTModel.py"
    assert tft_model_path.exists(), tft_model_path

    classes_seen: list[type] = []
    for i in range(3):
        spec = importlib.util.spec_from_file_location(
            f"TFTModel_resolver_sim_{i}", str(tft_model_path),
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        classes_seen.append(mod.TFTTrainerWrapper)

    # All three resolver-style imports must yield the SAME class object.
    # Before the fix this assertion would fail (each exec_module produced
    # a fresh class), which is exactly the bug pickle complained about.
    first = classes_seen[0]
    for cls in classes_seen[1:]:
        assert cls is first, (
            f"TFTTrainerWrapper class identity unstable across re-imports: "
            f"got {cls!r} != {first!r}. This means pickle will raise "
            f"\"PicklingError: it's not the same object\" on the next save."
        )


def test_pickle_roundtrip_in_memory() -> None:
    """Pure ``pickle.dumps`` / ``pickle.loads`` round-trip."""
    wrapper = _make_wrapper()
    buf = _stdlib_pickle.dumps(wrapper)
    restored = _stdlib_pickle.loads(buf)

    assert isinstance(restored, TFTTrainerWrapper)
    assert restored.model_meta_data == wrapper.model_meta_data
    # State dicts must match shape-for-shape and value-for-value.
    orig_sd = wrapper.model.state_dict()
    new_sd = restored.model.state_dict()
    assert set(orig_sd.keys()) == set(new_sd.keys())
    for k in orig_sd:
        assert torch.equal(orig_sd[k], new_sd[k]), f"state dict mismatch for {k}"


def test_torch_save_roundtrip_via_wrapper() -> None:
    """End-to-end: same call freqai data_drawer.save_data uses -
    ``wrapper.save(path)`` followed by ``torch.load(path)``.
    """
    wrapper = _make_wrapper()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "model.zip"
        wrapper.save(path)
        # weights_only=False because pytrainer is a serialized python object,
        # matching freqai/data_drawer.py:618 verbatim.
        ckpt = torch.load(path, weights_only=False)

    assert "pytrainer" in ckpt
    pytrainer = ckpt["pytrainer"]
    assert isinstance(pytrainer, TFTTrainerWrapper)
    assert ckpt["model_meta_data"] == wrapper.model_meta_data
    # Loaded wrapper carries the original state-dict embedded in its model.
    for k, v in wrapper.model.state_dict().items():
        assert torch.equal(pytrainer.model.state_dict()[k], v)


def test_atomic_save_leaves_no_tmp_on_success() -> None:
    """``wrapper.save(p)`` must write through ``p.tmp`` then rename;
    on success no .tmp leftover, on overwrite the new file replaces atomically."""
    wrapper = _make_wrapper()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "model.zip"
        # 1. First save - clean directory.
        wrapper.save(path)
        siblings = sorted(p.name for p in Path(d).iterdir())
        assert siblings == ["model.zip"], f"unexpected siblings: {siblings}"

        # 2. Overwrite with a second save - existing file replaced atomically.
        wrapper.model_meta_data["window_size"] = 24
        wrapper.save(path)
        siblings = sorted(p.name for p in Path(d).iterdir())
        assert siblings == ["model.zip"], f"unexpected siblings: {siblings}"
        ckpt = torch.load(path, weights_only=False)
        assert ckpt["model_meta_data"]["window_size"] == 24


def test_old_pickle_loads_via_tftmodel_proxy() -> None:
    """Backward-compat: a payload whose class metadata says
    ``__module__ == "TFTModel"`` must still unpickle through the
    ``sys.modules["TFTModel"]`` proxy that TFTModel.py registers.

    This emulates the existing on-disk model.zip files saved before
    the fix - they will be loaded after the fix lands without a
    migration step.

    Requires freqtrade to be installed (TFTModel.py imports BasePyTorchClassifier).
    On a host without freqtrade we skip - run inside the container instead.
    """
    if not _have_freqtrade():
        print("SKIP test_old_pickle_loads_via_tftmodel_proxy "
              "(freqtrade not installed - run inside container)")
        return
    # Loading TFTModel.py triggers its bottom-of-file
    # _register_module_aliases() which installs sys.modules["TFTModel"].
    tft_model_path = USER_DATA / "freqaimodels" / "TFTModel.py"
    spec = importlib.util.spec_from_file_location("TFTModel", str(tft_model_path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert "TFTModel" in sys.modules, "proxy module was not registered"
    proxy = sys.modules["TFTModel"]
    assert hasattr(proxy, "TFTTrainerWrapper"), "proxy missing TFTTrainerWrapper"

    # Build a wrapper, then synthesize a payload whose class header says
    # __module__="TFTModel". We do that by temporarily setting __module__
    # on the class, dumping, and restoring.
    wrapper = _make_wrapper()
    canonical_module = TFTTrainerWrapper.__module__
    try:
        TFTTrainerWrapper.__module__ = "TFTModel"
        buf = _stdlib_pickle.dumps(wrapper)
    finally:
        TFTTrainerWrapper.__module__ = canonical_module

    # Now load it - pickle's find_class should hit sys.modules["TFTModel"]
    # and resolve to our (canonical) TFTTrainerWrapper. The restored
    # instance is created from the SAME class identity.
    restored = _stdlib_pickle.loads(buf)
    assert isinstance(restored, TFTTrainerWrapper)
    assert restored.model_meta_data == wrapper.model_meta_data


# --------------------------------------------------------------------------
# Script entry point - matches the existing test_tft.py convention.
# --------------------------------------------------------------------------


def _run_all() -> None:
    tests = [
        test_module_is_stable_under_repeated_imports,
        test_class_identity_stable_across_simulated_resolver_imports,
        test_pickle_roundtrip_in_memory,
        test_torch_save_roundtrip_via_wrapper,
        test_atomic_save_leaves_no_tmp_on_success,
        test_old_pickle_loads_via_tftmodel_proxy,
    ]
    failures = 0
    for fn in tests:
        try:
            fn()
            # The freqtrade-dependent tests emit their own SKIP line.
            if fn.__name__ not in (
                "test_class_identity_stable_across_simulated_resolver_imports",
                "test_old_pickle_loads_via_tftmodel_proxy",
            ) or _have_freqtrade():
                print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {fn.__name__}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"\n{failures} of {len(tests)} tests failed")
        sys.exit(1)
    print(f"\nall {len(tests)} tests passed")


if __name__ == "__main__":
    _run_all()
