#!/usr/bin/env python3
"""
Freqtrade container entrypoint.

Reads `secrets/coinbase.json` (mounted at /run/secrets/trading-bot/coinbase.json
by docker-compose) and exports its `name` and `privateKey` fields as
`FREQTRADE__EXCHANGE__KEY` and `FREQTRADE__EXCHANGE__SECRET`. Also mirrors
them as `COINBASE_API_KEY` / `COINBASE_API_SECRET` so the application-side
SDK paths still work.

Freqtrade's native env-var override pattern (`FREQTRADE__SECTION__KEY=value`)
overrides anything in config.json at startup, so this lets us ship a
config.json that has no secrets in it.

Then it execs the freqtrade binary with whatever command-line args the
container received, preserving the modified environment.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote_plus

KEY_FILE = Path(os.environ.get(
    "COINBASE_KEY_FILE",
    "/run/secrets/trading-bot/coinbase.json",
))


def _log(msg: str) -> None:
    sys.stderr.write(f"[entrypoint] {msg}\n")
    sys.stderr.flush()


def _load_coinbase_secrets(env: dict[str, str]) -> None:
    if not KEY_FILE.is_file():
        _log(f"no coinbase key file at {KEY_FILE} — relying on env-var pair if set")
        return
    try:
        data = json.loads(KEY_FILE.read_text())
    except Exception as exc:
        _log(f"could not parse {KEY_FILE}: {exc!r}")
        return
    name = str(data.get("name") or "").strip()
    priv = str(data.get("privateKey") or "").strip()
    if not name or not priv:
        _log(f"{KEY_FILE} is missing 'name' or 'privateKey'")
        return
    # Freqtrade native overrides — populate config.json[exchange] at startup
    env.setdefault("FREQTRADE__EXCHANGE__KEY", name)
    env.setdefault("FREQTRADE__EXCHANGE__SECRET", priv)
    # Application-side modules still read these
    env.setdefault("COINBASE_API_KEY", name)
    env.setdefault("COINBASE_API_SECRET", priv)
    _log(f"loaded coinbase credentials from {KEY_FILE} "
         f"(name='{name[:32]}...', priv_pem_len={len(priv)})")


def _build_freqtrade_db_url(env: dict[str, str]) -> None:
    """
    Construct FREQTRADE__DB_URL from POSTGRES_* env vars with URL-encoding
    on user + password. Avoids the trap of `@` (or any URL-unsafe char) in
    POSTGRES_PASSWORD silently corrupting the DSN.

    Caller can still set FREQTRADE__DB_URL explicitly; if it's already set
    we leave it alone.
    """
    if env.get("FREQTRADE__DB_URL"):
        _log(f"FREQTRADE__DB_URL already set, leaving alone")
        return
    user = env.get("POSTGRES_USER", "tradebot")
    password = env.get("POSTGRES_PASSWORD", "tradebot-change-me")
    host = env.get("POSTGRES_HOST", "postgres")
    port = env.get("POSTGRES_PORT", "5432")
    db_url = (
        f"postgresql+psycopg2://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/freqtrade"
    )
    env["FREQTRADE__DB_URL"] = db_url
    # Mirror to DATABASE_URL too so any application-side code that uses it
    # gets the URL-encoded version (the dashboard data_sources, scripts,
    # modules/db.py all key off DATABASE_URL when set).
    env.setdefault(
        "DATABASE_URL",
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{env.get('POSTGRES_DB', 'tradebot')}",
    )
    _log(f"built FREQTRADE__DB_URL targeting {host}:{port}/freqtrade")


def main() -> int:
    env = dict(os.environ)
    _load_coinbase_secrets(env)
    _build_freqtrade_db_url(env)

    # If invoked with bare freqtrade args ("trade", "--config", ...), prepend
    # the binary name. If the user already passed it (or python -m freqtrade),
    # leave it alone.
    argv = sys.argv[1:] or ["trade"]
    if argv[0] not in ("freqtrade", "python", "python3", "/usr/bin/python3"):
        argv = ["freqtrade"] + argv

    _log(f"exec: {' '.join(argv)}")
    os.execvpe(argv[0], argv, env)
    return 0   # unreachable


if __name__ == "__main__":
    sys.exit(main())
