"""``quanta_core.hermes.gpu_yield_adapter`` — V4 wrapper around shell scripts.

Cadence
-------
``yield_now`` — ``55 13 * * 0`` (5 min before Sunday 14:00 ET window).
``resume``    — end-of-window (manually invoked, or by post-training cron).

Per the build brief: subprocess shell-out is fine — the heavy lifting lives
in the existing scripts.  This module wraps them so the V4 scheduler can
invoke them, capture exit codes, and persist a small state record.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from quanta_core.hermes._common import (
    HermesConfig,
    SlackNotifier,
    StateWriter,
    configure_logging,
    load_config,
    utc_iso,
)

HERMES_SCRIPTS_DIR = Path.home() / ".hermes" / "scripts"
YIELD_SCRIPT = HERMES_SCRIPTS_DIR / "gpu_yield_now.sh"
RESUME_SCRIPT = HERMES_SCRIPTS_DIR / "gpu_resume.sh"


def _run_script(path: Path, timeout: float, log_args: Sequence[str] = ()) -> tuple[int, str, str]:
    if not path.exists():
        return 127, "", f"script not found: {path}"
    try:
        result = subprocess.run(
            [str(path), *log_args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        return 124, stdout, f"timeout after {timeout}s"
    except Exception as exc:  # pragma: no cover — defensive
        return 1, "", str(exc)


def yield_now(cfg: HermesConfig, timeout: float = 120.0) -> int:
    log = configure_logging("gpu_yield_adapter")
    notifier = SlackNotifier(cfg.slack_webhook_url, cfg.slack_channel)
    code, stdout, stderr = _run_script(YIELD_SCRIPT, timeout)
    log.info("gpu_yield_now exit=%d", code)
    StateWriter(cfg.state_root / "last_gpu_yield.json").write(
        {
            "ts": utc_iso(),
            "action": "yield_now",
            "exit_code": code,
            "stdout_tail": stdout[-2048:],
            "stderr_tail": stderr[-2048:],
            "script": str(YIELD_SCRIPT),
        }
    )
    if code != 0:
        notifier.post(
            f":warning: gpu_yield exit={code} — see ~/.hermes/logs/gpu_gate.log"
        )
    return code


def resume(cfg: HermesConfig, timeout: float = 60.0) -> int:
    log = configure_logging("gpu_yield_adapter")
    notifier = SlackNotifier(cfg.slack_webhook_url, cfg.slack_channel)
    code, stdout, stderr = _run_script(RESUME_SCRIPT, timeout)
    log.info("gpu_resume exit=%d", code)
    StateWriter(cfg.state_root / "last_gpu_resume.json").write(
        {
            "ts": utc_iso(),
            "action": "resume",
            "exit_code": code,
            "stdout_tail": stdout[-2048:],
            "stderr_tail": stderr[-2048:],
            "script": str(RESUME_SCRIPT),
        }
    )
    if code != 0:
        notifier.post(
            f":warning: gpu_resume exit={code} — pre-warm may not have completed"
        )
    return code


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="quanta_core.hermes.gpu_yield_adapter",
        description="V4 wrapper around gpu_yield_now.sh / gpu_resume.sh",
    )
    parser.add_argument(
        "action",
        choices=["yield", "resume"],
        help="which shell script to invoke",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="subprocess timeout seconds",
    )
    return parser.parse_args(list(argv))


def run(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    cfg = load_config()
    if args.action == "yield":
        return yield_now(cfg, timeout=args.timeout)
    return resume(cfg, timeout=args.timeout)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
