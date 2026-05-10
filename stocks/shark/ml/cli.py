"""
Stocks ML CLI.

Usage:
    python -m shark.ml.cli train_tft [--epochs N] [--max-samples N]
    python -m shark.ml.cli infer SYMBOL
    python -m shark.ml.cli ept_generation
    python -m shark.ml.cli train_drl    (no-op scaffold)

Designed to run from inside stocks/ with the shark venv active.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    p = argparse.ArgumentParser(prog="shark.ml.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_train = sub.add_parser("train_tft", help="Train the TFT predictor on kb/historical_bars/")
    p_train.add_argument("--epochs", type=int, default=25)
    p_train.add_argument("--max-samples", type=int, default=None,
                         help="cap on training samples (for quick iteration)")
    p_train.add_argument("--tickers", default=None,
                         help="comma-separated ticker subset (default: all)")

    p_infer = sub.add_parser("infer", help="Run inference for a single symbol")
    p_infer.add_argument("symbol")

    sub.add_parser("ept_generation", help="Record one EPT generation row")
    sub.add_parser("train_drl", help="Placeholder — DRL ensemble training not implemented yet")

    args = p.parse_args(argv)

    # Make sure stocks/ is on sys.path so `from shark.ml...` resolves
    here = Path(__file__).resolve()
    stocks_root = here.parents[2]
    if str(stocks_root) not in sys.path:
        sys.path.insert(0, str(stocks_root))

    if args.cmd == "train_tft":
        return _cmd_train_tft(args)
    if args.cmd == "infer":
        return _cmd_infer(args)
    if args.cmd == "ept_generation":
        return _cmd_ept_generation(args)
    if args.cmd == "train_drl":
        return _cmd_train_drl(args)
    return 1


def _cmd_train_tft(args) -> int:
    from shark.ml.tft_stock import train, TFTStockConfig
    here = Path(__file__).resolve()
    kb_dir = here.parents[2] / "kb" / "historical_bars"
    cfg = TFTStockConfig(epochs=int(args.epochs))
    tickers = (
        [t.strip() for t in args.tickers.split(",") if t.strip()]
        if args.tickers else None
    )
    summary = train(
        kb_dir, cfg=cfg,
        tickers=tickers, max_train_samples=args.max_samples,
    )
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_infer(args) -> int:
    from shark.ml.tft_stock import predict_direction
    from shark.ml.dataset_stock import _load_bars_json
    from shark.ml.features_stock import build_features, FEATURE_COLS

    here = Path(__file__).resolve()
    bars_path = here.parents[2] / "kb" / "historical_bars" / f"{args.symbol.upper()}.json"
    if not bars_path.is_file():
        print(json.dumps({"error": f"no bars for {args.symbol}"}))
        return 1
    bars = _load_bars_json(bars_path)
    feats = build_features(bars)
    if len(feats) < 60:
        print(json.dumps({"error": "insufficient history"}))
        return 1
    window = feats[list(FEATURE_COLS)].iloc[-60:].values
    result = predict_direction(args.symbol.upper(), window)
    print(json.dumps(result, indent=2))
    return 0


def _cmd_ept_generation(args) -> int:
    from shark.ml.ept_evolution_stocks import run_generation
    result = run_generation()
    print(json.dumps(result, indent=2, default=str))
    return 0


def _cmd_train_drl(args) -> int:
    from shark.ml.drl_ensemble_stocks import train_drl_placeholder
    result = train_drl_placeholder()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
