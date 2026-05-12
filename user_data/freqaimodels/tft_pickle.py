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
import os
import zipfile
from pathlib import Path
from typing import Any

import torch
from torch import nn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stub-artifact validation
# ---------------------------------------------------------------------------
#
# Today (2026-05-12) we observed four pairs (DOGE, XRP, AVAX, LINK) on disk
# with 786-789 byte model.zip files whose contents were ONLY::
#
#     version, byteorder, .data/serialization_id  (3 files, no data.pkl,
#                                                  no tensor blobs)
#
# That shape is exactly what torch.save leaves behind when the zip writer
# context manager is entered but the pickle phase aborts (e.g. PicklingError
# on the wrapper class before commit feb2926 landed the stable-module fix).
# The old code wrote those stubs directly to the final path; we now write
# to .tmp and replace on success, but a strong post-write validation gate
# is the only thing that guarantees we NEVER promote a stub into the
# pair_dictionary again. This is the centralised contract — any future
# serialization regression trips this gate, the stub is unlinked, and
# the prior good .zip is restored from .prev-backup.
#
# Thresholds (chosen to be loose enough to never false-positive on tiny
# real models but tight enough that the documented stub shape always
# fails):
#   - size > 1 MB           (smallest real artifact observed: 28 MB)
#   - has "/data.pkl"       (always present in a real torch.save zip)
#   - tensor blobs > 0      (per-tensor binary files under /data/*)
# ---------------------------------------------------------------------------

MIN_VALID_ZIP_BYTES = 1_000_000


class StubArtifactError(RuntimeError):
    """Raised by ``validate_model_zip`` when a freshly-written model.zip is a
    stub (small size, no data.pkl, or zero tensor blobs). The caller is
    responsible for unlinking the stub and propagating this exception so the
    training cycle is skipped cleanly for the affected pair."""


def validate_model_zip(path: Path) -> dict[str, Any]:
    """Return a structured validation summary for a model.zip on disk.

    Raises :class:`StubArtifactError` if any check fails. The returned dict
    is also useful for the dashboard's training-health endpoint, which
    consumes the same checks at read time.

    Layout of a healthy torch.save zip:
      {basename}/data.pkl           — pickled object graph
      {basename}/data/0..N          — per-tensor binary blobs
      {basename}/version            — torch format version
      {basename}/byteorder          — endianness marker
      {basename}/.data/serialization_id  (>=2.0)

    A stub has only the last three.
    """
    path = Path(path)
    if not path.exists():
        raise StubArtifactError(f"{path.name} does not exist")

    size = path.stat().st_size
    info: dict[str, Any] = {
        "path": str(path),
        "size_bytes": size,
        "has_data_pkl": False,
        "tensor_blobs": 0,
        "n_files": 0,
    }

    if size <= MIN_VALID_ZIP_BYTES:
        raise StubArtifactError(
            f"{path.name} size={size}B below {MIN_VALID_ZIP_BYTES}B threshold "
            f"(stub artifact — torch.save likely failed mid-pickle)"
        )

    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile as exc:
        raise StubArtifactError(f"{path.name} is not a valid zip: {exc}") from exc

    info["n_files"] = len(names)
    info["has_data_pkl"] = any(n.endswith("/data.pkl") for n in names)
    info["tensor_blobs"] = sum(
        1 for n in names
        if "/data/" in n and not n.endswith("serialization_id")
    )

    if not info["has_data_pkl"]:
        raise StubArtifactError(
            f"{path.name} missing data.pkl ({info['n_files']} files, "
            f"{info['tensor_blobs']} tensor blobs) — stub artifact"
        )
    if info["tensor_blobs"] == 0:
        raise StubArtifactError(
            f"{path.name} has zero tensor blobs ({info['n_files']} files) "
            f"— stub artifact"
        )

    return info


# ---------------------------------------------------------------------------
# Stub-detection alert hook (best-effort Slack notify, never raises)
# ---------------------------------------------------------------------------
#
# Imported lazily so this module stays importable in test environments that
# don't have the SlackAlerter / requests stack on the path. The alert call
# itself is wrapped in a broad try/except — alerting failure must never
# block the training cycle, and the underlying RuntimeError still propagates
# unchanged so FreqAI logs + skips the pair.
# ---------------------------------------------------------------------------

_STUB_ALERT_DEDUP_DIR = Path(
    os.environ.get(
        "TFT_STUB_ALERT_DIR",
        str(Path.home() / ".hermes" / "state-snapshots"),
    )
)
_STUB_ALERT_DEDUP_WINDOW_S = 30 * 60  # 30 minutes per spec


def _maybe_emit_stub_alert(pair: str, path: Path, exc: Exception) -> None:
    """Fire a Slack alert (deduped 30 min/pair) when a stub artifact is
    detected. Failures are swallowed; the validation RuntimeError still
    propagates from the caller."""
    try:
        _STUB_ALERT_DEDUP_DIR.mkdir(parents=True, exist_ok=True)
        safe_pair = pair.replace("/", "_").replace("\\", "_") or "unknown"
        marker = _STUB_ALERT_DEDUP_DIR / f"tft_stub_alert_{safe_pair}.ts"
        import time as _time
        now = _time.time()
        if marker.exists():
            try:
                last = float(marker.read_text().strip())
            except (OSError, ValueError):
                last = 0.0
            if now - last < _STUB_ALERT_DEDUP_WINDOW_S:
                logger.debug(
                    "[tft-training] stub alert suppressed (deduped) for %s",
                    pair,
                )
                return
        marker.write_text(str(now))
    except Exception as dedup_exc:  # noqa: BLE001 — alerting must never raise
        logger.debug(
            "[tft-training] stub alert dedup bookkeeping failed: %s", dedup_exc,
        )

    try:
        # Lazy import so the rest of this module stays decoupled.
        from user_data.modules.slack_alerts import SlackAlerter  # type: ignore
    except Exception:
        try:
            # Fallback for in-container path layout where freqtrade runs
            # without the leading "user_data." package prefix.
            from modules.slack_alerts import SlackAlerter  # type: ignore
        except Exception as imp_exc:
            logger.info(
                "[tft-training] slack_alerts unavailable, stub alert skipped: %s",
                imp_exc,
            )
            return

    try:
        size_b = path.stat().st_size if path.exists() else 0
        files = 0
        tensor_blobs = 0
        if path.exists():
            try:
                with zipfile.ZipFile(path) as zf:
                    names = zf.namelist()
                files = len(names)
                tensor_blobs = sum(
                    1 for n in names
                    if "/data/" in n and not n.endswith("serialization_id")
                )
            except Exception:  # noqa: BLE001 — best-effort enrichment only
                pass

        alerter = SlackAlerter.from_env()
        body = (
            f"STUB ARTIFACT for {pair} — size={size_b}B, files={files}, "
            f"tensor_blobs={tensor_blobs}. Pair quarantined from runtime. "
            f"Investigate /ops · TrainingHealth card. Detail: {exc}"
        )
        alerter.notify_error(
            "tft-training",
            body,
            context={
                "pair": pair,
                "size_bytes": size_b,
                "files": files,
                "tensor_blobs": tensor_blobs,
                "path": str(path),
            },
        )
        logger.warning("[tft-training] Slack stub-artifact alert sent for %s", pair)
    except Exception as alert_exc:  # noqa: BLE001 — alerting must never raise
        logger.info(
            "[tft-training] slack stub alert send failed (continuing): %s",
            alert_exc,
        )


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
        """Atomically save the wrapper to ``path`` with a hard validation gate.

        Sequence:
          1. If ``path`` already exists, rename it to ``path.prev-backup`` so
             a regression can be rolled back without losing prior weights.
          2. Write payload to ``path.tmp`` (torch.save).
          3. Run :func:`validate_model_zip` against ``path.tmp`` — raises
             :class:`StubArtifactError` if the freshly-written artifact looks
             like a stub (size, no data.pkl, or zero tensor blobs).
          4. Only on validation success: ``tmp.replace(path)``.

        On ANY failure:
          - Unlink the .tmp (best-effort).
          - Restore ``path.prev-backup`` to ``path`` if it exists.
          - Re-raise so FreqAI's data_drawer logs the failure and skips the
            pair on this retrain cycle. The pair stays on its previous
            (validated) weights for the next 24h.

        Note: when this method runs under FreqAI, ``path`` is the FINAL
        destination passed by ``data_drawer.save_data`` (e.g.
        ``.../sub-train-XYZ_{ts}/cb_xyz_{ts}_model.zip``). The .tmp/.replace
        dance is OUR layer of safety on top of torch.save's own zip-writing.
        """
        path = Path(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        prev_backup = path.with_suffix(path.suffix + ".prev-backup")

        # Step 1: preserve any existing artifact under a side-name so a
        # validation failure below can roll back. Use rename (atomic on
        # POSIX) — never a copy — to avoid disk-doubling on a 90 MB model.
        had_prior = False
        if path.exists():
            try:
                # If a stale prev-backup is on disk, drop it. We only keep
                # the most-recent prior good artifact.
                if prev_backup.exists():
                    prev_backup.unlink()
                path.rename(prev_backup)
                had_prior = True
            except Exception as exc:  # noqa: BLE001
                # Non-fatal: log and proceed. Worst case we lose the prior
                # artifact only if validation later fails.
                logger.warning(
                    "TFTTrainerWrapper.save: could not stash prior artifact %s "
                    "to .prev-backup: %s",
                    path, exc,
                )

        try:
            # Payload assembly is part of the protected region: state_dict()
            # itself can raise (e.g. CUDA OOM caching, hook errors), and
            # without the rollback path firing those failures would leave the
            # pair on a deleted .zip and a stale pair_dictionary entry.
            payload = {
                "model_state_dict": self.model.state_dict(),
                "model_meta_data": self.model_meta_data,
                "pytrainer": self,
                "optimizer_state_dict": (
                    self.optimizer.state_dict() if self.optimizer is not None else None
                ),
            }
            torch.save(payload, tmp)
        except Exception:
            self._cleanup_and_restore(tmp, path, prev_backup, had_prior)
            logger.exception(
                "TFTTrainerWrapper.save: torch.save raised for %s — prior "
                "weights restored from .prev-backup (if any)",
                path,
            )
            raise

        # Step 3: validate the freshly-written .tmp BEFORE promotion.
        try:
            info = validate_model_zip(tmp)
        except StubArtifactError as exc:
            self._cleanup_and_restore(tmp, path, prev_backup, had_prior)
            pair = self.model_meta_data.get("pair") if isinstance(
                self.model_meta_data, dict
            ) else None
            pair_label = pair or path.stem
            logger.error(
                "TFTTrainerWrapper.save: validation gate REJECTED stub artifact "
                "for %s (path=%s): %s. Prior weights restored from "
                ".prev-backup (if any).",
                pair_label, path, exc,
            )
            # Fire a Slack alert (Fix 6) — deduped per pair / 30 min.
            _maybe_emit_stub_alert(pair_label, path, exc)
            raise RuntimeError(
                f"training produced stub artifact for {path.name} — {exc}"
            ) from exc

        # Step 4: promote .tmp -> final path atomically.
        try:
            tmp.replace(path)
        except Exception:
            self._cleanup_and_restore(tmp, path, prev_backup, had_prior)
            logger.exception(
                "TFTTrainerWrapper.save: atomic replace failed for %s — prior "
                "weights restored from .prev-backup (if any)",
                path,
            )
            raise

        logger.info(
            "TFTTrainerWrapper.save: %s validated OK (size=%dB, files=%d, "
            "tensor_blobs=%d)",
            path.name, info["size_bytes"], info["n_files"], info["tensor_blobs"],
        )

    @staticmethod
    def _cleanup_and_restore(
        tmp: Path, path: Path, prev_backup: Path, had_prior: bool,
    ) -> None:
        """Best-effort post-failure cleanup: drop the .tmp and put the
        prior-good artifact back at ``path`` so the runtime never sees a
        gap."""
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        if had_prior and prev_backup.exists() and not path.exists():
            try:
                prev_backup.rename(path)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "TFTTrainerWrapper: failed to restore %s from .prev-backup: %s",
                    path, exc,
                )

    def load_from_checkpoint(self, checkpoint: dict) -> "TFTTrainerWrapper":
        self.model.load_state_dict(checkpoint["model_state_dict"])
        self.model_meta_data = checkpoint["model_meta_data"]
        return self
