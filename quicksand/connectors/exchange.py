"""Unified exchange connector wrapping ccxt."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import ccxt.async_support as ccxt_async

from quicksand.utils.logging import get_logger

log = get_logger("connector")


@dataclass
class Balance:
    total: dict[str, float] = field(default_factory=dict)
    free: dict[str, float] = field(default_factory=dict)
    used: dict[str, float] = field(default_factory=dict)

    @property
    def total_usdt(self) -> float:
        return self.total.get("USDT", 0.0) + self.total.get("USDC", 0.0)


@dataclass
class FundingRate:
    symbol: str
    rate: float  # Per-period rate (typically 8h)
    annualized: float  # Annualized rate
    next_funding_time: int | None = None  # Unix ms
    timestamp: int | None = None


@dataclass
class OrderResult:
    id: str
    symbol: str
    side: str  # buy | sell
    type: str  # limit | market
    amount: float
    price: float | None
    status: str  # open | closed | canceled
    filled: float = 0.0
    avg_price: float | None = None
    fee: float = 0.0
    raw: dict = field(default_factory=dict)


class ExchangeConnector:
    """Wraps a ccxt async exchange instance with typed helpers."""

    def __init__(
        self,
        exchange_id: str,
        api_key: str = "",
        secret: str = "",
        sandbox: bool = True,
    ):
        self.exchange_id = exchange_id
        self._sandbox = sandbox

        exchange_class = getattr(ccxt_async, exchange_id, None)
        if exchange_class is None:
            raise ValueError(f"Unknown exchange: {exchange_id}")

        config: dict[str, Any] = {
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
        if api_key:
            config["apiKey"] = api_key
        if secret:
            config["secret"] = secret

        self._exchange: ccxt_async.Exchange = exchange_class(config)

        if sandbox:
            self._exchange.set_sandbox_mode(True)

    @property
    def name(self) -> str:
        return self.exchange_id

    async def connect(self) -> None:
        """Load markets and verify connectivity."""
        await self._exchange.load_markets()
        log.info(
            "exchange_connected",
            exchange=self.exchange_id,
            sandbox=self._sandbox,
            markets=len(self._exchange.markets),
        )

    async def close(self) -> None:
        """Close the exchange connection."""
        await self._exchange.close()

    async def fetch_balance(self) -> Balance:
        """Fetch account balances."""
        raw = await self._exchange.fetch_balance()
        return Balance(
            total={k: v for k, v in raw.get("total", {}).items() if v and v > 0},
            free={k: v for k, v in raw.get("free", {}).items() if v and v > 0},
            used={k: v for k, v in raw.get("used", {}).items() if v and v > 0},
        )

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        """Fetch current funding rate for a perpetual contract."""
        raw = await self._exchange.fetch_funding_rate(symbol)
        rate = raw.get("fundingRate", 0.0) or 0.0
        # Funding is typically every 8h = 3x/day = 1095x/year
        annualized = rate * 3 * 365
        return FundingRate(
            symbol=symbol,
            rate=rate,
            annualized=annualized,
            next_funding_time=raw.get("fundingTimestamp"),
            timestamp=raw.get("timestamp"),
        )

    async def fetch_all_funding_rates(self, symbols: list[str]) -> list[FundingRate]:
        """Fetch funding rates for multiple symbols concurrently."""
        tasks = [self.fetch_funding_rate(s) for s in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        rates = []
        for symbol, result in zip(symbols, results):
            if isinstance(result, Exception):
                log.warning("funding_rate_error", symbol=symbol, error=str(result))
            else:
                rates.append(result)
        return rates

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        """Fetch current ticker (bid/ask/last price)."""
        return await self._exchange.fetch_ticker(symbol)

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        """Fetch order book."""
        return await self._exchange.fetch_order_book(symbol, limit)

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        amount: float,
        price: float | None = None,
        params: dict | None = None,
    ) -> OrderResult:
        """Place an order on the exchange."""
        raw = await self._exchange.create_order(
            symbol=symbol,
            type=order_type,
            side=side,
            amount=amount,
            price=price,
            params=params or {},
        )
        return OrderResult(
            id=raw["id"],
            symbol=raw["symbol"],
            side=raw["side"],
            type=raw["type"],
            amount=raw["amount"],
            price=raw.get("price"),
            status=raw["status"],
            filled=raw.get("filled", 0.0),
            avg_price=raw.get("average"),
            fee=raw.get("fee", {}).get("cost", 0.0) if raw.get("fee") else 0.0,
            raw=raw,
        )

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Cancel an open order."""
        return await self._exchange.cancel_order(order_id, symbol)

    async def fetch_order(self, order_id: str, symbol: str) -> OrderResult:
        """Fetch order status."""
        raw = await self._exchange.fetch_order(order_id, symbol)
        return OrderResult(
            id=raw["id"],
            symbol=raw["symbol"],
            side=raw["side"],
            type=raw["type"],
            amount=raw["amount"],
            price=raw.get("price"),
            status=raw["status"],
            filled=raw.get("filled", 0.0),
            avg_price=raw.get("average"),
            fee=raw.get("fee", {}).get("cost", 0.0) if raw.get("fee") else 0.0,
            raw=raw,
        )

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1h", limit: int = 100
    ) -> list[list]:
        """Fetch OHLCV candles."""
        return await self._exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

    def get_perp_symbol(self, spot_symbol: str) -> str:
        """Convert spot symbol to perpetual futures symbol.

        Different exchanges use different conventions:
        - Binance: BTC/USDT -> BTC/USDT:USDT
        - Bybit: BTC/USDT -> BTC/USDT:USDT
        - OKX: BTC/USDT -> BTC/USDT:USDT
        """
        base_symbol = spot_symbol.replace(":USDT", "")
        return f"{base_symbol}:USDT"

    def has_perp(self, symbol: str) -> bool:
        """Check if a perpetual contract exists for this symbol."""
        perp = self.get_perp_symbol(symbol)
        return perp in self._exchange.markets
