#!/usr/bin/env python3
"""Compare backtest results across different parameter configurations."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from quicksand.backtest.analytics import print_report
from quicksand.backtest.engine import BacktestConfig, BacktestEngine
from quicksand.backtest.simulator import FeeModel
from quicksand.backtest.synthetic import generate_synthetic_data
from quicksand.backtest.data_loader import HistoricalData
from quicksand.utils.logging import setup_logging

import numpy as np
import pandas as pd


def run_multi_pair_backtest(
    config: BacktestConfig,
    pairs: list[dict],
    days: int = 365,
) -> dict:
    """Run backtest across multiple pairs, combining equity curves.

    Each pair runs independently with its share of capital.
    """
    engine = BacktestEngine(config)
    combined_equity = []
    all_trades = []
    per_pair_capital = config.initial_capital  # Each pair gets full capital access

    for pair_info in pairs:
        data = generate_synthetic_data(
            symbol=pair_info["symbol"],
            days=days,
            base_price=pair_info["base_price"],
            mean_funding_rate=pair_info["mean_rate"],
            funding_vol=pair_info["funding_vol"],
            seed=pair_info.get("seed", 42),
        )
        result = engine.run(data)
        all_trades.extend(result.trades)

        for point in result.equity_curve:
            combined_equity.append({
                "timestamp": point["timestamp"],
                "equity_delta": point["equity"] - config.initial_capital,
                "pair": pair_info["symbol"],
            })

    return {
        "trades": len(all_trades),
        "total_funding": sum(t.get("funding", 0) for t in all_trades),
        "total_fees": sum(t.get("fees", 0) for t in all_trades),
    }


def main():
    setup_logging(level="WARNING")

    capital = 10_000
    days = 365

    print("=" * 70)
    print("  STRATEGY COMPARISON: Funding Rate Arbitrage Optimization")
    print("=" * 70)
    print()

    # --- Baseline: single pair, conservative settings ---
    print("1. BASELINE: Single pair (BTC), conservative")
    print("   Settings: min_rate=15%, max_position=20%, max_positions=5")
    config_baseline = BacktestConfig(
        initial_capital=capital,
        min_annualized_rate=0.15,
        max_position_pct=0.20,
        max_open_positions=5,
    )
    data_btc = generate_synthetic_data("BTC/USDT", days=days, base_price=60000, seed=42)
    result_baseline = BacktestEngine(config_baseline).run(data_btc)
    print(f"   Return: {result_baseline.total_return_pct:.1f}% | "
          f"Sharpe: {result_baseline.sharpe_ratio:.2f} | "
          f"Drawdown: {result_baseline.max_drawdown_pct:.1f}% | "
          f"Trades: {result_baseline.total_trades}")
    print()

    # --- Optimization 1: Lower entry threshold ---
    print("2. LOWER THRESHOLD: min_rate=10% (catches more opportunities)")
    config_low_thresh = BacktestConfig(
        initial_capital=capital,
        min_annualized_rate=0.10,
        max_position_pct=0.20,
        max_open_positions=5,
    )
    result_low = BacktestEngine(config_low_thresh).run(data_btc)
    print(f"   Return: {result_low.total_return_pct:.1f}% | "
          f"Sharpe: {result_low.sharpe_ratio:.2f} | "
          f"Drawdown: {result_low.max_drawdown_pct:.1f}% | "
          f"Trades: {result_low.total_trades}")
    print()

    # --- Optimization 2: Larger position sizing ---
    print("3. AGGRESSIVE SIZING: max_position=35%")
    config_big = BacktestConfig(
        initial_capital=capital,
        min_annualized_rate=0.15,
        max_position_pct=0.35,
        max_open_positions=5,
    )
    result_big = BacktestEngine(config_big).run(data_btc)
    print(f"   Return: {result_big.total_return_pct:.1f}% | "
          f"Sharpe: {result_big.sharpe_ratio:.2f} | "
          f"Drawdown: {result_big.max_drawdown_pct:.1f}% | "
          f"Trades: {result_big.total_trades}")
    print()

    # --- Optimization 3: Multi-pair ---
    print("4. MULTI-PAIR: BTC + ETH + SOL (3 independent pairs)")
    pairs = [
        {"symbol": "BTC/USDT", "base_price": 60000, "mean_rate": 0.0003, "funding_vol": 0.0005, "seed": 42},
        {"symbol": "ETH/USDT", "base_price": 3000, "mean_rate": 0.00035, "funding_vol": 0.0006, "seed": 99},
        {"symbol": "SOL/USDT", "base_price": 150, "mean_rate": 0.0004, "funding_vol": 0.0008, "seed": 77},
    ]
    # Each pair gets 1/3 of capital
    per_pair_capital = capital / 3
    config_multi = BacktestConfig(
        initial_capital=per_pair_capital,
        min_annualized_rate=0.15,
        max_position_pct=0.30,
        max_open_positions=3,
    )
    multi_results = []
    for pair in pairs:
        data = generate_synthetic_data(
            pair["symbol"], days=days, base_price=pair["base_price"],
            mean_funding_rate=pair["mean_rate"], funding_vol=pair["funding_vol"],
            seed=pair["seed"],
        )
        r = BacktestEngine(config_multi).run(data)
        multi_results.append(r)

    combined_return = sum(r.final_equity - per_pair_capital for r in multi_results)
    combined_pct = combined_return / capital * 100
    combined_trades = sum(r.total_trades for r in multi_results)
    combined_funding = sum(r.total_funding_collected for r in multi_results)
    combined_fees = sum(r.total_fees_paid for r in multi_results)
    # Portfolio Sharpe approximation: average Sharpe * sqrt(N) for uncorrelated
    avg_sharpe = np.mean([r.sharpe_ratio for r in multi_results])
    portfolio_sharpe = avg_sharpe * np.sqrt(len(pairs))  # Diversification benefit
    max_dd = max(r.max_drawdown_pct for r in multi_results)

    print(f"   Return: {combined_pct:.1f}% | "
          f"Sharpe: ~{portfolio_sharpe:.2f} (diversified) | "
          f"Max DD: {max_dd:.1f}% (worst pair) | "
          f"Trades: {combined_trades}")
    for r in multi_results:
        print(f"     {r.symbol}: {r.total_return_pct:.1f}% return, Sharpe {r.sharpe_ratio:.2f}")
    print()

    # --- Optimization 4: All optimizations combined ---
    print("5. COMBINED: Multi-pair + lower threshold + aggressive sizing")
    config_combined = BacktestConfig(
        initial_capital=per_pair_capital,
        min_annualized_rate=0.10,
        max_position_pct=0.35,
        max_open_positions=3,
    )
    combined_results = []
    for pair in pairs:
        data = generate_synthetic_data(
            pair["symbol"], days=days, base_price=pair["base_price"],
            mean_funding_rate=pair["mean_rate"], funding_vol=pair["funding_vol"],
            seed=pair["seed"],
        )
        r = BacktestEngine(config_combined).run(data)
        combined_results.append(r)

    best_return = sum(r.final_equity - per_pair_capital for r in combined_results)
    best_pct = best_return / capital * 100
    best_trades = sum(r.total_trades for r in combined_results)
    best_funding = sum(r.total_funding_collected for r in combined_results)
    best_fees = sum(r.total_fees_paid for r in combined_results)
    avg_sharpe2 = np.mean([r.sharpe_ratio for r in combined_results])
    portfolio_sharpe2 = avg_sharpe2 * np.sqrt(len(pairs))
    max_dd2 = max(r.max_drawdown_pct for r in combined_results)

    print(f"   Return: {best_pct:.1f}% | "
          f"Sharpe: ~{portfolio_sharpe2:.2f} (diversified) | "
          f"Max DD: {max_dd2:.1f}% (worst pair) | "
          f"Trades: {best_trades}")
    for r in combined_results:
        print(f"     {r.symbol}: {r.total_return_pct:.1f}% return, Sharpe {r.sharpe_ratio:.2f}")
    print()

    # --- Summary ---
    print("=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    print(f"  {'Strategy':<45} {'Return':>8} {'Sharpe':>8} {'Trades':>8}")
    print(f"  {'-'*45} {'-'*8} {'-'*8} {'-'*8}")
    print(f"  {'1. Baseline (single pair, conservative)':<45} {result_baseline.total_return_pct:>7.1f}% {result_baseline.sharpe_ratio:>8.2f} {result_baseline.total_trades:>8}")
    print(f"  {'2. Lower threshold (10%)':<45} {result_low.total_return_pct:>7.1f}% {result_low.sharpe_ratio:>8.2f} {result_low.total_trades:>8}")
    print(f"  {'3. Aggressive sizing (35%)':<45} {result_big.total_return_pct:>7.1f}% {result_big.sharpe_ratio:>8.2f} {result_big.total_trades:>8}")
    print(f"  {'4. Multi-pair (BTC+ETH+SOL)':<45} {combined_pct:>7.1f}% {portfolio_sharpe:>8.2f} {combined_trades:>8}")
    print(f"  {'5. Combined (multi + low thresh + big size)':<45} {best_pct:>7.1f}% {portfolio_sharpe2:>8.2f} {best_trades:>8}")
    print()
    improvement = (best_pct / result_baseline.total_return_pct - 1) * 100
    print(f"  Best vs Baseline: +{improvement:.0f}% improvement")
    print()


if __name__ == "__main__":
    main()
