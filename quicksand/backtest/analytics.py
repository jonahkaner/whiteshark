"""Backtest performance analytics."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class BacktestResult:
    """Complete backtest results."""

    # Config
    symbol: str
    exchange: str
    start_date: str
    end_date: str
    initial_capital: float

    # Returns
    final_equity: float = 0.0
    total_return_pct: float = 0.0
    annualized_return_pct: float = 0.0
    avg_daily_return_pct: float = 0.0

    # Risk
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_duration_days: float = 0.0
    calmar_ratio: float = 0.0  # Annual return / max drawdown

    # Trading
    total_trades: int = 0
    avg_position_duration_hours: float = 0.0
    total_funding_collected: float = 0.0
    total_fees_paid: float = 0.0
    total_slippage_cost: float = 0.0
    win_rate: float = 0.0

    # Time series
    equity_curve: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)


def compute_analytics(
    equity_curve: pd.DataFrame,
    trades: list[dict],
    initial_capital: float,
    symbol: str = "",
    exchange: str = "",
) -> BacktestResult:
    """Compute comprehensive analytics from backtest equity curve and trade log.

    Args:
        equity_curve: DataFrame with columns [timestamp, equity]
        trades: List of trade dicts with keys [entry_time, exit_time, pnl, funding, fees, slippage]
        initial_capital: Starting capital
    """
    result = BacktestResult(
        symbol=symbol,
        exchange=exchange,
        start_date=str(equity_curve["timestamp"].iloc[0]) if len(equity_curve) > 0 else "",
        end_date=str(equity_curve["timestamp"].iloc[-1]) if len(equity_curve) > 0 else "",
        initial_capital=initial_capital,
    )

    if equity_curve.empty:
        return result

    # Basic returns
    result.final_equity = equity_curve["equity"].iloc[-1]
    result.total_return_pct = (result.final_equity / initial_capital - 1) * 100

    # Time span
    days = max(1, (equity_curve["timestamp"].iloc[-1] - equity_curve["timestamp"].iloc[0]) / (24 * 3600 * 1000))
    years = days / 365.25

    # Annualized return
    if years > 0:
        result.annualized_return_pct = ((result.final_equity / initial_capital) ** (1 / years) - 1) * 100

    # Daily returns for Sharpe calculation
    equity_curve = equity_curve.copy()
    equity_curve["returns"] = equity_curve["equity"].pct_change().fillna(0)

    daily_returns = equity_curve["returns"]
    result.avg_daily_return_pct = daily_returns.mean() * 100

    # Sharpe ratio (annualized, assuming 365 trading days for crypto)
    if daily_returns.std() > 0:
        result.sharpe_ratio = (daily_returns.mean() / daily_returns.std()) * np.sqrt(365)

    # Max drawdown
    equity_series = equity_curve["equity"]
    peak = equity_series.expanding().max()
    drawdown = (equity_series - peak) / peak
    result.max_drawdown_pct = abs(drawdown.min()) * 100

    # Drawdown duration
    in_drawdown = drawdown < 0
    if in_drawdown.any():
        drawdown_groups = (~in_drawdown).cumsum()
        drawdown_lengths = in_drawdown.groupby(drawdown_groups).sum()
        if len(drawdown_lengths) > 0:
            # Each row represents roughly one funding period (8h) or one check interval
            max_length = drawdown_lengths.max()
            result.max_drawdown_duration_days = max_length * 8 / 24  # Rough estimate

    # Calmar ratio
    if result.max_drawdown_pct > 0:
        result.calmar_ratio = result.annualized_return_pct / result.max_drawdown_pct

    # Trade statistics
    result.total_trades = len(trades)
    if trades:
        pnls = [t.get("pnl", 0) for t in trades]
        result.win_rate = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        result.total_funding_collected = sum(t.get("funding", 0) for t in trades)
        result.total_fees_paid = sum(t.get("fees", 0) for t in trades)
        result.total_slippage_cost = sum(t.get("slippage", 0) for t in trades)

        durations = []
        for t in trades:
            if "entry_time" in t and "exit_time" in t:
                dur = (t["exit_time"] - t["entry_time"]) / (3600 * 1000)
                durations.append(dur)
        if durations:
            result.avg_position_duration_hours = sum(durations) / len(durations)

    result.equity_curve = equity_curve[["timestamp", "equity"]].to_dict("records")
    result.trades = trades

    return result


def print_report(result: BacktestResult) -> str:
    """Format a backtest result as a readable report."""
    lines = [
        "=" * 60,
        f"  BACKTEST REPORT: {result.symbol} on {result.exchange}",
        f"  Period: {result.start_date} → {result.end_date}",
        "=" * 60,
        "",
        "  RETURNS",
        f"    Initial Capital:      ${result.initial_capital:>12,.2f}",
        f"    Final Equity:         ${result.final_equity:>12,.2f}",
        f"    Total Return:          {result.total_return_pct:>11.2f}%",
        f"    Annualized Return:     {result.annualized_return_pct:>11.2f}%",
        f"    Avg Daily Return:      {result.avg_daily_return_pct:>11.4f}%",
        "",
        "  RISK",
        f"    Sharpe Ratio:          {result.sharpe_ratio:>11.2f}",
        f"    Max Drawdown:          {result.max_drawdown_pct:>11.2f}%",
        f"    Calmar Ratio:          {result.calmar_ratio:>11.2f}",
        "",
        "  TRADING",
        f"    Total Trades:          {result.total_trades:>11d}",
        f"    Win Rate:              {result.win_rate:>11.1f}%",
        f"    Avg Hold Time:         {result.avg_position_duration_hours:>11.1f}h",
        f"    Funding Collected:    ${result.total_funding_collected:>12,.2f}",
        f"    Fees Paid:            ${result.total_fees_paid:>12,.2f}",
        f"    Slippage Cost:        ${result.total_slippage_cost:>12,.2f}",
        "",
        "  NET PROFIT BREAKDOWN",
        f"    Funding Income:       ${result.total_funding_collected:>12,.2f}",
        f"    Execution Costs:      ${(result.total_fees_paid + result.total_slippage_cost):>12,.2f}",
        f"    Net Profit:           ${(result.final_equity - result.initial_capital):>12,.2f}",
        "=" * 60,
    ]
    return "\n".join(lines)
