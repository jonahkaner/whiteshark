"""Portfolio and position tracking."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from quicksand.utils.logging import get_logger

log = get_logger("portfolio")


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass
class Position:
    """A single position (one leg of a trade)."""

    id: str
    symbol: str
    exchange: str
    side: PositionSide
    amount: float
    entry_price: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    opened_at: float = field(default_factory=time.time)

    @property
    def notional(self) -> float:
        return abs(self.amount * self.current_price)

    def update_price(self, price: float) -> None:
        self.current_price = price
        if self.side == PositionSide.LONG:
            self.unrealized_pnl = (price - self.entry_price) * self.amount
        else:
            self.unrealized_pnl = (self.entry_price - price) * self.amount


@dataclass
class ArbPosition:
    """A paired position for funding rate arbitrage (spot long + perp short or vice versa)."""

    id: str
    pair: str  # e.g. "BTC/USDT"
    exchange: str
    spot_leg: Position
    perp_leg: Position
    entry_funding_rate: float
    funding_collected: float = 0.0
    opened_at: float = field(default_factory=time.time)

    @property
    def total_pnl(self) -> float:
        return (
            self.spot_leg.unrealized_pnl
            + self.perp_leg.unrealized_pnl
            + self.funding_collected
            - self.spot_leg.fees_paid
            - self.perp_leg.fees_paid
        )

    @property
    def basis(self) -> float:
        """Current basis = spot price - perp price."""
        return self.spot_leg.current_price - self.perp_leg.current_price

    @property
    def notional(self) -> float:
        return self.spot_leg.notional


class Portfolio:
    """Tracks all positions and overall P&L."""

    def __init__(self, initial_capital: float = 0.0):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.arb_positions: dict[str, ArbPosition] = {}
        self._peak_equity = initial_capital
        self._daily_start_equity = initial_capital
        self._daily_start_time = time.time()

    @property
    def equity(self) -> float:
        """Total equity = cash + unrealized P&L from all positions."""
        total_pnl = sum(p.total_pnl for p in self.arb_positions.values())
        return self.cash + total_pnl

    @property
    def total_notional(self) -> float:
        return sum(p.notional for p in self.arb_positions.values())

    @property
    def drawdown_pct(self) -> float:
        """Current drawdown from peak equity."""
        if self._peak_equity <= 0:
            return 0.0
        return max(0.0, 1.0 - self.equity / self._peak_equity)

    @property
    def daily_pnl(self) -> float:
        return self.equity - self._daily_start_equity

    @property
    def daily_pnl_pct(self) -> float:
        if self._daily_start_equity <= 0:
            return 0.0
        return self.daily_pnl / self._daily_start_equity

    @property
    def utilization_pct(self) -> float:
        """Percentage of capital deployed."""
        if self.equity <= 0:
            return 0.0
        return self.total_notional / self.equity

    def add_arb_position(self, position: ArbPosition) -> None:
        self.arb_positions[position.id] = position
        log.info(
            "position_opened",
            id=position.id,
            pair=position.pair,
            exchange=position.exchange,
            notional=position.notional,
            funding_rate=position.entry_funding_rate,
        )

    def close_arb_position(self, position_id: str) -> ArbPosition | None:
        position = self.arb_positions.pop(position_id, None)
        if position:
            realized = position.total_pnl
            self.cash += realized
            log.info(
                "position_closed",
                id=position.id,
                pair=position.pair,
                pnl=realized,
                funding_collected=position.funding_collected,
            )
        return position

    def update_peak(self) -> None:
        """Update peak equity for drawdown tracking."""
        current = self.equity
        if current > self._peak_equity:
            self._peak_equity = current

    def reset_daily(self) -> None:
        """Reset daily P&L tracking. Call at start of each trading day."""
        self._daily_start_equity = self.equity
        self._daily_start_time = time.time()

    def summary(self) -> dict:
        return {
            "equity": round(self.equity, 2),
            "cash": round(self.cash, 2),
            "positions": len(self.arb_positions),
            "total_notional": round(self.total_notional, 2),
            "utilization_pct": round(self.utilization_pct * 100, 1),
            "daily_pnl": round(self.daily_pnl, 2),
            "daily_pnl_pct": round(self.daily_pnl_pct * 100, 3),
            "drawdown_pct": round(self.drawdown_pct * 100, 2),
        }
