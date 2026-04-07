"""Backtesting engine for funding rate arbitrage.

Replays historical funding rate and price data, simulating the strategy's
entry/exit logic with realistic fee and slippage modeling.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import pandas as pd

from quicksand.backtest.analytics import BacktestResult, compute_analytics
from quicksand.backtest.data_loader import HistoricalData
from quicksand.backtest.simulator import FeeModel, FillSimulator, SlippageModel
from quicksand.utils.logging import get_logger

log = get_logger("backtest")


@dataclass
class BacktestConfig:
    """Configuration for a backtest run."""

    initial_capital: float = 10_000.0
    max_position_pct: float = 0.20  # Max 20% of equity per position
    min_annualized_rate: float = 0.15  # 15% annualized minimum to enter
    exit_rate_threshold: float = 0.5  # Exit when rate drops to 50% of min
    max_basis_drift_pct: float = 0.02  # Exit if basis drifts > 2%
    max_open_positions: int = 5
    fee_model: FeeModel = field(default_factory=FeeModel)
    slippage_model: SlippageModel = field(default_factory=SlippageModel)


@dataclass
class SimPosition:
    """A simulated arb position during backtest."""

    id: int
    symbol: str
    amount: float
    entry_spot_price: float
    entry_perp_price: float
    entry_funding_rate: float
    entry_time: int  # timestamp ms
    direction: str  # "long_spot_short_perp" or "short_spot_long_perp"
    funding_collected: float = 0.0
    entry_fees: float = 0.0
    entry_slippage: float = 0.0

    @property
    def notional(self) -> float:
        return self.amount * self.entry_spot_price


class BacktestEngine:
    """Replays historical data to simulate funding arb strategy."""

    def __init__(self, config: BacktestConfig | None = None):
        self.config = config or BacktestConfig()
        self.simulator = FillSimulator(self.config.fee_model, self.config.slippage_model)

    def run(self, data: HistoricalData) -> BacktestResult:
        """Run backtest on historical data for a single pair.

        The simulation steps through each funding rate event (every 8h).
        At each step it:
        1. Collects funding payments on open positions
        2. Checks exit conditions (low rate, basis drift)
        3. Checks entry conditions for new positions
        4. Records equity
        """
        log.info(
            "backtest_starting",
            symbol=data.symbol,
            exchange=data.exchange,
            funding_rows=len(data.funding_rates),
            price_rows=len(data.spot_prices),
        )

        funding = data.funding_rates.copy()
        spot = data.spot_prices.copy()
        perp = data.perp_prices.copy()

        if funding.empty or spot.empty or perp.empty:
            log.warning("insufficient_data", symbol=data.symbol)
            return BacktestResult(
                symbol=data.symbol, exchange=data.exchange,
                start_date=data.start_date, end_date=data.end_date,
                initial_capital=self.config.initial_capital,
            )

        # Build price lookup: for each funding timestamp, find nearest price
        spot_prices = self._build_price_index(spot)
        perp_prices = self._build_price_index(perp)

        equity = self.config.initial_capital
        cash = equity
        positions: list[SimPosition] = []
        closed_trades: list[dict] = []
        equity_curve: list[dict] = []
        next_pos_id = 0

        for _, row in funding.iterrows():
            ts = row["timestamp"]
            rate = row["rate"]
            annualized = row["annualized"]

            spot_price = self._lookup_price(spot_prices, ts)
            perp_price = self._lookup_price(perp_prices, ts)

            if spot_price is None or perp_price is None:
                continue

            # 1. Collect funding on open positions
            for pos in positions:
                # Funding payment = position_amount * funding_rate * perp_price
                if pos.direction == "long_spot_short_perp":
                    # We are short perp: if rate > 0, longs pay us
                    payment = pos.amount * rate * perp_price
                else:
                    # We are long perp: if rate < 0, shorts pay us
                    payment = pos.amount * abs(rate) * perp_price

                pos.funding_collected += payment
                cash += payment

            # 2. Check exit conditions on open positions
            to_close = []
            for i, pos in enumerate(positions):
                should_exit = False
                exit_reason = ""

                # Rate dropped below threshold
                exit_threshold = self.config.min_annualized_rate * self.config.exit_rate_threshold
                if pos.direction == "long_spot_short_perp" and annualized < exit_threshold:
                    should_exit = True
                    exit_reason = "low_rate"
                elif pos.direction == "short_spot_long_perp" and annualized > -exit_threshold:
                    should_exit = True
                    exit_reason = "low_rate"

                # Basis drift
                entry_basis = pos.entry_spot_price - pos.entry_perp_price
                current_basis = spot_price - perp_price
                basis_drift = abs(current_basis - entry_basis) / pos.entry_spot_price
                if basis_drift > self.config.max_basis_drift_pct:
                    should_exit = True
                    exit_reason = "basis_drift"

                if should_exit:
                    # Calculate exit costs
                    exit_cost = self.simulator.exit_cost(pos.amount, spot_price, perp_price)

                    # Calculate P&L from price movement (should be ~0 for delta-neutral)
                    if pos.direction == "long_spot_short_perp":
                        spot_pnl = (spot_price - pos.entry_spot_price) * pos.amount
                        perp_pnl = (pos.entry_perp_price - perp_price) * pos.amount
                    else:
                        spot_pnl = (pos.entry_spot_price - spot_price) * pos.amount
                        perp_pnl = (perp_price - pos.entry_perp_price) * pos.amount

                    total_pnl = spot_pnl + perp_pnl + pos.funding_collected - pos.entry_fees - exit_cost
                    cash += spot_pnl + perp_pnl - exit_cost

                    closed_trades.append({
                        "symbol": pos.symbol,
                        "direction": pos.direction,
                        "entry_time": pos.entry_time,
                        "exit_time": ts,
                        "entry_rate": pos.entry_funding_rate,
                        "amount": pos.amount,
                        "notional": pos.notional,
                        "pnl": total_pnl,
                        "funding": pos.funding_collected,
                        "fees": pos.entry_fees + exit_cost,
                        "slippage": pos.entry_slippage,
                        "exit_reason": exit_reason,
                    })

                    to_close.append(i)

            # Remove closed positions (reverse order to preserve indices)
            for i in sorted(to_close, reverse=True):
                positions.pop(i)

            # 3. Check entry conditions
            if len(positions) < self.config.max_open_positions:
                can_enter = False
                direction = ""

                if annualized > self.config.min_annualized_rate:
                    can_enter = True
                    direction = "long_spot_short_perp"
                elif annualized < -self.config.min_annualized_rate:
                    can_enter = True
                    direction = "short_spot_long_perp"

                if can_enter:
                    # Position sizing: use max_position_pct of current equity
                    current_equity = cash + sum(p.funding_collected for p in positions)
                    max_notional = current_equity * self.config.max_position_pct
                    amount = max_notional / spot_price

                    # Entry costs (fees + slippage combined)
                    entry_cost = self.simulator.entry_cost(amount, spot_price, perp_price)

                    cash -= entry_cost

                    positions.append(SimPosition(
                        id=next_pos_id,
                        symbol=data.symbol,
                        amount=amount,
                        entry_spot_price=spot_price,
                        entry_perp_price=perp_price,
                        entry_funding_rate=rate,
                        entry_time=ts,
                        direction=direction,
                        entry_fees=entry_cost,
                    ))
                    next_pos_id += 1

            # 4. Record equity
            position_value = sum(p.funding_collected for p in positions)
            equity = cash + position_value
            equity_curve.append({"timestamp": ts, "equity": equity})

        # Close any remaining positions at end
        for pos in positions:
            last_spot = self._lookup_price(spot_prices, funding["timestamp"].iloc[-1])
            last_perp = self._lookup_price(perp_prices, funding["timestamp"].iloc[-1])
            if last_spot and last_perp:
                exit_cost = self.simulator.exit_cost(pos.amount, last_spot, last_perp)
                if pos.direction == "long_spot_short_perp":
                    spot_pnl = (last_spot - pos.entry_spot_price) * pos.amount
                    perp_pnl = (pos.entry_perp_price - last_perp) * pos.amount
                else:
                    spot_pnl = (pos.entry_spot_price - last_spot) * pos.amount
                    perp_pnl = (last_perp - pos.entry_perp_price) * pos.amount

                total_pnl = spot_pnl + perp_pnl + pos.funding_collected - pos.entry_fees - exit_cost
                cash += spot_pnl + perp_pnl - exit_cost

                closed_trades.append({
                    "symbol": pos.symbol,
                    "direction": pos.direction,
                    "entry_time": pos.entry_time,
                    "exit_time": funding["timestamp"].iloc[-1],
                    "entry_rate": pos.entry_funding_rate,
                    "amount": pos.amount,
                    "notional": pos.notional,
                    "pnl": total_pnl,
                    "funding": pos.funding_collected,
                    "fees": pos.entry_fees + exit_cost,
                    "slippage": 0,
                    "exit_reason": "backtest_end",
                })

        equity_df = pd.DataFrame(equity_curve)

        result = compute_analytics(
            equity_curve=equity_df,
            trades=closed_trades,
            initial_capital=self.config.initial_capital,
            symbol=data.symbol,
            exchange=data.exchange,
        )

        log.info(
            "backtest_complete",
            symbol=data.symbol,
            total_return=f"{result.total_return_pct:.2f}%",
            sharpe=f"{result.sharpe_ratio:.2f}",
            max_drawdown=f"{result.max_drawdown_pct:.2f}%",
            trades=result.total_trades,
        )

        return result

    def _build_price_index(self, df: pd.DataFrame) -> pd.Series:
        """Build a timestamp -> close price Series for fast lookups."""
        return df.set_index("timestamp")["close"]

    def _lookup_price(self, prices: pd.Series, ts: int) -> float | None:
        """Find the closest price at or before a given timestamp."""
        if prices.empty:
            return None
        # Find nearest timestamp <= ts
        valid = prices.index[prices.index <= ts]
        if valid.empty:
            valid = prices.index[prices.index >= ts]
            if valid.empty:
                return None
            return float(prices[valid[0]])
        return float(prices[valid[-1]])
