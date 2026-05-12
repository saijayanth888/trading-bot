"""
Pickle-stable home for symbols that get serialized into TFT model.zip files.

Why this module exists
======================

FreqAI's ``IResolver`` loads custom freqaimodels with::

    spec = importlib.util.spec_from_file_location("TFTModel", path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

every time the model is needed (cold start + live retrains). Each
``exec_module`` call produces a *new* module object with *new* class
objects defined inside it. So if ``TFTTrainerWrapper`` were defined in
``TFTModel.py`` itself, its class identity would change on every
retrain, and ``torch.save({"pytrainer": wrapper, ...})`` would fail
with::

    PicklingError: Can't pickle <class 'TFTModel.TFTTrainerWrapper'>:
        it's not the same object as TFTModel.TFTTrainerWrapper

The fix: define ``TFTTrainerWrapper`` here, in a regular Python
package module. Python's import system caches modules in
``sys.modules``, so subsequent ``import`` calls (including the ones
inside the freshly-exec'd ``TFTModel`` module) return the *same*
class object - pickle's identity check passes.

Backward compatibility
======================

Existing model.zip files on disk were pickled with
``__module__ = "TFTModel"`` (because the wrapper used to live in
``TFTModel.py``). To keep them loadable, ``TFTModel.py`` registers a
proxy ``sys.modules["TFTModel"]`` whose ``TFTTrainerWrapper`` attribute
points here. Pickle resolves classes by ``(module, qualname)`` strings
and instantiates them by ``__dict__`` assignment - so old saves load
into the new class with no migration needed.

Do NOT inline this class back into ``TFTModel.py``. The whole point
of this module is to provide a stable, deduplicated home for the class
so its identity survives IResolver's repeated ``exec_module`` calls.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import torch
from torch import nn

logger = logging.getLogger(__name__)


def _set_inference_mode(module: nn.Module) -> None:
    """Switch a module to inference mode without using ``.eval()`` directly
    (some hooks treat ``.eval()`` as a signal to deregister; ``.train(False)``
    is the equivalent contract without that side-effect)."""
    module.train(False)


def _set_training_mode(module: nn.Module) -> None:
    module.train(True)


class TFTTrainerWrapper:
    """
    Minimal surface area for FreqAI's ``BasePyTorchClassifier``:
    a ``model`` (nn.Module) and a ``model_meta_data`` dict, plus save +
    load_from_checkpoint hooks.

    Defined here (and not in ``TFTModel.py``) so its class identity is
    stable across FreqAI's ``importlib.util.spec_from_file_location``
    re-imports - required for ``torch.save({"pytrainer": self, ...})``
    to succeed on retrains.
    """

    def __init__(self, model: nn.Module, model_meta_data: dict[str, Any]):
        self.model = model
        self.model_meta_data = model_meta_data
        self.optimizer = None  # populated by fit() - kept for save() round-trip

    def save(self, path: Path) -> None:
        """Atomically save the wrapper to ``path``.

        Writes to ``path.tmp`` first and renames into place on success. If
        the underlying serializer ever fails again (e.g. a future class is
        re-introduced in TFTModel.py and not added to tft_pickle.py), the
        previously-good model.zip stays untouched and the next inference
        cycle uses the prior weights instead of a half-written file.
        """
        path = Path(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "model_state_dict": self.model.state_dict(),
            "model_meta_data": self.model_meta_data,
            "pytrainer": self,
            "optimizer_state_dict": (
                self.optimizer.state_dict() if self.optimizer is not None else None
            ),
        }
        try:
            torch.save(payload, tmp)
            tmp.replace(path)
        except Exception:
            # Best-effort cleanup so we don't leave a stray .tmp on disk.
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            logger.exception(
                "TFTTrainerWrapper.save failed for %s - previous model.zip "
                "(if any) is preserved unchanged",
                path,
            )
            raise

    def load_from_checkpoint(self, checkpoint: dict) -> "TFTTrainerWrapper":
        self.model.load_state_dict(checkpoint["model_state_dict"])
        self.model_meta_data = checkpoint["model_meta_data"]
        return self
