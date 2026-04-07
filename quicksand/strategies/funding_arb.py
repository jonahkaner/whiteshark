"""Funding Rate Arbitrage Strategy.

Collects funding payments by going long spot + short perp (when funding is positive)
or short spot + long perp (when funding is negative). Delta-neutral.

How it works:
- Perpetual futures pay/receive "funding" every 8 hours
- When funding rate > 0: longs pay shorts → we buy spot + short perp = collect funding
- When funding rate < 0: shorts pay longs → we sell spot + long perp = collect funding
- The spot and perp positions cancel out directional risk
"""

from __future__ import annotations

import time
import uuid

from quicksand.config import Config, FundingArbConfig
from quicksand.connectors.exchange import ExchangeConnector, FundingRate
from quicksand.core.order_manager import OrderManager
from quicksand.core.portfolio import ArbPosition, Portfolio, Position, PositionSide
from quicksand.core.risk_manager import RiskManager
from quicksand.strategies.base import BaseStrategy
from quicksand.utils.logging import get_logger

log = get_logger("funding_arb")


class FundingArbStrategy(BaseStrategy):
    """Delta-neutral funding rate arbitrage."""

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

        # Respect check interval
        if now - self._last_scan < self._arb_config.check_interval_seconds:
            return
        self._last_scan = now

        # Monitor existing positions
        await self._monitor_positions()

        # Scan for new opportunities (only if risk allows)
        if not self.risk.is_killed:
            await self._scan_opportunities()

    async def _scan_opportunities(self) -> None:
        """Fetch funding rates and enter positions where profitable."""
        for exchange_name, connector in self.exchanges.items():
            pairs_to_check = []
            for pair in self._arb_config.pairs:
                perp_symbol = connector.get_perp_symbol(pair)
                if connector.has_perp(pair) or connector.has_perp(perp_symbol):
                    pairs_to_check.append(perp_symbol)

            if not pairs_to_check:
                continue

            rates = await connector.fetch_all_funding_rates(pairs_to_check)

            for rate in rates:
                # Already have a position in this pair?
                existing = any(
                    p.pair == rate.symbol.split(":")[0]
                    and p.exchange == exchange_name
                    for p in self.portfolio.arb_positions.values()
                )
                if existing:
                    continue

                # Is the rate attractive enough?
                if abs(rate.annualized) < self._arb_config.min_annualized_rate:
                    continue

                log.info(
                    "opportunity_found",
                    symbol=rate.symbol,
                    exchange=exchange_name,
                    funding_rate=f"{rate.rate:.6f}",
                    annualized=f"{rate.annualized:.2%}",
                )

                await self._enter_position(connector, exchange_name, rate)

    async def _enter_position(
        self,
        connector: ExchangeConnector,
        exchange_name: str,
        rate: FundingRate,
    ) -> None:
        """Enter a funding arb position: spot + perp, delta-neutral."""
        spot_symbol = rate.symbol.split(":")[0]  # e.g. BTC/USDT
        perp_symbol = rate.symbol  # e.g. BTC/USDT:USDT

        # Calculate position size
        equity = self.portfolio.equity
        max_notional = equity * self._arb_config.max_position_pct  # Capped by config

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
            # Positive funding: longs pay shorts
            # Strategy: buy spot (long), short perp (collect funding)
            spot_side = "buy"
            perp_side = "sell"
            spot_position_side = PositionSide.LONG
            perp_position_side = PositionSide.SHORT
        else:
            # Negative funding: shorts pay longs
            # Strategy: sell spot (short), long perp (collect funding)
            spot_side = "sell"
            perp_side = "buy"
            spot_position_side = PositionSide.SHORT
            perp_position_side = PositionSide.LONG

        position_id = str(uuid.uuid4())[:8]

        try:
            # Execute both legs
            spot_order = await self.orders.submit_order(
                connector=connector,
                symbol=spot_symbol,
                side=spot_side,
                order_type="market",
                amount=amount,
            )
            perp_order = await self.orders.submit_order(
                connector=connector,
                symbol=perp_symbol,
                side=perp_side,
                order_type="market",
                amount=amount,
                params={"reduceOnly": False},
            )
        except Exception as e:
            log.error("entry_failed", symbol=spot_symbol, error=str(e))
            # TODO: If one leg filled, need to unwind it
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
            funding_rate=f"{rate.rate:.6f}",
            annualized=f"{rate.annualized:.2%}",
        )

    async def _monitor_positions(self) -> None:
        """Monitor existing positions: update prices, check for exit conditions."""
        positions_to_close = []

        for pos_id, arb_pos in self.portfolio.arb_positions.items():
            connector = self.exchanges.get(arb_pos.exchange)
            if not connector:
                continue

            # Update current prices
            try:
                spot_ticker = await connector.fetch_ticker(arb_pos.spot_leg.symbol)
                perp_ticker = await connector.fetch_ticker(arb_pos.perp_leg.symbol)

                arb_pos.spot_leg.update_price(spot_ticker.get("last", 0))
                arb_pos.perp_leg.update_price(perp_ticker.get("last", 0))
            except Exception as e:
                log.warning("price_update_failed", id=pos_id, error=str(e))
                continue

            # Check current funding rate
            try:
                current_rate = await connector.fetch_funding_rate(arb_pos.perp_leg.symbol)
            except Exception:
                continue

            # Exit condition 1: funding rate dropped below threshold
            if abs(current_rate.annualized) < self._arb_config.min_annualized_rate * 0.5:
                log.info(
                    "exit_signal_low_funding",
                    id=pos_id,
                    pair=arb_pos.pair,
                    current_rate=f"{current_rate.annualized:.2%}",
                )
                positions_to_close.append(pos_id)
                continue

            # Exit condition 2: basis has drifted too far (price divergence risk)
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
                positions_to_close.append(pos_id)
                continue

            log.debug(
                "position_status",
                id=pos_id,
                pair=arb_pos.pair,
                pnl=round(arb_pos.total_pnl, 2),
                funding_collected=round(arb_pos.funding_collected, 2),
                current_funding=f"{current_rate.annualized:.2%}",
            )

        # Close positions that hit exit conditions
        for pos_id in positions_to_close:
            await self._exit_position(pos_id)

    async def _exit_position(self, position_id: str) -> None:
        """Close both legs of an arb position."""
        arb_pos = self.portfolio.arb_positions.get(position_id)
        if not arb_pos:
            return

        connector = self.exchanges.get(arb_pos.exchange)
        if not connector:
            return

        # Close spot leg
        spot_close_side = "sell" if arb_pos.spot_leg.side == PositionSide.LONG else "buy"
        perp_close_side = "buy" if arb_pos.perp_leg.side == PositionSide.SHORT else "sell"

        try:
            await self.orders.submit_order(
                connector=connector,
                symbol=arb_pos.spot_leg.symbol,
                side=spot_close_side,
                order_type="market",
                amount=arb_pos.spot_leg.amount,
            )
            await self.orders.submit_order(
                connector=connector,
                symbol=arb_pos.perp_leg.symbol,
                side=perp_close_side,
                order_type="market",
                amount=arb_pos.perp_leg.amount,
                params={"reduceOnly": True},
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
