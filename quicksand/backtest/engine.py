"""Backtesting engine for funding rate arbitrage.

Replays historical funding rate and price data, simulating the strategy's
entry/exit logic with realistic fee and slippage modeling.

Supports:
- Single-pair and multi-pair backtesting
- Kelly criterion dynamic position sizing
- Concurrent position management across pairs
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from quicksand.backtest.analytics import BacktestResult, compute_analytics
from quicksand.backtest.data_loader import HistoricalData
from quicksand.backtest.simulator import FeeModel, FillSimulator, SlippageModel
from quicksand.strategies.funding_arb import kelly_position_size
from quicksand.utils.logging import get_logger

log = get_logger("backtest")


@dataclass
class BacktestConfig:
    """Configuration for a backtest run."""

    initial_capital: float = 10_000.0
    max_position_pct: float = 0.35  # Max per-position cap
    min_annualized_rate: float = 0.15  # 15% annualized minimum to enter
    exit_rate_threshold: float = 0.5  # Exit when rate drops to 50% of min
    max_basis_drift_pct: float = 0.02  # Exit if basis drifts > 2%
    max_open_positions: int = 10
    kelly_sizing: bool = True  # Use Kelly criterion
    kelly_fraction: float = 0.5  # Half-Kelly
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

    @property
    def notional(self) -> float:
        return self.amount * self.entry_spot_price


class BacktestEngine:
    """Replays historical data to simulate funding arb strategy."""

    def __init__(self, config: BacktestConfig | None = None):
        self.config = config or BacktestConfig()
        self.simulator = FillSimulator(self.config.fee_model, self.config.slippage_model)

    def run(self, data: HistoricalData) -> BacktestResult:
        """Run backtest on historical data for a single pair."""
        return self.run_multi([data])

    def run_multi(self, datasets: list[HistoricalData]) -> BacktestResult:
        """Run backtest across multiple pairs simultaneously.

        All pairs share the same capital pool. At each timestamp:
        1. Collect funding on all open positions
        2. Check exit conditions on all positions
        3. Rank new entry opportunities by expected yield
        4. Enter best opportunities with Kelly sizing
        5. Record total equity
        """
        log.info(
            "backtest_starting",
            pairs=[d.symbol for d in datasets],
            capital=self.config.initial_capital,
            kelly=self.config.kelly_sizing,
        )

        # Merge all funding events into a single timeline
        events = self._build_event_timeline(datasets)
        if not events:
            return BacktestResult(
                symbol=",".join(d.symbol for d in datasets),
                exchange=datasets[0].exchange if datasets else "",
                start_date="", end_date="",
                initial_capital=self.config.initial_capital,
            )

        # Build price indexes per pair
        price_indexes = {}
        for data in datasets:
            price_indexes[data.symbol] = {
                "spot": self._build_price_index(data.spot_prices),
                "perp": self._build_price_index(data.perp_prices),
            }

        cash = self.config.initial_capital
        positions: list[SimPosition] = []
        closed_trades: list[dict] = []
        equity_curve: list[dict] = []
        next_pos_id = 0

        for ts, events_at_ts in events:
            # 1. Collect funding on all open positions
            for pos in positions:
                # Find the funding rate for this position's pair at this timestamp
                rate_for_pos = self._find_rate_at_ts(events_at_ts, pos.symbol)
                if rate_for_pos is None:
                    continue

                if pos.direction == "long_spot_short_perp":
                    payment = pos.amount * rate_for_pos * pos.entry_spot_price
                else:
                    payment = pos.amount * abs(rate_for_pos) * pos.entry_spot_price

                pos.funding_collected += payment
                cash += payment

            # 2. Check exit conditions
            to_close = []
            for i, pos in enumerate(positions):
                should_exit, exit_reason = self._check_exit(
                    pos, events_at_ts, price_indexes, ts
                )
                if should_exit:
                    spot_price = self._get_price(price_indexes, pos.symbol, "spot", ts)
                    perp_price = self._get_price(price_indexes, pos.symbol, "perp", ts)
                    if spot_price and perp_price:
                        exit_cost = self.simulator.exit_cost(pos.amount, spot_price, perp_price)
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
                            "slippage": 0,
                            "exit_reason": exit_reason,
                        })
                        to_close.append(i)

            for i in sorted(to_close, reverse=True):
                positions.pop(i)

            # 3. Rank and enter new opportunities
            if len(positions) < self.config.max_open_positions:
                opportunities = self._find_opportunities(
                    events_at_ts, positions, price_indexes, ts, cash
                )
                for opp_symbol, opp_rate, opp_direction, opp_size_pct in opportunities:
                    if len(positions) >= self.config.max_open_positions:
                        break

                    spot_price = self._get_price(price_indexes, opp_symbol, "spot", ts)
                    perp_price = self._get_price(price_indexes, opp_symbol, "perp", ts)
                    if not spot_price or not perp_price:
                        continue

                    current_equity = cash + sum(p.funding_collected for p in positions)
                    max_notional = current_equity * opp_size_pct
                    amount = max_notional / spot_price

                    entry_cost = self.simulator.entry_cost(amount, spot_price, perp_price)
                    cash -= entry_cost

                    positions.append(SimPosition(
                        id=next_pos_id,
                        symbol=opp_symbol,
                        amount=amount,
                        entry_spot_price=spot_price,
                        entry_perp_price=perp_price,
                        entry_funding_rate=opp_rate,
                        entry_time=ts,
                        direction=opp_direction,
                        entry_fees=entry_cost,
                    ))
                    next_pos_id += 1

            # 4. Record equity
            position_value = sum(p.funding_collected for p in positions)
            equity = cash + position_value
            equity_curve.append({"timestamp": ts, "equity": equity})

        # Close remaining positions at end
        for pos in positions:
            last_ts = events[-1][0] if events else 0
            spot_price = self._get_price(price_indexes, pos.symbol, "spot", last_ts)
            perp_price = self._get_price(price_indexes, pos.symbol, "perp", last_ts)
            if spot_price and perp_price:
                exit_cost = self.simulator.exit_cost(pos.amount, spot_price, perp_price)
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
                    "exit_time": last_ts,
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
        symbol_str = ",".join(d.symbol for d in datasets)

        result = compute_analytics(
            equity_curve=equity_df,
            trades=closed_trades,
            initial_capital=self.config.initial_capital,
            symbol=symbol_str,
            exchange=datasets[0].exchange if datasets else "",
        )

        log.info(
            "backtest_complete",
            symbols=symbol_str,
            total_return=f"{result.total_return_pct:.2f}%",
            sharpe=f"{result.sharpe_ratio:.2f}",
            max_drawdown=f"{result.max_drawdown_pct:.2f}%",
            trades=result.total_trades,
        )

        return result

    def _build_event_timeline(
        self, datasets: list[HistoricalData]
    ) -> list[tuple[int, list[dict]]]:
        """Merge funding events from all pairs into a sorted timeline.

        Returns list of (timestamp, [event_dicts]) where each event has
        symbol, rate, annualized.
        """
        all_events: dict[int, list[dict]] = {}

        for data in datasets:
            if data.funding_rates.empty:
                continue
            for _, row in data.funding_rates.iterrows():
                ts = int(row["timestamp"])
                event = {
                    "symbol": data.symbol,
                    "rate": row["rate"],
                    "annualized": row["annualized"],
                }
                if ts not in all_events:
                    all_events[ts] = []
                all_events[ts].append(event)

        return sorted(all_events.items())

    def _find_rate_at_ts(self, events: list[dict], symbol: str) -> float | None:
        """Find the funding rate for a symbol at a given timestamp."""
        for event in events:
            if event["symbol"] == symbol:
                return event["rate"]
        return None

    def _check_exit(
        self,
        pos: SimPosition,
        events: list[dict],
        price_indexes: dict,
        ts: int,
    ) -> tuple[bool, str]:
        """Check if a position should be exited."""
        # Find current rate for this pair
        current_rate = None
        current_annualized = None
        for event in events:
            if event["symbol"] == pos.symbol:
                current_rate = event["rate"]
                current_annualized = event["annualized"]
                break

        if current_annualized is not None:
            # Rate dropped below threshold
            exit_threshold = self.config.min_annualized_rate * self.config.exit_rate_threshold
            if pos.direction == "long_spot_short_perp" and current_annualized < exit_threshold:
                return True, "low_rate"
            if pos.direction == "short_spot_long_perp" and current_annualized > -exit_threshold:
                return True, "low_rate"

            # Rate flipped sign
            if current_rate is not None:
                if pos.entry_funding_rate > 0 and current_rate < -0.0001:
                    return True, "rate_flipped"
                if pos.entry_funding_rate < 0 and current_rate > 0.0001:
                    return True, "rate_flipped"

        # Basis drift
        spot_price = self._get_price(price_indexes, pos.symbol, "spot", ts)
        perp_price = self._get_price(price_indexes, pos.symbol, "perp", ts)
        if spot_price and perp_price:
            entry_basis = pos.entry_spot_price - pos.entry_perp_price
            current_basis = spot_price - perp_price
            basis_drift = abs(current_basis - entry_basis) / pos.entry_spot_price
            if basis_drift > self.config.max_basis_drift_pct:
                return True, "basis_drift"

        return False, ""

    def _find_opportunities(
        self,
        events: list[dict],
        positions: list[SimPosition],
        price_indexes: dict,
        ts: int,
        cash: float,
    ) -> list[tuple[str, float, str, float]]:
        """Find and rank entry opportunities.

        Returns list of (symbol, rate, direction, size_pct) sorted by expected yield.
        """
        existing_symbols = {p.symbol for p in positions}
        opportunities = []

        for event in events:
            symbol = event["symbol"]
            rate = event["rate"]
            annualized = event["annualized"]

            if symbol in existing_symbols:
                continue

            if abs(annualized) < self.config.min_annualized_rate:
                continue

            if annualized > 0:
                direction = "long_spot_short_perp"
            else:
                direction = "short_spot_long_perp"

            # Kelly sizing
            if self.config.kelly_sizing:
                fee_estimate = 0.001 * 2
                kelly_raw = kelly_position_size(annualized, fee_drag=fee_estimate)
                size_pct = min(
                    kelly_raw * self.config.kelly_fraction,
                    self.config.max_position_pct,
                )
            else:
                size_pct = self.config.max_position_pct

            if size_pct < 0.01:
                continue

            expected_yield = abs(annualized)
            opportunities.append((symbol, rate, direction, size_pct))

        # Sort by expected yield descending
        opportunities.sort(key=lambda x: abs(x[1]), reverse=True)
        return opportunities

    def _build_price_index(self, df: pd.DataFrame) -> pd.Series:
        if df.empty:
            return pd.Series(dtype=float)
        return df.set_index("timestamp")["close"]

    def _get_price(
        self, indexes: dict, symbol: str, price_type: str, ts: int
    ) -> float | None:
        if symbol not in indexes or price_type not in indexes[symbol]:
            return None
        prices = indexes[symbol][price_type]
        return self._lookup_price(prices, ts)

    def _lookup_price(self, prices: pd.Series, ts: int) -> float | None:
        if prices.empty:
            return None
        valid = prices.index[prices.index <= ts]
        if valid.empty:
            valid = prices.index[prices.index >= ts]
            if valid.empty:
                return None
            return float(prices[valid[0]])
        return float(prices[valid[-1]])
