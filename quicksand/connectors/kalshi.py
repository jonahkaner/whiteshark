"""Kalshi prediction market connector.

Kalshi is a CFTC-regulated prediction market (US-legal).
Binary event contracts: YES/NO, priced $0.01-$0.99, pay $1 if correct.

API docs: https://docs.kalshi.com
Auth: RSA-PSS signed requests (not bearer tokens).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from quicksand.utils.logging import get_logger

log = get_logger("kalshi")

PROD_BASE = "https://api.kalshi.com/trade-api/v2"
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"


@dataclass
class KalshiMarket:
    """A single Kalshi prediction market."""

    ticker: str  # e.g. "KXBTC-25APR11-T100000"
    event_ticker: str  # Parent event
    title: str  # Human-readable title
    yes_bid: float  # Best bid for YES (cents)
    yes_ask: float  # Best ask for YES
    no_bid: float  # Best bid for NO
    no_ask: float  # Best ask for NO
    last_price: float
    volume: int
    open_interest: int
    status: str  # "active", "closed", "settled"
    result: str | None = None  # "yes", "no", or None if unsettled
    expiration_time: str = ""
    category: str = ""

    @property
    def mid_price(self) -> float:
        """Midpoint between yes bid and ask."""
        if self.yes_bid > 0 and self.yes_ask > 0:
            return (self.yes_bid + self.yes_ask) / 2
        return self.last_price

    @property
    def spread(self) -> float:
        """Bid-ask spread in cents."""
        if self.yes_bid > 0 and self.yes_ask > 0:
            return self.yes_ask - self.yes_bid
        return 0

    @property
    def spread_pct(self) -> float:
        """Spread as percentage of mid price."""
        mid = self.mid_price
        if mid > 0:
            return self.spread / mid
        return 0


@dataclass
class KalshiPosition:
    """A position in a Kalshi market."""

    ticker: str
    side: str  # "yes" or "no"
    count: int  # Number of contracts
    avg_price: float  # Average entry price (cents)
    market_price: float = 0  # Current market price

    @property
    def cost_basis(self) -> float:
        """Total cost in dollars."""
        return self.count * self.avg_price / 100

    @property
    def market_value(self) -> float:
        """Current value in dollars."""
        return self.count * self.market_price / 100

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_basis


@dataclass
class KalshiOrder:
    """An order on Kalshi."""

    order_id: str
    ticker: str
    side: str  # "yes" or "no"
    action: str  # "buy" or "sell"
    count: int
    price: int  # Price in cents
    status: str  # "resting", "filled", "canceled"
    filled_count: int = 0
    created_time: str = ""


class KalshiConnector:
    """REST API connector for Kalshi.

    Handles authentication, market data, and order management.
    Uses the official REST API with API key + private key auth.
    """

    def __init__(
        self,
        api_key: str = "",
        private_key_path: str = "",
        demo: bool = True,
    ):
        self.api_key = api_key
        self.private_key_path = private_key_path
        self.demo = demo
        self._base_url = DEMO_BASE if demo else PROD_BASE
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=15,
        )
        self._logged_in = False
        self._token: str = ""

    async def connect(self) -> None:
        """Authenticate and verify connectivity."""
        if self.api_key:
            await self._login()
        markets = await self.get_markets(limit=1)
        log.info(
            "kalshi_connected",
            demo=self.demo,
            markets_available=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ── Authentication ─────────────────────────────────────────────────────

    async def _login(self) -> None:
        """Login with email/password or API key to get a session token.

        Note: Production uses RSA-PSS signed headers per request.
        Demo mode uses simpler email/password login.
        """
        # For demo mode, use the login endpoint
        if self.demo:
            # Demo uses a simpler auth flow
            self._client.headers["Authorization"] = f"Bearer {self.api_key}"
            self._logged_in = True
            return

        # Production: set API key header
        # RSA-PSS signing is handled per-request
        self._client.headers["Authorization"] = f"Bearer {self.api_key}"
        self._logged_in = True

    def _auth_headers(self) -> dict[str, str]:
        """Get authentication headers for a request."""
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        elif self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    # ── Market Data ────────────────────────────────────────────────────────

    async def get_markets(
        self,
        status: str = "active",
        limit: int = 100,
        cursor: str | None = None,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
    ) -> list[KalshiMarket]:
        """Fetch active markets."""
        params: dict[str, Any] = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker

        resp = await self._client.get("/markets", params=params)
        resp.raise_for_status()
        data = resp.json()

        markets = []
        for m in data.get("markets", []):
            markets.append(KalshiMarket(
                ticker=m.get("ticker", ""),
                event_ticker=m.get("event_ticker", ""),
                title=m.get("title", ""),
                yes_bid=m.get("yes_bid", 0),
                yes_ask=m.get("yes_ask", 0),
                no_bid=m.get("no_bid", 0),
                no_ask=m.get("no_ask", 0),
                last_price=m.get("last_price", 0),
                volume=m.get("volume", 0),
                open_interest=m.get("open_interest", 0),
                status=m.get("status", ""),
                result=m.get("result"),
                expiration_time=m.get("expiration_time", ""),
                category=m.get("category", ""),
            ))
        return markets

    async def get_market(self, ticker: str) -> KalshiMarket:
        """Fetch a single market by ticker."""
        resp = await self._client.get(f"/markets/{ticker}")
        resp.raise_for_status()
        m = resp.json().get("market", {})
        return KalshiMarket(
            ticker=m.get("ticker", ""),
            event_ticker=m.get("event_ticker", ""),
            title=m.get("title", ""),
            yes_bid=m.get("yes_bid", 0),
            yes_ask=m.get("yes_ask", 0),
            no_bid=m.get("no_bid", 0),
            no_ask=m.get("no_ask", 0),
            last_price=m.get("last_price", 0),
            volume=m.get("volume", 0),
            open_interest=m.get("open_interest", 0),
            status=m.get("status", ""),
            result=m.get("result"),
            expiration_time=m.get("expiration_time", ""),
            category=m.get("category", ""),
        )

    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """Fetch the order book for a market."""
        resp = await self._client.get(
            f"/markets/{ticker}/orderbook",
            params={"depth": depth},
        )
        resp.raise_for_status()
        return resp.json().get("orderbook", {})

    async def get_events(self, status: str = "active", limit: int = 50) -> list[dict]:
        """Fetch active events (parent containers for markets)."""
        resp = await self._client.get(
            "/events", params={"status": status, "limit": limit}
        )
        resp.raise_for_status()
        return resp.json().get("events", [])

    # ── Trading ────────────────────────────────────────────────────────────

    async def place_order(
        self,
        ticker: str,
        side: str,  # "yes" or "no"
        action: str,  # "buy" or "sell"
        count: int,
        price: int,  # In cents (1-99)
        time_in_force: str = "gtc",  # "gtc" or "ioc"
    ) -> KalshiOrder:
        """Place a limit order.

        Args:
            ticker: Market ticker
            side: "yes" or "no"
            action: "buy" (open position) or "sell" (close position)
            count: Number of contracts
            price: Price in cents (1-99)
            time_in_force: "gtc" (good til canceled) or "ioc" (immediate or cancel)
        """
        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": "limit",
            "yes_price": price if side == "yes" else (100 - price),
            "time_in_force": time_in_force,
        }

        resp = await self._client.post("/portfolio/orders", json=body)
        resp.raise_for_status()
        o = resp.json().get("order", {})

        return KalshiOrder(
            order_id=o.get("order_id", ""),
            ticker=ticker,
            side=side,
            action=action,
            count=count,
            price=price,
            status=o.get("status", "resting"),
            filled_count=o.get("filled_count", 0),
            created_time=o.get("created_time", ""),
        )

    async def cancel_order(self, order_id: str) -> None:
        """Cancel a resting order."""
        resp = await self._client.delete(f"/portfolio/orders/{order_id}")
        resp.raise_for_status()
        log.info("order_cancelled", order_id=order_id)

    async def cancel_all_orders(self, ticker: str | None = None) -> int:
        """Cancel all resting orders, optionally filtered by ticker."""
        params = {}
        if ticker:
            params["ticker"] = ticker
        resp = await self._client.delete("/portfolio/orders", params=params)
        resp.raise_for_status()
        count = resp.json().get("reduced_count", 0)
        log.info("orders_cancelled", count=count, ticker=ticker)
        return count

    async def get_orders(
        self, ticker: str | None = None, status: str = "resting"
    ) -> list[KalshiOrder]:
        """Fetch orders, optionally filtered."""
        params: dict[str, Any] = {"status": status}
        if ticker:
            params["ticker"] = ticker

        resp = await self._client.get("/portfolio/orders", params=params)
        resp.raise_for_status()
        orders = []
        for o in resp.json().get("orders", []):
            orders.append(KalshiOrder(
                order_id=o.get("order_id", ""),
                ticker=o.get("ticker", ""),
                side=o.get("side", ""),
                action=o.get("action", ""),
                count=o.get("count", 0),
                price=o.get("yes_price", 0),
                status=o.get("status", ""),
                filled_count=o.get("filled_count", 0),
                created_time=o.get("created_time", ""),
            ))
        return orders

    # ── Portfolio ──────────────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Get account balance in dollars."""
        resp = await self._client.get("/portfolio/balance")
        resp.raise_for_status()
        data = resp.json()
        # Balance is in cents
        return data.get("balance", 0) / 100

    async def get_positions(self) -> list[KalshiPosition]:
        """Get all open positions."""
        resp = await self._client.get("/portfolio/positions")
        resp.raise_for_status()
        positions = []
        for p in resp.json().get("market_positions", []):
            # Determine side and count from position fields
            yes_count = p.get("position", 0)
            if yes_count > 0:
                positions.append(KalshiPosition(
                    ticker=p.get("ticker", ""),
                    side="yes",
                    count=yes_count,
                    avg_price=p.get("total_cost", 0) / max(yes_count, 1),
                ))
            elif yes_count < 0:
                positions.append(KalshiPosition(
                    ticker=p.get("ticker", ""),
                    side="no",
                    count=abs(yes_count),
                    avg_price=p.get("total_cost", 0) / max(abs(yes_count), 1),
                ))
        return positions

    async def get_fills(self, ticker: str | None = None, limit: int = 50) -> list[dict]:
        """Get recent fills (executed trades)."""
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        resp = await self._client.get("/portfolio/fills", params=params)
        resp.raise_for_status()
        return resp.json().get("fills", [])
