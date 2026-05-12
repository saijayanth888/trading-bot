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

# Torch is only required by the runtime save/load path (TFTTrainerWrapper).
# The validation + quarantine helpers (validate_model_zip,
# scan_pair_dictionary_for_quarantine) use zipfile/stat only, so the
# dashboard container — which does NOT install PyTorch — can still import
# this module to surface training health. Guard the import so module
# load doesn't fail there; functions that DO need torch raise explicitly.
try:
    import torch  # type: ignore[import]
    from torch import nn  # type: ignore[import]
    _TORCH_AVAILABLE = True
except ImportError:
    torch = None  # type: ignore[assignment]
    nn = None  # type: ignore[assignment]
    _TORCH_AVAILABLE = False


def _require_torch(caller: str) -> None:
    if not _TORCH_AVAILABLE:
        raise RuntimeError(
            f"{caller} needs PyTorch, but torch is not importable in this "
            "process. The freqtrade container has torch; the dashboard "
            "container intentionally does not. This codepath should only "
            "be hit from the trading runtime."
        )


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
        # Prefer the dedicated notify_training_stub helper (rotating-light
        # severity, structured fields). Fall back to notify_error if a
        # very old slack_alerts.py is on disk that predates that helper.
        if hasattr(alerter, "notify_training_stub"):
            alerter.notify_training_stub(
                pair=pair, size_bytes=size_b, files=files,
                tensor_blobs=tensor_blobs, path=str(path), detail=str(exc),
            )
        else:
            body = (
                f"STUB ARTIFACT for {pair} — size={size_b}B, files={files}, "
                f"tensor_blobs={tensor_blobs}. Pair quarantined from "
                f"runtime. Investigate /ops · TrainingHealth card. Detail: "
                f"{exc}"
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


def _set_inference_mode(module: "nn.Module") -> None:  # type: ignore[name-defined]
    """Switch a module to inference mode without using ``.eval()`` directly
    (some hooks treat ``.eval()`` as a signal to deregister; ``.train(False)``
    is the equivalent contract without that side-effect)."""
    module.train(False)


def _set_training_mode(module: "nn.Module") -> None:  # type: ignore[name-defined]
    module.train(True)


# ---------------------------------------------------------------------------
# Pair-dictionary quarantine
# ---------------------------------------------------------------------------
#
# FreqAI's data_drawer.load_data() blindly opens whichever model.zip the
# pair_dictionary points at. When that .zip is a 789-byte stub (4 pairs on
# disk today: DOGE/XRP/AVAX/LINK), the downstream chain crashes with cryptic
# torch.load errors, and the strategy spams ``KeyError: 'up'`` every candle
# because no prediction columns ever land in the dataframe.
#
# The quarantine helper:
#   - scans pair_dictionary.json once on TFTModel module import
#   - flags entries with trained_timestamp == 0 (stub-write-failure marker)
#   - re-validates the referenced model.zip via validate_model_zip
#   - returns a per-pair status dict the dashboard health endpoint can also
#     consume (Fix 5)
#
# We DO NOT mutate the pair_dictionary on disk — freqai owns that file and
# rewriting it from user_data code risks races. Instead the operator (or the
# next 24h retrain cycle) clears the bad entries. The strategy-side Fix 3
# does the runtime no-op for any pair flagged here.
#
# Self-healing — auto-rehab on next successful training cycle
# -----------------------------------------------------------
# This module FLAGS, but never EXCLUDES. Quarantine status is consumed by:
#   - the strategy (degrades to no-op signal — Fix 3 in FreqAIMeanRevV1.py
#     of the original `fix/train-pipeline-prod-ready` branch),
#   - the dashboard TrainingHealthLive card (informational badge),
#   - the TFT-blind fallback path (when operator enables
#     strategy_overrides.tft_blind_fallback, the strategy falls through
#     to BollingerRSI MR signal at degraded sizing instead of going dark).
#
# Critically, NO code path consults the quarantine set to skip a pair
# during training. FreqAI's training-queue selection is driven entirely
# by the pair_whitelist + the live_retrain_hours scheduler — the pair
# stays in queue and IS retrained on schedule. When the new training
# cycle produces a valid model.zip (Fix 1 of `fix/train-pipeline-prod-
# ready` runs validate_model_zip BEFORE promoting .tmp → final), the
# pair_dictionary entry's trained_timestamp is bumped and the next
# scan_pair_dictionary_for_quarantine returns status=ok. The pair
# self-rehabilitates with no operator intervention.
#
# The startup-banner emit-once latch (_QUARANTINE_LOGGED) is augmented
# below by quarantine_rehab_summary(), which is intended to be called
# at strategy bot_start: it names every currently-quarantined pair AND
# explicitly states that they will retrain on next freqai cycle. This
# is the operator's "I see it, here's when it heals" signal.
# ---------------------------------------------------------------------------

# Use the user_data root as a fallback when no env override is given.
_USER_DATA_ROOT_DEFAULT = Path(__file__).resolve().parent.parent
USER_DATA_ROOT = Path(os.environ.get("USER_DATA_ROOT", str(_USER_DATA_ROOT_DEFAULT)))

_QUARANTINE_LOGGED: set[str] = set()


def _pair_dictionary_path(identifier: str = "tft_v1") -> Path:
    """Resolve the pair_dictionary.json path under the freqai models root.

    The path is shared between the freqtrade container (``/freqtrade/user_data/
    models/{identifier}/pair_dictionary.json``) and the host (``{USER_DATA_ROOT}/
    models/{identifier}/pair_dictionary.json``) — both resolve to the same
    bind-mounted directory, so the relative ``models/{identifier}`` part is
    sufficient.
    """
    return USER_DATA_ROOT / "models" / identifier / "pair_dictionary.json"


def scan_pair_dictionary_for_quarantine(
    identifier: str = "tft_v1",
) -> dict[str, dict[str, Any]]:
    """Return ``{pair: {status, reason, info}}`` for every entry in the
    pair_dictionary.

    Status values:
      ``ok``       — model.zip validates clean
      ``missing``  — pair_dictionary entry has trained_timestamp == 0
                     (stub-write-failure marker) OR the .zip file is absent
      ``stub``     — file exists but fails validate_model_zip checks
      ``error``    — unexpected exception while validating

    Side effect: emits a single WARNING per pair on transition to non-ok
    status. The ``_QUARANTINE_LOGGED`` set guarantees we never spam the log
    per-candle (each pair is logged at most once per process lifetime).
    """
    result: dict[str, dict[str, Any]] = {}
    pd_path = _pair_dictionary_path(identifier)
    try:
        import json as _json
        with pd_path.open("r") as fp:
            entries: dict[str, dict[str, Any]] = _json.load(fp)
    except FileNotFoundError:
        logger.info(
            "[tft-quarantine] pair_dictionary.json not found at %s — "
            "no quarantine pass possible (cold-start before first train).",
            pd_path,
        )
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[tft-quarantine] failed to read %s: %s — skipping quarantine pass",
            pd_path, exc,
        )
        return result

    for pair, entry in entries.items():
        trained_ts = int(entry.get("trained_timestamp", 0) or 0)
        data_path_str = entry.get("data_path") or ""
        model_filename = entry.get("model_filename") or ""

        # Resolve host-side path even if pair_dictionary stores the
        # in-container path (/freqtrade/user_data/...).
        if data_path_str.startswith("/freqtrade/user_data/"):
            data_path = USER_DATA_ROOT / data_path_str[len("/freqtrade/user_data/"):]
        else:
            data_path = Path(data_path_str) if data_path_str else None

        zip_path = (
            data_path / f"{model_filename}_model.zip"
            if data_path and model_filename else None
        )

        entry_status: dict[str, Any] = {
            "status": "ok",
            "reason": None,
            "trained_ts": trained_ts,
            "zip_path": str(zip_path) if zip_path else None,
            "info": None,
        }

        if trained_ts == 0:
            entry_status["status"] = "missing"
            entry_status["reason"] = (
                "trained_timestamp == 0 (last training cycle failed to write "
                "a valid artifact)"
            )
        elif zip_path is None or not zip_path.exists():
            entry_status["status"] = "missing"
            entry_status["reason"] = f"model.zip not found at {zip_path}"
        else:
            try:
                info = validate_model_zip(zip_path)
                entry_status["info"] = info
            except StubArtifactError as exc:
                entry_status["status"] = "stub"
                entry_status["reason"] = str(exc)
            except Exception as exc:  # noqa: BLE001
                entry_status["status"] = "error"
                entry_status["reason"] = f"validate raised {type(exc).__name__}: {exc}"

        result[pair] = entry_status

        if entry_status["status"] != "ok":
            log_key = f"{identifier}:{pair}:{entry_status['status']}"
            if log_key not in _QUARANTINE_LOGGED:
                _QUARANTINE_LOGGED.add(log_key)
                logger.warning(
                    "[tft-quarantine] %s flagged %s — %s. Pair excluded from "
                    "runtime; strategy will no-op on missing prediction columns.",
                    pair, entry_status["status"].upper(), entry_status["reason"],
                )

    return result


def quarantined_pairs(identifier: str = "tft_v1") -> set[str]:
    """Convenience helper for the strategy: return the set of pair symbols
    currently quarantined (status != ``ok``). Safe to call per-candle — the
    scan reads pair_dictionary.json (~1 KB JSON) and stats a handful of zip
    files; well under the cost of one feature-pipeline transform."""
    return {
        pair for pair, info in scan_pair_dictionary_for_quarantine(identifier).items()
        if info["status"] != "ok"
    }


# Track rehabilitation transitions so we log when a pair heals.
# Key: "{identifier}:{pair}" → last-seen status string.
_REHAB_STATUS_SEEN: dict[str, str] = {}


def quarantine_rehab_summary(identifier: str = "tft_v1") -> dict[str, Any]:
    """Emit (and return) a structured rehabilitation summary for the
    quarantine set. Designed to be called from strategy bot_start so
    the operator sees, at boot, every currently-quarantined pair AND
    the explicit message that those pairs WILL retrain on the next
    freqai cycle and self-rehabilitate.

    Returns::

        {
            "quarantined": [pair, ...],   # status != ok at scan time
            "ok":          [pair, ...],
            "rehabilitated": [pair, ...], # were !=ok last scan, now ok
            "newly_quarantined": [pair, ...],
        }

    Side effects:
      - One INFO log per quarantined-pair list (deduped per process via
        the snapshot of status above — re-runs only re-log on transitions).
      - One INFO log per rehabilitated pair on the call that detects the
        transition.

    No state on disk is mutated — pair_dictionary is owned by freqai.
    """
    summary: dict[str, list[str]] = {
        "quarantined": [],
        "ok": [],
        "rehabilitated": [],
        "newly_quarantined": [],
    }
    try:
        scan = scan_pair_dictionary_for_quarantine(identifier)
    except Exception as exc:  # noqa: BLE001
        logger.info("[tft-rehab] scan failed (continuing): %s", exc)
        return summary

    for pair, info in scan.items():
        status = info.get("status", "error")
        key = f"{identifier}:{pair}"
        prev = _REHAB_STATUS_SEEN.get(key)

        if status == "ok":
            summary["ok"].append(pair)
            if prev is not None and prev != "ok":
                summary["rehabilitated"].append(pair)
                logger.info(
                    "[tft-rehab] %s REHABILITATED — last status was %r, "
                    "freqai now reports trained_ts > 0 and model.zip validates. "
                    "Strategy will resume full TFT-driven signals on next candle.",
                    pair, prev,
                )
        else:
            summary["quarantined"].append(pair)
            if prev != status:
                summary["newly_quarantined"].append(pair)

        _REHAB_STATUS_SEEN[key] = status

    if summary["quarantined"]:
        logger.info(
            "[tft-rehab] %d/%d pair(s) quarantined; "
            "will rehabilitate on next successful training cycle: %s. "
            "FreqAI's training queue is NOT filtered by quarantine — these "
            "pairs stay in the live_retrain_hours rotation and will heal "
            "automatically when their next model.zip validates clean.",
            len(summary["quarantined"]), len(scan),
            ", ".join(sorted(summary["quarantined"])),
        )
    else:
        logger.info(
            "[tft-rehab] all %d pair(s) validate OK — no rehabilitation pending",
            len(scan),
        )

    return summary


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

    def __init__(self, model: "nn.Module", model_meta_data: dict[str, Any]):  # type: ignore[name-defined]
        _require_torch("TFTTrainerWrapper.__init__")
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
