"""Simulated order fills with fee and slippage modeling."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FeeModel:
    """Exchange fee structure."""

    maker_fee: float = 0.0002  # 0.02% (Binance VIP0 with BNB)
    taker_fee: float = 0.0005  # 0.05%
    funding_fee: float = 0.0   # No fee on funding payments themselves

    def market_order_fee(self, notional: float) -> float:
        """Fee for a market order (taker)."""
        return notional * self.taker_fee

    def limit_order_fee(self, notional: float) -> float:
        """Fee for a limit order (maker)."""
        return notional * self.maker_fee


@dataclass
class SlippageModel:
    """Simple linear slippage model.

    Assumes slippage is proportional to order size relative to typical volume.
    For funding arb, slippage is usually minimal since we trade liquid pairs.
    """

    base_slippage_bps: float = 1.0  # 0.01% base slippage
    impact_factor: float = 0.1  # Additional bps per $100K notional

    def estimate_slippage(self, notional: float) -> float:
        """Estimate slippage in price percentage terms."""
        base = self.base_slippage_bps / 10000
        impact = (notional / 100_000) * (self.impact_factor / 10000)
        return base + impact

    def apply_slippage(self, price: float, notional: float, side: str) -> float:
        """Apply slippage to a price based on order side."""
        slip = self.estimate_slippage(notional)
        if side == "buy":
            return price * (1 + slip)  # Pay more when buying
        else:
            return price * (1 - slip)  # Receive less when selling


@dataclass
class SimulatedFill:
    """Result of a simulated order execution."""

    symbol: str
    side: str
    amount: float
    price: float  # Fill price after slippage
    fee: float
    slippage_cost: float  # Dollar cost of slippage

    @property
    def notional(self) -> float:
        return self.amount * self.price

    @property
    def total_cost(self) -> float:
        """Total execution cost (fees + slippage)."""
        return self.fee + self.slippage_cost


class FillSimulator:
    """Simulates order fills for backtesting."""

    def __init__(
        self,
        fee_model: FeeModel | None = None,
        slippage_model: SlippageModel | None = None,
    ):
        self.fees = fee_model or FeeModel()
        self.slippage = slippage_model or SlippageModel()

    def simulate_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        market_price: float,
    ) -> SimulatedFill:
        """Simulate a market order fill."""
        notional = amount * market_price
        fill_price = self.slippage.apply_slippage(market_price, notional, side)
        fee = self.fees.market_order_fee(amount * fill_price)
        slippage_cost = abs(fill_price - market_price) * amount

        return SimulatedFill(
            symbol=symbol,
            side=side,
            amount=amount,
            price=fill_price,
            fee=fee,
            slippage_cost=slippage_cost,
        )

    def entry_cost(self, amount: float, spot_price: float, perp_price: float) -> float:
        """Total cost to enter a funding arb position (both legs)."""
        spot_fill = self.simulate_market_order("spot", "buy", amount, spot_price)
        perp_fill = self.simulate_market_order("perp", "sell", amount, perp_price)
        return spot_fill.total_cost + perp_fill.total_cost

    def exit_cost(self, amount: float, spot_price: float, perp_price: float) -> float:
        """Total cost to exit a funding arb position (both legs)."""
        spot_fill = self.simulate_market_order("spot", "sell", amount, spot_price)
        perp_fill = self.simulate_market_order("perp", "buy", amount, perp_price)
        return spot_fill.total_cost + perp_fill.total_cost
