"""Kalshi Market Making Strategy.

Makes money by quoting both YES and NO sides on prediction markets,
capturing the bid-ask spread. Key insight: maker fees on Kalshi are ZERO,
so every spread captured is pure profit.

How it works:
1. Scan active markets for liquid events with wide spreads
2. Place resting limit orders on both sides (buy YES low, buy NO low)
3. When both sides fill, you've locked in the spread as profit
4. Manage inventory to avoid being too one-sided
5. Cancel and re-quote when prices move

Example:
- Market: "Will BTC be above $100K on April 15?"
- YES bid: 55¢, YES ask: 60¢ (spread = 5¢)
- We place: Buy YES at 56¢, Sell YES at 59¢ (our spread = 3¢)
- If both sides fill on 100 contracts: 100 × $0.03 = $3.00 profit
- Scale across 15+ markets simultaneously
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from quicksand.connectors.kalshi import KalshiConnector, KalshiMarket, KalshiOrder
from quicksand.utils.logging import get_logger

log = get_logger("kalshi_mm")


@dataclass
class MarketMakingConfig:
    """Configuration for the market making strategy."""

    min_spread_cents: int = 3  # Minimum spread to quote (in cents)
    quote_spread_cents: int = 2  # How wide our quotes are inside the spread
    max_position_per_market: int = 200  # Max contracts per market
    max_total_exposure: float = 5000  # Max total $ deployed across all markets
    max_markets: int = 15  # Max simultaneous markets to make
    min_volume: int = 0  # Minimum 24h volume (0 = no filter)
    min_open_interest: int = 0  # Minimum open interest (0 = no filter)
    requote_interval_seconds: int = 45  # How often to check and re-quote
    inventory_skew: float = 0.3  # Skew quotes when inventory is one-sided
    max_expiry_days: int = 7  # Prefer markets expiring within this many days
    order_size: int = 50  # Contracts per order


@dataclass
class MarketQuote:
    """Active quotes in a market."""

    ticker: str
    title: str
    bid_order: KalshiOrder | None = None  # Our buy YES order
    ask_order: KalshiOrder | None = None  # Our sell YES (buy NO) order
    yes_inventory: int = 0  # Net YES contracts held (from Kalshi positions)
    total_filled: int = 0  # Total contracts filled (both sides)
    total_pnl: float = 0  # Realized P&L from completed round trips
    last_update: float = 0


class KalshiMarketMaker:
    """Market making strategy for Kalshi prediction markets.

    Scans for liquid markets with wide spreads, quotes both sides,
    and captures the spread as profit. Zero maker fees = pure edge.
    """

    def __init__(
        self,
        connector: KalshiConnector,
        config: MarketMakingConfig | None = None,
        paper_mode: bool = True,
    ):
        self.connector = connector
        self.config = config or MarketMakingConfig()
        self.paper_mode = paper_mode
        self.active_quotes: dict[str, MarketQuote] = {}
        self._last_scan = 0.0
        self._total_pnl = 0.0
        self._total_trades = 0

        # Paper mode tracking
        self._paper_balance = 0.0
        self._paper_positions: dict[str, int] = {}  # ticker -> net position
        self._processed_fills: set[str] = set()  # Fill IDs already counted
        self._known_positions: dict[str, int] = {}  # ticker -> position from Kalshi
        self._last_unwind_check = 0.0

    @property
    def name(self) -> str:
        return "kalshi_mm"

    async def initialize(self, starting_balance: float) -> None:
        """Initialize with starting capital and load existing positions."""
        self._paper_balance = starting_balance

        # Load existing positions from Kalshi on startup
        if not self.paper_mode:
            await self._load_existing_positions()
            await self._unwind_long_dated()

        log.info("kalshi_mm_initialized", balance=starting_balance, paper=self.paper_mode)

    async def _load_existing_positions(self) -> None:
        """Load existing positions from Kalshi to prevent over-accumulation."""
        try:
            positions = await self.connector.get_positions()
            for pos in positions:
                if pos.count > 0:
                    inventory = pos.count if pos.side == "yes" else -pos.count
                    self._known_positions[pos.ticker] = inventory
                    log.info(
                        "existing_position_loaded",
                        ticker=pos.ticker,
                        side=pos.side,
                        count=pos.count,
                        inventory=inventory,
                    )
            log.info("positions_loaded", count=len(self._known_positions))
        except Exception as e:
            log.warning("load_positions_failed", error=str(e))

    async def _unwind_long_dated(self) -> None:
        """Sell any positions in markets expiring beyond our max horizon."""
        log.info(
            "unwind_check_start",
            total_positions=len(self._known_positions),
            positions=dict(self._known_positions),
        )
        for ticker, inventory in list(self._known_positions.items()):
            if inventory == 0:
                continue
            try:
                market = await self.connector.get_market(ticker)
                days = self._days_until_expiry(market)
                log.info(
                    "unwind_check_position",
                    ticker=ticker, inventory=inventory,
                    days=round(days, 1), max_days=self.config.max_expiry_days,
                    expiration_time=market.expiration_time,
                    yes_bid=market.yes_bid, no_bid=market.no_bid,
                    will_unwind=days > self.config.max_expiry_days,
                )
                if days <= self.config.max_expiry_days:
                    continue

                # This position is too long-dated — sell it
                count = abs(inventory)
                if inventory > 0:
                    # We hold YES — sell YES
                    side, action = "yes", "sell"
                    # Sell at 2 cents below current bid to fill quickly
                    price = max(1, int(market.yes_bid) - 2)
                else:
                    # We hold NO (negative YES) — sell NO
                    side, action = "no", "sell"
                    price = max(1, int(market.no_bid) - 2)

                log.info(
                    "unwinding_long_dated",
                    ticker=ticker, days=round(days, 1),
                    side=side, count=count, price=price,
                )
                order = await self.connector.place_order(
                    ticker=ticker,
                    side=side,
                    action=action,
                    count=count,
                    price=price,
                )
                log.info(
                    "unwind_order_placed",
                    ticker=ticker, count=count,
                    order_id=order.order_id, status=order.status,
                )
            except Exception as e:
                log.error("unwind_failed", ticker=ticker, error=str(e), exc_info=True)

    async def _sync_positions(self) -> None:
        """Periodically sync positions from Kalshi to stay accurate."""
        try:
            positions = await self.connector.get_positions()
            new_positions = {}
            for pos in positions:
                if pos.count > 0:
                    inventory = pos.count if pos.side == "yes" else -pos.count
                    new_positions[pos.ticker] = inventory
            self._known_positions = new_positions

            # Update active quotes with real inventory
            for ticker, quote in self.active_quotes.items():
                quote.yes_inventory = self._known_positions.get(ticker, 0)
        except Exception as e:
            log.warning("sync_positions_failed", error=str(e))

    async def on_tick(self) -> None:
        """Main tick: scan markets, update quotes, manage inventory."""
        now = time.time()

        if now - self._last_scan < self.config.requote_interval_seconds:
            return
        self._last_scan = now

        # 1. Sync positions from Kalshi (live mode only)
        if not self.paper_mode:
            await self._sync_positions()

            # Check for long-dated positions to unwind (every 5 minutes)
            if now - self._last_unwind_check > 300:
                self._last_unwind_check = now
                await self._unwind_long_dated()

        # 2. Scan for good markets to make
        await self._scan_markets()

        # 3. Update existing quotes
        await self._update_quotes()

        # 4. Log status
        total_inventory = sum(
            abs(q.yes_inventory) for q in self.active_quotes.values()
        )
        log.info(
            "mm_tick",
            active_markets=len(self.active_quotes),
            total_pnl=round(self._total_pnl, 2),
            total_trades=self._total_trades,
            total_inventory=total_inventory,
        )

    def _days_until_expiry(self, market: KalshiMarket) -> float:
        """Calculate days until market expires."""
        # Try API expiration field first
        if market.expiration_time:
            try:
                exp_str = market.expiration_time.replace("Z", "+00:00")
                exp_time = datetime.fromisoformat(exp_str)
                now = datetime.now(timezone.utc)
                delta = (exp_time - now).total_seconds() / 86400
                return max(0, delta)
            except (ValueError, TypeError):
                pass

        # Fallback: parse date from ticker (e.g. KXETH-26APR09, KXFED-27MAR)
        import re
        ticker = market.ticker
        # Match patterns like 26APR09 (year 2026, April 9) or 27MAR (year 2027, March)
        m = re.search(r'-(\d{2})([A-Z]{3})(\d{2})?', ticker)
        if m:
            try:
                year = 2000 + int(m.group(1))
                month_str = m.group(2)
                day_str = m.group(3)
                months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                          "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
                month = months.get(month_str, 0)
                if month == 0:
                    return 999
                day = int(day_str) if day_str else 15  # Default to mid-month
                exp = datetime(year, month, day, tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                delta = (exp - now).total_seconds() / 86400
                return max(0, delta)
            except (ValueError, TypeError):
                pass

        # Check for obvious long-dated markers in ticker
        for marker in ["-50", "-45", "-40", "-35", "-30"]:
            if ticker.endswith(marker):
                return 999  # Likely a decade+ market

        return 999  # Unknown = exclude

    async def _scan_markets(self) -> None:
        """Find liquid markets with wide spreads worth making."""
        all_markets = []

        try:
            # Scan liquid series
            for series in ["KXINX", "KXBTC", "KXETH", "KXFED", "KXGDP",
                           "KXNBA", "KXMLB", "KXNFL", "KXWEATHER"]:
                try:
                    series_markets = await self.connector.get_markets(
                        status="open", limit=30, series_ticker=series
                    )
                    all_markets.extend(series_markets)
                except Exception:
                    continue

            # Also scan top events
            events = await self.connector.get_events(status="open", limit=10)
            events_scanned = 0
            for event in events:
                event_ticker = event.get("event_ticker", "")
                if "CROSSCATEGORY" in event_ticker or "MULTIGAME" in event_ticker:
                    continue
                try:
                    event_markets = await self.connector.get_markets(
                        status="open", limit=20, event_ticker=event_ticker
                    )
                    all_markets.extend(event_markets)
                except Exception:
                    continue
                events_scanned += 1
                if events_scanned >= 5:
                    break

            # Deduplicate by ticker
            seen = set()
            markets = []
            for m in all_markets:
                if m.ticker not in seen:
                    seen.add(m.ticker)
                    markets.append(m)

            log.info("scan_fetched", total_fetched=len(markets))
        except Exception as e:
            log.warning("market_scan_failed", error=str(e))
            return

        # Filter and rank markets
        candidates = []
        skipped = {"status": 0, "no_spread": 0, "extreme": 0, "no_prices": 0, "too_far": 0}
        for market in markets:
            if market.status not in ("active", "open"):
                skipped["status"] += 1
                continue
            if market.yes_bid <= 0 or market.yes_ask <= 0:
                skipped["no_prices"] += 1
                continue
            if market.spread < self.config.min_spread_cents:
                skipped["no_spread"] += 1
                continue
            # Skip extreme prices (too close to 0 or 100)
            if market.mid_price < 5 or market.mid_price > 95:
                skipped["extreme"] += 1
                continue
            # Prefer markets expiring soon
            days = self._days_until_expiry(market)
            if days > self.config.max_expiry_days:
                skipped["too_far"] += 1
                continue

            candidates.append(market)

        log.info("filter_stats", **skipped)
        log.info("scan_results", total=len(markets), candidates=len(candidates))

        # Rank: prefer wider spreads, higher volume, and sooner expiry
        def score(m: KalshiMarket) -> float:
            days = max(0.1, self._days_until_expiry(m))
            # Spread value * volume, boosted for near-term expiry
            expiry_boost = 7.0 / days  # 7x boost for same-day vs 7-day
            return m.spread * max(m.volume, 1) * expiry_boost

        candidates.sort(key=score, reverse=True)

        # Add new markets up to our limit
        for market in candidates[:self.config.max_markets]:
            if market.ticker not in self.active_quotes:
                initial_inventory = self._known_positions.get(market.ticker, 0)
                self.active_quotes[market.ticker] = MarketQuote(
                    ticker=market.ticker,
                    title=market.title,
                    yes_inventory=initial_inventory,
                )
                log.info(
                    "market_added",
                    ticker=market.ticker,
                    title=market.title[:60],
                    spread=market.spread,
                    volume=market.volume,
                    expiry_days=round(self._days_until_expiry(market), 1),
                    existing_inventory=initial_inventory,
                )

        # Remove markets that are no longer attractive
        active_tickers = {m.ticker for m in candidates}
        to_remove = [
            t for t in self.active_quotes
            if t not in active_tickers and abs(self.active_quotes[t].yes_inventory) == 0
        ]
        for ticker in to_remove:
            await self._cancel_quotes(ticker)
            del self.active_quotes[ticker]

    async def _update_quotes(self) -> None:
        """Update quotes in all active markets."""
        tasks = [
            self._quote_market(ticker, quote)
            for ticker, quote in self.active_quotes.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for ticker, result in zip(self.active_quotes.keys(), results):
            if isinstance(result, Exception):
                log.error("quote_update_exception", ticker=ticker, error=str(result))

    async def _quote_market(self, ticker: str, quote: MarketQuote) -> None:
        """Place or update quotes in a single market."""
        try:
            market = await self.connector.get_market(ticker)
        except Exception as e:
            log.warning("market_fetch_failed", ticker=ticker, error=str(e))
            return

        if market.status not in ("active", "open"):
            return

        # Use real inventory from Kalshi positions
        real_inventory = self._known_positions.get(ticker, 0)
        quote.yes_inventory = real_inventory

        # Calculate our quote prices
        mid = market.mid_price
        half_spread = self.config.quote_spread_cents / 2

        # Inventory skew: if we're long YES, lower our bid and raise our ask
        skew = 0
        if abs(real_inventory) > 0:
            inventory_ratio = real_inventory / self.config.max_position_per_market
            skew = int(inventory_ratio * self.config.inventory_skew * 10)

        our_bid = int(mid - half_spread - skew)  # Buy YES price
        our_ask = int(mid + half_spread - skew)  # Sell YES price

        # Clamp to valid range
        our_bid = max(1, min(98, our_bid))
        our_ask = max(our_bid + 1, min(99, our_ask))

        # Check position limits using REAL inventory
        if abs(real_inventory) >= self.config.max_position_per_market:
            # Only quote the reducing side
            if real_inventory > 0:
                our_bid = 0  # Don't buy more YES
            else:
                our_ask = 0  # Don't sell more YES

        # Place/update orders
        if self.paper_mode:
            await self._paper_quote(ticker, quote, market, our_bid, our_ask)
        else:
            try:
                await self._live_quote(ticker, quote, our_bid, our_ask)
            except Exception as e:
                log.error("live_quote_error", ticker=ticker, error=str(e))

        quote.last_update = time.time()

    async def _paper_quote(
        self,
        ticker: str,
        quote: MarketQuote,
        market: KalshiMarket,
        bid_price: int,
        ask_price: int,
    ) -> None:
        """Simulate market making in paper mode."""
        contracts_per_fill = min(10, self.config.max_position_per_market // 10)

        # Simulate bid fill (we buy YES)
        if bid_price > 0 and bid_price >= market.yes_bid and market.volume > 0:
            fill_chance = min(0.3, (bid_price - market.yes_bid + 1) * 0.1)
            import random
            if random.random() < fill_chance:
                quote.yes_inventory += contracts_per_fill
                cost = contracts_per_fill * bid_price / 100
                self._paper_balance -= cost
                quote.total_filled += contracts_per_fill
                log.info("paper_fill_bid", ticker=ticker, price=bid_price, count=contracts_per_fill)

        # Simulate ask fill (we sell YES / buy NO)
        if ask_price > 0 and ask_price <= market.yes_ask and market.volume > 0:
            fill_chance = min(0.3, (market.yes_ask - ask_price + 1) * 0.1)
            import random
            if random.random() < fill_chance:
                quote.yes_inventory -= contracts_per_fill
                revenue = contracts_per_fill * ask_price / 100
                self._paper_balance += revenue
                quote.total_filled += contracts_per_fill
                log.info("paper_fill_ask", ticker=ticker, price=ask_price, count=contracts_per_fill)

        # Calculate realized P&L from round trips
        if quote.total_filled > 0:
            self._total_pnl = sum(
                q.total_filled // 2 * self.config.quote_spread_cents / 100
                for q in self.active_quotes.values()
            )
            self._total_trades = sum(q.total_filled for q in self.active_quotes.values())

    async def _live_quote(
        self, ticker: str, quote: MarketQuote, bid_price: int, ask_price: int
    ) -> None:
        """Place real orders on Kalshi."""
        log.info(
            "live_quote_start",
            ticker=ticker, bid_price=bid_price, ask_price=ask_price,
            inventory=quote.yes_inventory,
        )

        # Cancel existing orders for this market
        await self._cancel_quotes(ticker)

        contracts = self.config.order_size

        # Place bid order (buy YES)
        if bid_price > 0:
            try:
                quote.bid_order = await self.connector.place_order(
                    ticker=ticker,
                    side="yes",
                    action="buy",
                    count=contracts,
                    price=bid_price,
                )
                log.info("bid_placed", ticker=ticker, price=bid_price, contracts=contracts)
            except Exception as e:
                log.warning("bid_failed", ticker=ticker, price=bid_price, error=str(e))

        # Place ask order (sell YES = buy NO)
        if ask_price > 0:
            try:
                no_price = 100 - ask_price
                quote.ask_order = await self.connector.place_order(
                    ticker=ticker,
                    side="no",
                    action="buy",
                    count=contracts,
                    price=no_price,
                )
                log.info("ask_placed", ticker=ticker, ask_price=ask_price, no_price=no_price, contracts=contracts)
            except Exception as e:
                log.warning("ask_failed", ticker=ticker, ask_price=ask_price, error=str(e))

    async def _cancel_quotes(self, ticker: str) -> None:
        """Cancel all our orders in a market."""
        quote = self.active_quotes.get(ticker)
        if not quote:
            return

        if not self.paper_mode:
            try:
                await self.connector.cancel_all_orders(ticker)
            except Exception:
                pass

        quote.bid_order = None
        quote.ask_order = None

    def get_status(self) -> dict:
        """Get strategy status for the dashboard."""
        return {
            "strategy": "kalshi_mm",
            "active_markets": len(self.active_quotes),
            "total_pnl": round(self._total_pnl, 2),
            "total_trades": self._total_trades,
            "markets": [
                {
                    "ticker": q.ticker,
                    "title": q.title[:50],
                    "inventory": q.yes_inventory,
                    "filled": q.total_filled,
                }
                for q in self.active_quotes.values()
            ],
        }

    async def on_shutdown(self) -> None:
        """Cancel all orders on shutdown."""
        log.info("kalshi_mm_shutting_down", markets=len(self.active_quotes))
        for ticker in list(self.active_quotes.keys()):
            await self._cancel_quotes(ticker)
