"""Diagnostic — inspect cached pair_candles DataFrame for int64 leaks.

Run from inside the freqtrade container, attached to the running bot's
dataprovider, via:

    docker exec freqtrade python3 /freqtrade/user_data/scripts/inspect_int64_leak.py

Looks at every column in the cached analyzed dataframe for the four
failing pairs (XRP/USD, DOGE/USD, AVAX/USD, LINK/USD), reports column
dtype + a sample of unique cell types, and flags object-dtype columns
whose cells contain numpy.integer.
"""

from __future__ import annotations

import json
import sys

import numpy as np
import requests

PAIRS = ["XRP/USD", "DOGE/USD", "AVAX/USD", "LINK/USD"]
API = "http://127.0.0.1:8080"


def login() -> str:
    import os
    user = os.environ.get("FREQTRADE__API_SERVER__USERNAME") or os.environ.get(
        "FREQTRADE_API_USER", "freqtrader",
    )
    pw = os.environ.get("FREQTRADE__API_SERVER__PASSWORD") or os.environ.get(
        "FREQTRADE_API_PASS", "",
    )
    r = requests.post(f"{API}/api/v1/token/login", auth=(user, pw), timeout=5)
    r.raise_for_status()
    return r.json()["access_token"]


def inspect_via_rpc():
    """Use the strategy's already-loaded dataprovider via the strategy
    instance accessible from the bot worker. We can't reach in-process
    state from outside the worker, so fall back to hitting the HTTP
    endpoint and parsing the error body."""
    try:
        token = login()
    except Exception as exc:
        print(f"LOGIN FAILED: {exc}")
        return

    for pair in PAIRS:
        print(f"\n=== {pair} ===")
        for limit in (60, 50, 30, 20, 10):
            r = requests.get(
                f"{API}/api/v1/pair_candles",
                params={"pair": pair, "timeframe": "5m", "limit": limit},
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            print(f"  limit={limit}: HTTP {r.status_code}", end="")
            if r.status_code == 200:
                body = r.json()
                print(f"  rows={body.get('length')}")
                break
            else:
                body = r.text[:300]
                print(f"  body={body!r}")


def inspect_dataframe_in_process():
    """When attached as a freqtrade module via execfile-style import,
    the strategy instance's dataprovider is in-memory. Try to reach it
    through the FreqtradeBot singleton."""
    try:
        # Best-effort: look for a running freqtrade RPC singleton.
        import gc
        from freqtrade.freqtradebot import FreqtradeBot
        bots = [o for o in gc.get_objects() if isinstance(o, FreqtradeBot)]
    except Exception as exc:
        print(f"[in-process] import failed: {exc}")
        return
    if not bots:
        print("[in-process] no FreqtradeBot instance found in process")
        return
    bot = bots[0]
    dp = bot.dataprovider
    for pair in PAIRS:
        print(f"\n--- {pair} dataprovider snapshot ---")
        try:
            df, last_analyzed = dp.get_analyzed_dataframe(pair, "5m")
        except Exception as exc:
            print(f"  get_analyzed_dataframe failed: {exc}")
            continue
        if df is None or df.empty:
            print("  empty")
            continue
        print(f"  rows={len(df)}  last_analyzed={last_analyzed}")
        # Per-column report
        for col in df.columns:
            dt = df[col].dtype
            if dt == "object":
                # Look at unique cell types
                types = set()
                int64_count = 0
                for v in df[col].iloc[-60:].to_numpy():
                    types.add(type(v).__name__)
                    if isinstance(v, np.integer):
                        int64_count += 1
                if int64_count > 0 or any("int" in t for t in types) or any("float" in t for t in types):
                    print(f"  [OBJECT] {col}: types={types}  np.integer_cells={int64_count}")
            elif "int" in str(dt) and "Int64" not in str(dt):
                print(f"  [INT64-DTYPE] {col}: dtype={dt}")


if __name__ == "__main__":
    print("=== HTTP probe ===")
    inspect_via_rpc()
    print("\n=== In-process probe (only works when imported by freqtrade) ===")
    inspect_dataframe_in_process()
