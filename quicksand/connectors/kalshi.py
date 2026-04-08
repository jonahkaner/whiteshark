"""Kalshi prediction market connector.

Kalshi is a CFTC-regulated prediction market (US-legal).
Binary event contracts: YES/NO, priced $0.01-$0.99, pay $1 if correct.

API docs: https://docs.kalshi.com
Auth: RSA-PSS signed requests. Each request includes:
  - KALSHI-ACCESS-KEY: your API key ID
  - KALSHI-ACCESS-TIMESTAMP: unix timestamp in milliseconds
  - KALSHI-ACCESS-SIGNATURE: RSA-PSS signature of "{timestamp}{METHOD}{path}"
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

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
    Auth: RSA-PSS signed headers on every request.

    Setup:
    1. Go to kalshi.com → Settings → API Keys → Create
    2. Save the API Key ID (string like "25bc41aa-...")
    3. Download the private key file (.pem)
    4. Pass both to this connector
    """

    def __init__(
        self,
        api_key_id: str = "",
        private_key_path: str = "",
        demo: bool = True,
    ):
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self.demo = demo
        self._base_url = DEMO_BASE if demo else PROD_BASE
        self._private_key = None
        self._client = httpx.AsyncClient(timeout=15)

    async def connect(self) -> None:
        """Load private key and verify connectivity."""
        self._load_private_key()
        markets = await self.get_markets(limit=1)
        log.info(
            "kalshi_connected",
            demo=self.demo,
            api_key_id=self.api_key_id[:8] + "..." if self.api_key_id else "none",
            markets_available=len(markets) > 0,
        )

    def _load_private_key(self) -> None:
        """Load the RSA private key from file."""
        if not self.private_key_path:
            log.warning("no_private_key", msg="Running without auth — read-only market data only")
            return

        try:
            from cryptography.hazmat.primitives import serialization
            with open(self.private_key_path, "rb") as f:
                self._private_key = serialization.load_pem_private_key(f.read(), password=None)
            log.info("private_key_loaded", path=self.private_key_path)
        except ImportError:
            log.error("cryptography_not_installed", msg="pip install cryptography")
            raise RuntimeError("Install 'cryptography' package: pip install cryptography")
        except Exception as e:
            log.error("private_key_load_failed", error=str(e))
            raise

    def _sign_request(self, method: str, path: str) -> dict[str, str]:
        """Generate RSA-PSS signed auth headers for a request.

        Signs: "{timestamp_ms}{METHOD}{path}" (no query params)
        Returns headers dict with KALSHI-ACCESS-KEY, TIMESTAMP, SIGNATURE.
        """
        timestamp_ms = str(int(time.time() * 1000))

        # Strip query parameters for signing
        path_without_query = path.split("?")[0]

        headers = {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }

        if self._private_key:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding as crypto_padding

            message = f"{timestamp_ms}{method}{path_without_query}".encode("utf-8")
            signature = self._private_key.sign(
                message,
                crypto_padding.PSS(
                    mgf=crypto_padding.MGF1(hashes.SHA256()),
                    salt_length=crypto_padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
            headers["KALSHI-ACCESS-SIGNATURE"] = base64.b64encode(signature).decode("utf-8")

        return headers

    async def _request(
        self, method: str, path: str, params: dict | None = None, json: dict | None = None
    ) -> dict:
        """Make an authenticated request to the Kalshi API."""
        full_path = f"/trade-api/v2{path}"
        url = f"{self._base_url}{path}"

        headers = self._sign_request(method.upper(), full_path)

        resp = await self._client.request(
            method, url, params=params, json=json, headers=headers
        )
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _parse_market(self, m: dict) -> KalshiMarket:
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

        data = await self._request("GET", "/markets", params=params)
        return [self._parse_market(m) for m in data.get("markets", [])]

    async def get_market(self, ticker: str) -> KalshiMarket:
        """Fetch a single market by ticker."""
        data = await self._request("GET", f"/markets/{ticker}")
        return self._parse_market(data.get("market", {}))

    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """Fetch the order book for a market."""
        data = await self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        return data.get("orderbook", {})

    async def get_events(self, status: str = "active", limit: int = 50) -> list[dict]:
        """Fetch active events (parent containers for markets)."""
        data = await self._request("GET", "/events", params={"status": status, "limit": limit})
        return data.get("events", [])

    # ── Trading ────────────────────────────────────────────────────────────

    async def place_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        price: int,
        time_in_force: str = "gtc",
    ) -> KalshiOrder:
        """Place a limit order.

        Args:
            ticker: Market ticker
            side: "yes" or "no"
            action: "buy" or "sell"
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
        data = await self._request("POST", "/portfolio/orders", json=body)
        o = data.get("order", {})
        return KalshiOrder(
            order_id=o.get("order_id", ""),
            ticker=ticker, side=side, action=action,
            count=count, price=price,
            status=o.get("status", "resting"),
            filled_count=o.get("filled_count", 0),
            created_time=o.get("created_time", ""),
        )

    async def cancel_order(self, order_id: str) -> None:
        """Cancel a resting order."""
        await self._request("DELETE", f"/portfolio/orders/{order_id}")
        log.info("order_cancelled", order_id=order_id)

    async def cancel_all_orders(self, ticker: str | None = None) -> int:
        """Cancel all resting orders, optionally filtered by ticker."""
        params = {"ticker": ticker} if ticker else {}
        data = await self._request("DELETE", "/portfolio/orders", params=params)
        count = data.get("reduced_count", 0)
        log.info("orders_cancelled", count=count, ticker=ticker)
        return count

    async def get_orders(
        self, ticker: str | None = None, status: str = "resting"
    ) -> list[KalshiOrder]:
        """Fetch orders, optionally filtered."""
        params: dict[str, Any] = {"status": status}
        if ticker:
            params["ticker"] = ticker
        data = await self._request("GET", "/portfolio/orders", params=params)
        return [
            KalshiOrder(
                order_id=o.get("order_id", ""),
                ticker=o.get("ticker", ""),
                side=o.get("side", ""),
                action=o.get("action", ""),
                count=o.get("count", 0),
                price=o.get("yes_price", 0),
                status=o.get("status", ""),
                filled_count=o.get("filled_count", 0),
                created_time=o.get("created_time", ""),
            )
            for o in data.get("orders", [])
        ]

    # ── Portfolio ──────────────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Get account balance in dollars."""
        data = await self._request("GET", "/portfolio/balance")
        return data.get("balance", 0) / 100  # Balance is in cents

    async def get_positions(self) -> list[KalshiPosition]:
        """Get all open positions."""
        data = await self._request("GET", "/portfolio/positions")
        positions = []
        for p in data.get("market_positions", []):
            yes_count = p.get("position", 0)
            if yes_count > 0:
                positions.append(KalshiPosition(
                    ticker=p.get("ticker", ""),
                    side="yes", count=yes_count,
                    avg_price=p.get("total_cost", 0) / max(yes_count, 1),
                ))
            elif yes_count < 0:
                positions.append(KalshiPosition(
                    ticker=p.get("ticker", ""),
                    side="no", count=abs(yes_count),
                    avg_price=p.get("total_cost", 0) / max(abs(yes_count), 1),
                ))
        return positions

    async def get_fills(self, ticker: str | None = None, limit: int = 50) -> list[dict]:
        """Get recent fills (executed trades)."""
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        data = await self._request("GET", "/portfolio/fills", params=params)
        return data.get("fills", [])
