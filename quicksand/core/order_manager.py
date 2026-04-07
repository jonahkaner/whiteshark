"""Order lifecycle management."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from quicksand.connectors.exchange import ExchangeConnector, OrderResult
from quicksand.utils.logging import get_logger

log = get_logger("orders")


class OrderState(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class Order:
    id: str
    exchange_order_id: str | None
    symbol: str
    exchange: str
    side: str
    order_type: str
    amount: float
    price: float | None
    state: OrderState = OrderState.PENDING
    filled: float = 0.0
    avg_price: float | None = None
    fee: float = 0.0
    created_at: float = field(default_factory=time.time)
    params: dict = field(default_factory=dict)


class OrderManager:
    """Manages order lifecycle: create, submit, track, cancel."""

    def __init__(self):
        self.orders: dict[str, Order] = {}
        self._paper_mode = False

    def set_paper_mode(self, enabled: bool) -> None:
        self._paper_mode = enabled

    async def submit_order(
        self,
        connector: ExchangeConnector,
        symbol: str,
        side: str,
        order_type: str,
        amount: float,
        price: float | None = None,
        params: dict | None = None,
    ) -> Order:
        """Submit an order to the exchange (or simulate in paper mode)."""
        order_id = str(uuid.uuid4())[:8]
        order = Order(
            id=order_id,
            exchange_order_id=None,
            symbol=symbol,
            exchange=connector.name,
            side=side,
            order_type=order_type,
            amount=amount,
            price=price,
            params=params or {},
        )
        self.orders[order_id] = order

        if self._paper_mode:
            return self._simulate_fill(order)

        try:
            result = await connector.create_order(
                symbol=symbol,
                side=side,
                order_type=order_type,
                amount=amount,
                price=price,
                params=params,
            )
            order.exchange_order_id = result.id
            order.state = OrderState.SUBMITTED
            if result.status == "closed":
                order.state = OrderState.FILLED
                order.filled = result.filled
                order.avg_price = result.avg_price
                order.fee = result.fee

            log.info(
                "order_submitted",
                id=order_id,
                exchange_id=result.id,
                symbol=symbol,
                side=side,
                amount=amount,
                status=result.status,
            )
        except Exception as e:
            order.state = OrderState.FAILED
            log.error("order_failed", id=order_id, symbol=symbol, error=str(e))
            raise

        return order

    async def cancel_order(self, order: Order, connector: ExchangeConnector) -> None:
        """Cancel an open order."""
        if order.state not in (OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED):
            return

        if self._paper_mode:
            order.state = OrderState.CANCELLED
            return

        if order.exchange_order_id:
            await connector.cancel_order(order.exchange_order_id, order.symbol)
            order.state = OrderState.CANCELLED
            log.info("order_cancelled", id=order.id, symbol=order.symbol)

    def _simulate_fill(self, order: Order) -> Order:
        """Simulate an immediate fill for paper trading."""
        order.state = OrderState.FILLED
        order.filled = order.amount
        order.avg_price = order.price or 0.0
        order.fee = 0.0  # Paper trading ignores fees for simplicity
        log.info(
            "paper_fill",
            id=order.id,
            symbol=order.symbol,
            side=order.side,
            amount=order.amount,
            price=order.avg_price,
        )
        return order

    def get_open_orders(self) -> list[Order]:
        return [
            o
            for o in self.orders.values()
            if o.state in (OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED)
        ]
