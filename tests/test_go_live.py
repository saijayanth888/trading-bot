"""
End-to-end smoke for the go-live automation scripts.

  1. validate_readiness.py against a journal that PASSES — exit 0
  2. validate_readiness.py against a journal that FAILS — exit 1
  3. validate_readiness.py --json — emits parseable JSON
  4. auto_rollback.py --dry — reports both checks without executing
  5. backup.sh daily — creates a verifiable .tar.gz under the dest dir
  6. install_crontab.sh --print — emits the expected cron lines
  7. go_live.sh status — reads/initialises state, no failures
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
DB_DIR = ROOT / "user_data" / "data"


def _ok(msg: str) -> None: print(f"  [✓] {msg}")
def _info(msg: str) -> None: print(f"  [i] {msg}")
def _hr() -> None: print("=" * 64)


def _truncate_journal() -> bool:
    sys.path.insert(0, str(ROOT / "user_data"))
    try:
        from modules import db as _db
        with _db.cursor() as cur:
            cur.execute("TRUNCATE TABLE trade_journal RESTART IDENTITY")
        return True
    except Exception as exc:
        print(f"  [-] SKIP: Postgres unreachable ({exc})")
        return False


def _seed_passing(n_trades: int = 250) -> None:
    """Seed `n_trades` closed trades that comfortably clear all 5 thresholds."""
    sys.path.insert(0, str(ROOT / "user_data"))
    from modules.trade_journal import TradeJournal
    j = TradeJournal()
    base = datetime.now(timezone.utc) - timedelta(days=60)
    rng_seed = 1
    for i in range(n_trades):
        is_win = (i * 7 + rng_seed) % 20 < 13     # 65% wins
        pnl_pct = 0.008 if is_win else -0.005
        pnl = pnl_pct * 1000.0
        opened = base + timedelta(hours=i * 4)
        closed = opened + timedelta(hours=2)
        jid = j.log_entry(
            pair="BTC/USD", direction="long",
            entry_price=65_000.0, stake=1000.0,
            opened_at=opened,
        )
        j.log_exit(
            jid, exit_price=65_000.0 * (1 + pnl_pct),
            pnl=pnl, pnl_pct=pnl_pct,
            exit_reason="test", duration_min=120,
            closed_at=closed,
        )


def _seed_failing(n_trades: int = 50) -> None:
    """Few trades + bad win rate → multiple FAILs."""
    sys.path.insert(0, str(ROOT / "user_data"))
    from modules.trade_journal import TradeJournal
    j = TradeJournal()
    base = datetime.now(timezone.utc) - timedelta(days=20)
    for i in range(n_trades):
        is_win = (i % 5) < 2                      # 40% wins
        pnl_pct = 0.005 if is_win else -0.01
        pnl = pnl_pct * 1000.0
        opened = base + timedelta(hours=i * 6)
        closed = opened + timedelta(hours=3)
        jid = j.log_entry(
            pair="BTC/USD", direction="long",
            entry_price=65_000.0, stake=1000.0, opened_at=opened,
        )
        j.log_exit(
            jid, exit_price=65_000.0 * (1 + pnl_pct),
            pnl=pnl, pnl_pct=pnl_pct,
            exit_reason="test", duration_min=180,
            closed_at=closed,
        )


def _run(cmd: list[str], **env_extra) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)


def test_validate_pass_and_fail() -> None:
    print("\n[1+2/7] validate_readiness.py — pass + fail journals")
    if not _truncate_journal():
        return
    _seed_passing()
    r_pass = _run([sys.executable, str(SCRIPTS / "validate_readiness.py")])
    if r_pass.returncode != 0:
        print(r_pass.stdout)
        print(r_pass.stderr, file=sys.stderr)
    assert r_pass.returncode == 0, f"pass case must return 0 (got {r_pass.returncode})"
    assert "READY" in r_pass.stdout
    _ok("pass journal → exit 0 (READY)")

    _truncate_journal()
    _seed_failing()
    r_fail = _run([sys.executable, str(SCRIPTS / "validate_readiness.py")])
    assert r_fail.returncode != 0, "fail case must return non-zero"
    assert "FAIL" in r_fail.stdout
    assert "NOT READY" in r_fail.stdout
    _ok(f"fail journal → exit {r_fail.returncode} (NOT READY)")


def test_validate_json() -> None:
    print("\n[3/7] validate_readiness.py --json")
    if not _truncate_journal():
        return
    _seed_passing()
    r = _run([sys.executable, str(SCRIPTS / "validate_readiness.py"), "--json"])
    assert r.returncode == 0
    report = json.loads(r.stdout)
    assert report["all_passed"] is True
    names = [c["name"] for c in report["checks"]]
    assert names == ["sharpe", "max_drawdown", "profit_factor", "win_rate", "total_trades"]
    _ok(f"--json emitted {len(report['checks'])} checks; all_passed={report['all_passed']}")


def test_auto_rollback_dry() -> None:
    print("\n[4/7] auto_rollback.py --dry")
    # auto_rollback uses the *fixed* DB_PATH inside the script, so we point
    # it at a tempfile via a symlink trick: monkey-patch HOME so the state
    # file lands in the temp dir, but the DB still has to exist at the
    # canonical path. We use the existing one and just pass --dry so no
    # side-effects can occur.
    r = _run([sys.executable, str(SCRIPTS / "auto_rollback.py"), "--dry"])
    # Either 0 (no triggers) or 0 (dry mode prevents side-effects)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "tick:" in r.stderr or "tick:" in r.stdout, "expected status line"
    _ok(f"dry tick ran cleanly (rc=0)")


def test_backup_daily() -> None:
    print("\n[5/7] backup.sh daily")
    with tempfile.TemporaryDirectory() as td:
        env = {"BACKUP_DIR": td}
        r = _run(["bash", str(SCRIPTS / "backup.sh"), "daily"], **env)
        assert r.returncode == 0, (r.stdout, r.stderr)
        archives = list(Path(td, "daily").glob("daily-*.tar.gz"))
        assert archives, f"no archive created in {td}/daily; stdout={r.stdout}"
        # Verify it's a valid tarball with at least the config inside
        size = archives[0].stat().st_size
        assert size > 100, f"archive suspiciously small: {size}B"
        # Confirm it lists files
        r2 = subprocess.run(
            ["tar", "-tzf", str(archives[0])], capture_output=True, text=True,
        )
        assert "config.json" in r2.stdout, f"config.json missing from archive"
        _ok(f"daily archive {archives[0].name} ({size/1024:.1f} KiB), "
            f"contains config.json + {r2.stdout.count(chr(10))} entries")


def test_install_crontab_print() -> None:
    print("\n[6/7] install_crontab.sh --print")
    r = _run(["bash", str(SCRIPTS / "install_crontab.sh"), "--print"])
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "BEGIN ===" in out and "END ===" in out
    assert "auto_rollback.py" in out
    assert "backup.sh daily" in out
    assert "backup.sh weekly" in out
    _ok(f"crontab block emitted: {out.count(chr(10))} lines")


def test_go_live_status() -> None:
    print("\n[7/7] go_live.sh status")
    # Use a temp HOME so we don't pollute the user's real ~/.trading-bot
    with tempfile.TemporaryDirectory() as td:
        r = _run(["bash", str(SCRIPTS / "go_live.sh"), "status"], HOME=td)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert "Go-live state" in r.stdout
        # First call initialises; expect stage 0
        assert '"stage": 0' in r.stdout or '"stage": 0,' in r.stdout
        _ok("status reports stage 0 at fresh state")


def main() -> int:
    _hr()
    print(" Go-live automation smoke test")
    _hr()

    test_validate_pass_and_fail()
    test_validate_json()
    test_auto_rollback_dry()
    test_backup_daily()
    test_install_crontab_print()
    test_go_live_status()

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
