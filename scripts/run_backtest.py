#!/usr/bin/env python3
"""Run a funding rate arbitrage backtest.

Usage:
    # With real exchange data (fetches from Binance):
    python scripts/run_backtest.py --exchange binance --pair BTC/USDT --days 180

    # With synthetic data (no exchange needed, for testing):
    python scripts/run_backtest.py --synthetic --days 365

    # Custom parameters:
    python scripts/run_backtest.py --synthetic --capital 50000 --min-rate 0.10 --max-position 0.25
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent.parent))

from quicksand.backtest.analytics import print_report
from quicksand.backtest.engine import BacktestConfig, BacktestEngine
from quicksand.backtest.simulator import FeeModel, SlippageModel
from quicksand.utils.logging import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run funding rate arb backtest")
    parser.add_argument("--exchange", default="binance", help="Exchange to fetch data from")
    parser.add_argument("--pair", default="BTC/USDT", help="Trading pair")
    parser.add_argument("--days", type=int, default=365, help="Days of history")
    parser.add_argument("--since", default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--capital", type=float, default=10_000, help="Initial capital (USD)")
    parser.add_argument("--min-rate", type=float, default=0.15, help="Min annualized rate to enter")
    parser.add_argument("--max-position", type=float, default=0.20, help="Max position as % of equity")
    parser.add_argument("--max-positions", type=int, default=5, help="Max simultaneous positions")
    parser.add_argument("--maker-fee", type=float, default=0.0002, help="Maker fee rate")
    parser.add_argument("--taker-fee", type=float, default=0.0005, help="Taker fee rate")
    parser.add_argument("--synthetic", action="store_true", help="Use synthetic data instead of real")
    parser.add_argument("--output-csv", default=None, help="Save trades to CSV file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return parser.parse_args()


def run_with_real_data(args: argparse.Namespace, config: BacktestConfig) -> None:
    """Run backtest with real exchange data."""
    from quicksand.backtest.data_loader import load_pair_data

    print(f"Fetching {args.days} days of data for {args.pair} from {args.exchange}...")
    data = load_pair_data(
        exchange_id=args.exchange,
        pair=args.pair,
        since=args.since,
        limit_days=args.days,
    )
    engine = BacktestEngine(config)
    result = engine.run(data)
    report = print_report(result)
    print(report)

    if args.output_csv and result.trades:
        import pandas as pd
        df = pd.DataFrame(result.trades)
        df.to_csv(args.output_csv, index=False)
        print(f"\nTrades saved to {args.output_csv}")


def run_with_synthetic_data(args: argparse.Namespace, config: BacktestConfig) -> None:
    """Run backtest with synthetic data for testing."""
    from quicksand.backtest.synthetic import generate_synthetic_data

    # Multi-pair synthetic data
    pairs_config = [
        {"symbol": "BTC/USDT", "base_price": 60000, "mean_rate": 0.0003, "funding_vol": 0.0005, "seed": 42},
        {"symbol": "ETH/USDT", "base_price": 3000, "mean_rate": 0.00035, "funding_vol": 0.0006, "seed": 99},
        {"symbol": "SOL/USDT", "base_price": 150, "mean_rate": 0.0004, "funding_vol": 0.0008, "seed": 77},
    ]

    if args.pair != "BTC/USDT":
        # Single pair mode
        print(f"Generating {args.days} days of synthetic data for {args.pair}...")
        data = generate_synthetic_data(
            symbol=args.pair,
            days=args.days,
            base_price=60000 if "BTC" in args.pair else 3000,
        )
        engine = BacktestEngine(config)
        result = engine.run(data)
    else:
        # Multi-pair mode
        print(f"Generating {args.days} days of synthetic data for {len(pairs_config)} pairs...")
        datasets = []
        for pc in pairs_config:
            datasets.append(generate_synthetic_data(
                symbol=pc["symbol"], days=args.days, base_price=pc["base_price"],
                mean_funding_rate=pc["mean_rate"], funding_vol=pc["funding_vol"],
                seed=pc["seed"],
            ))
        engine = BacktestEngine(config)
        result = engine.run_multi(datasets)

    report = print_report(result)
    print(report)

    if args.output_csv and result.trades:
        import pandas as pd
        df = pd.DataFrame(result.trades)
        df.to_csv(args.output_csv, index=False)
        print(f"\nTrades saved to {args.output_csv}")


def main() -> None:
    args = parse_args()
    setup_logging(level="DEBUG" if args.verbose else "WARNING")

    config = BacktestConfig(
        initial_capital=args.capital,
        max_position_pct=args.max_position,
        min_annualized_rate=args.min_rate,
        max_open_positions=args.max_positions,
        fee_model=FeeModel(maker_fee=args.maker_fee, taker_fee=args.taker_fee),
    )

    if args.synthetic:
        run_with_synthetic_data(args, config)
    else:
        run_with_real_data(args, config)


if __name__ == "__main__":
    main()
