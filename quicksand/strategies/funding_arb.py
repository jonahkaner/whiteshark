"""Funding Rate Arbitrage Strategy.

Collects funding payments by going long spot + short perp (when funding is positive)
or short spot + long perp (when funding is negative). Delta-neutral.

How it works:
- Perpetual futures pay/receive "funding" every 8 hours
- When funding rate > 0: longs pay shorts -> we buy spot + short perp = collect funding
- When funding rate < 0: shorts pay longs -> we sell spot + long perp = collect funding
- The spot and perp positions cancel out directional risk

Optimizations over v1:
- Multi-pair: scans all configured pairs concurrently, ranks by yield
- Kelly criterion: sizes positions proportional to edge strength
- Concurrent exchange scanning: checks all exchanges in parallel
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from dataclasses import dataclass

from quicksand.config import Config, FundingArbConfig
from quicksand.connectors.exchange import ExchangeConnector, FundingRate
from quicksand.core.order_manager import OrderManager
from quicksand.core.portfolio import ArbPosition, Portfolio, Position, PositionSide
from quicksand.core.risk_manager import RiskManager
from quicksand.strategies.base import BaseStrategy
from quicksand.utils.logging import get_logger

log = get_logger("funding_arb")


@dataclass
class Opportunity:
    """A ranked funding arb opportunity."""

    exchange_name: str
    connector: ExchangeConnector
    rate: FundingRate
    spot_symbol: str
    perp_symbol: str
    expected_annual_yield: float  # After estimated fees
    kelly_size_pct: float  # Kelly-optimal position as % of equity


def kelly_position_size(
    annualized_rate: float,
    win_probability: float = 0.85,
    fee_drag: float = 0.02,
) -> float:
    """Calculate Kelly criterion position size for funding arb.

    For funding arb, the "bet" is: will funding remain attractive long enough
    to cover entry/exit costs?

    Args:
        annualized_rate: Expected annualized yield from funding
        win_probability: Probability the trade is profitable (historically ~85% for funding arb)
        fee_drag: Round-trip fees + slippage as a fraction of notional

    Returns:
        Optimal fraction of capital to allocate (0 to 1)
    """
    # Expected profit per dollar per period (assume average 3-day hold)
    hold_days = 3
    expected_profit = abs(annualized_rate) * (hold_days / 365) - fee_drag
    expected_loss = fee_drag  # Worst case: funding flips and we exit at cost

    if expected_loss <= 0 or expected_profit <= 0:
        return 0.0

    # Kelly formula: f* = (p * b - q) / b
    # where p = win prob, q = 1-p, b = win/loss ratio
    b = expected_profit / expected_loss
    q = 1 - win_probability
    kelly = (win_probability * b - q) / b

    return max(0.0, kelly)


class FundingArbStrategy(BaseStrategy):
    """Delta-neutral funding rate arbitrage with Kelly sizing."""

    def __init__(
        self,
        config: Config,
        portfolio: Portfolio,
        risk_manager: RiskManager,
        order_manager: OrderManager,
        exchanges: dict[str, ExchangeConnector],
    ):
        super().__init__(config, portfolio, risk_manager, order_manager, exchanges)
        self._arb_config: FundingArbConfig = config.strategies.funding_arb
        self._last_scan = 0.0

    @property
    def name(self) -> str:
        return "funding_arb"

    async def on_tick(self) -> None:
        """Scan for funding opportunities and manage existing positions."""
        now = time.time()

        if now - self._last_scan < self._arb_config.check_interval_seconds:
            return
        self._last_scan = now

        # Monitor existing positions (all exchanges in parallel)
        await self._monitor_positions()

        # Scan for new opportunities (only if risk allows)
        if not self.risk.is_killed:
            await self._scan_and_enter()

    async def _scan_and_enter(self) -> None:
        """Scan all exchanges and pairs concurrently, rank by yield, enter best ones."""
        # Scan all exchanges in parallel
        scan_tasks = [
            self._scan_exchange(name, connector)
            for name, connector in self.exchanges.items()
        ]
        results = await asyncio.gather(*scan_tasks, return_exceptions=True)

        # Collect all opportunities
        all_opportunities: list[Opportunity] = []
        for result in results:
            if isinstance(result, Exception):
                log.warning("exchange_scan_error", error=str(result))
            elif isinstance(result, list):
                all_opportunities.extend(result)

        if not all_opportunities:
            return

        # Rank by expected yield (highest first)
        all_opportunities.sort(key=lambda o: o.expected_annual_yield, reverse=True)

        log.info(
            "opportunities_ranked",
            count=len(all_opportunities),
            top=f"{all_opportunities[0].spot_symbol} {all_opportunities[0].expected_annual_yield:.1%}"
            if all_opportunities else "none",
        )

        # Enter positions starting from highest yield until we hit limits
        for opp in all_opportunities:
            if len(self.portfolio.arb_positions) >= self.config.risk.max_open_positions:
                break
            await self._enter_position(opp)

    async def _scan_exchange(
        self, exchange_name: str, connector: ExchangeConnector
    ) -> list[Opportunity]:
        """Scan a single exchange for all configured pairs."""
        opportunities = []

        pairs_to_check = []
        for pair in self._arb_config.pairs:
            perp_symbol = connector.get_perp_symbol(pair)
            if connector.has_perp(pair) or connector.has_perp(perp_symbol):
                pairs_to_check.append((pair, perp_symbol))

        if not pairs_to_check:
            return opportunities

        # Fetch all funding rates concurrently
        perp_symbols = [p[1] for p in pairs_to_check]
        rates = await connector.fetch_all_funding_rates(perp_symbols)

        for rate in rates:
            spot_symbol = rate.symbol.split(":")[0]

            # Skip if we already have a position in this pair on this exchange
            existing = any(
                p.pair == spot_symbol and p.exchange == exchange_name
                for p in self.portfolio.arb_positions.values()
            )
            if existing:
                continue

            # Is the rate attractive enough?
            if abs(rate.annualized) < self._arb_config.min_annualized_rate:
                continue

            # Estimate fees (round-trip: 2 * taker fee for spot + perp)
            fee_estimate = 0.001 * 2  # ~0.2% round trip conservative

            # Calculate Kelly sizing
            kelly_raw = kelly_position_size(
                annualized_rate=rate.annualized,
                fee_drag=fee_estimate,
            )

            # Apply Kelly fraction (half-Kelly for safety)
            kelly_adjusted = kelly_raw * self._arb_config.kelly_fraction

            # Cap at max position pct
            position_pct = min(kelly_adjusted, self._arb_config.max_position_pct)

            if position_pct <= 0.01:  # Skip tiny positions
                continue

            expected_yield = abs(rate.annualized) - (fee_estimate * 365 / 3)  # Net of fees

            opportunities.append(Opportunity(
                exchange_name=exchange_name,
                connector=connector,
                rate=rate,
                spot_symbol=spot_symbol,
                perp_symbol=rate.symbol,
                expected_annual_yield=expected_yield,
                kelly_size_pct=position_pct,
            ))

            log.info(
                "opportunity_found",
                symbol=rate.symbol,
                exchange=exchange_name,
                funding_rate=f"{rate.rate:.6f}",
                annualized=f"{rate.annualized:.2%}",
                kelly_size=f"{position_pct:.1%}",
                expected_yield=f"{expected_yield:.1%}",
            )

        return opportunities

    async def _enter_position(self, opp: Opportunity) -> None:
        """Enter a funding arb position with Kelly-optimal sizing."""
        connector = opp.connector
        exchange_name = opp.exchange_name
        rate = opp.rate
        spot_symbol = opp.spot_symbol
        perp_symbol = opp.perp_symbol

        # Calculate position size using Kelly (or fixed if Kelly disabled)
        equity = self.portfolio.equity
        if self._arb_config.kelly_sizing:
            max_notional = equity * opp.kelly_size_pct
        else:
            max_notional = equity * self._arb_config.max_position_pct

        # Fetch current price for sizing
        try:
            ticker = await connector.fetch_ticker(perp_symbol)
        except Exception as e:
            log.warning("ticker_fetch_failed", symbol=perp_symbol, error=str(e))
            return

        price = ticker.get("last", 0)
        if not price or price <= 0:
            return

        amount = max_notional / price
        notional = amount * price

        # Risk check
        risk_check = self.risk.check_new_position(notional, leverage=1.0)
        if not risk_check.allowed:
            log.info("position_rejected_by_risk", reason=risk_check.reason)
            return

        # Determine direction based on funding rate sign
        if rate.rate > 0:
            spot_side = "buy"
            perp_side = "sell"
            spot_position_side = PositionSide.LONG
            perp_position_side = PositionSide.SHORT
        else:
            spot_side = "sell"
            perp_side = "buy"
            spot_position_side = PositionSide.SHORT
            perp_position_side = PositionSide.LONG

        position_id = str(uuid.uuid4())[:8]

        try:
            # Execute both legs concurrently for better fill prices
            spot_task = self.orders.submit_order(
                connector=connector,
                symbol=spot_symbol,
                side=spot_side,
                order_type="market",
                amount=amount,
            )
            perp_task = self.orders.submit_order(
                connector=connector,
                symbol=perp_symbol,
                side=perp_side,
                order_type="market",
                amount=amount,
                params={"reduceOnly": False},
            )
            spot_order, perp_order = await asyncio.gather(spot_task, perp_task)
        except Exception as e:
            log.error("entry_failed", symbol=spot_symbol, error=str(e))
            return

        spot_fill_price = spot_order.avg_price or price
        perp_fill_price = perp_order.avg_price or price

        arb_position = ArbPosition(
            id=position_id,
            pair=spot_symbol,
            exchange=exchange_name,
            spot_leg=Position(
                id=f"{position_id}-spot",
                symbol=spot_symbol,
                exchange=exchange_name,
                side=spot_position_side,
                amount=amount,
                entry_price=spot_fill_price,
                current_price=spot_fill_price,
                fees_paid=spot_order.fee,
            ),
            perp_leg=Position(
                id=f"{position_id}-perp",
                symbol=perp_symbol,
                exchange=exchange_name,
                side=perp_position_side,
                amount=amount,
                entry_price=perp_fill_price,
                current_price=perp_fill_price,
                fees_paid=perp_order.fee,
            ),
            entry_funding_rate=rate.rate,
        )

        self.portfolio.add_arb_position(arb_position)

        log.info(
            "arb_position_entered",
            id=position_id,
            pair=spot_symbol,
            direction="long_spot_short_perp" if rate.rate > 0 else "short_spot_long_perp",
            amount=round(amount, 6),
            notional=round(notional, 2),
            kelly_size=f"{opp.kelly_size_pct:.1%}",
            funding_rate=f"{rate.rate:.6f}",
            annualized=f"{rate.annualized:.2%}",
        )

    async def _monitor_positions(self) -> None:
        """Monitor existing positions: update prices, check for exit conditions."""
        if not self.portfolio.arb_positions:
            return

        # Monitor all positions concurrently
        tasks = [
            self._check_position(pos_id, arb_pos)
            for pos_id, arb_pos in self.portfolio.arb_positions.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect positions to close
        positions_to_close = []
        for result in results:
            if isinstance(result, str):  # Position ID to close
                positions_to_close.append(result)
            elif isinstance(result, Exception):
                log.warning("monitor_error", error=str(result))

        # Close positions that hit exit conditions
        for pos_id in positions_to_close:
            await self._exit_position(pos_id)

    async def _check_position(self, pos_id: str, arb_pos: ArbPosition) -> str | None:
        """Check a single position for exit conditions. Returns pos_id if should close."""
        connector = self.exchanges.get(arb_pos.exchange)
        if not connector:
            return None

        # Update current prices
        try:
            spot_ticker, perp_ticker = await asyncio.gather(
                connector.fetch_ticker(arb_pos.spot_leg.symbol),
                connector.fetch_ticker(arb_pos.perp_leg.symbol),
            )
            arb_pos.spot_leg.update_price(spot_ticker.get("last", 0))
            arb_pos.perp_leg.update_price(perp_ticker.get("last", 0))
        except Exception as e:
            log.warning("price_update_failed", id=pos_id, error=str(e))
            return None

        # Check current funding rate
        try:
            current_rate = await connector.fetch_funding_rate(arb_pos.perp_leg.symbol)
        except Exception:
            return None

        # Exit condition 1: funding rate dropped below threshold
        if abs(current_rate.annualized) < self._arb_config.min_annualized_rate * 0.5:
            log.info(
                "exit_signal_low_funding",
                id=pos_id,
                pair=arb_pos.pair,
                current_rate=f"{current_rate.annualized:.2%}",
            )
            return pos_id

        # Exit condition 2: funding rate flipped sign (we'd be paying instead of collecting)
        if arb_pos.entry_funding_rate > 0 and current_rate.rate < -0.0001:
            log.info("exit_signal_rate_flipped", id=pos_id, pair=arb_pos.pair)
            return pos_id
        if arb_pos.entry_funding_rate < 0 and current_rate.rate > 0.0001:
            log.info("exit_signal_rate_flipped", id=pos_id, pair=arb_pos.pair)
            return pos_id

        # Exit condition 3: basis has drifted too far
        entry_basis = arb_pos.spot_leg.entry_price - arb_pos.perp_leg.entry_price
        current_basis = arb_pos.basis
        basis_drift = abs(current_basis - entry_basis) / arb_pos.spot_leg.entry_price

        if basis_drift > self._arb_config.max_basis_drift_pct:
            log.info(
                "exit_signal_basis_drift",
                id=pos_id,
                pair=arb_pos.pair,
                basis_drift=f"{basis_drift:.4%}",
            )
            return pos_id

        log.debug(
            "position_status",
            id=pos_id,
            pair=arb_pos.pair,
            pnl=round(arb_pos.total_pnl, 2),
            funding_collected=round(arb_pos.funding_collected, 2),
            current_funding=f"{current_rate.annualized:.2%}",
        )
        return None

    async def _exit_position(self, position_id: str) -> None:
        """Close both legs of an arb position concurrently."""
        arb_pos = self.portfolio.arb_positions.get(position_id)
        if not arb_pos:
            return

        connector = self.exchanges.get(arb_pos.exchange)
        if not connector:
            return

        spot_close_side = "sell" if arb_pos.spot_leg.side == PositionSide.LONG else "buy"
        perp_close_side = "buy" if arb_pos.perp_leg.side == PositionSide.SHORT else "sell"

        try:
            # Close both legs concurrently
            await asyncio.gather(
                self.orders.submit_order(
                    connector=connector,
                    symbol=arb_pos.spot_leg.symbol,
                    side=spot_close_side,
                    order_type="market",
                    amount=arb_pos.spot_leg.amount,
                ),
                self.orders.submit_order(
                    connector=connector,
                    symbol=arb_pos.perp_leg.symbol,
                    side=perp_close_side,
                    order_type="market",
                    amount=arb_pos.perp_leg.amount,
                    params={"reduceOnly": True},
                ),
            )
        except Exception as e:
            log.error("exit_failed", id=position_id, error=str(e))
            return

        self.portfolio.close_arb_position(position_id)

    async def on_shutdown(self) -> None:
        """Close all arb positions on shutdown."""
        log.info("shutting_down", open_positions=len(self.portfolio.arb_positions))
        for pos_id in list(self.portfolio.arb_positions.keys()):
            await self._exit_position(pos_id)
