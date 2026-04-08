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
- Scale across 20+ markets simultaneously
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from quicksand.connectors.kalshi import KalshiConnector, KalshiMarket, KalshiOrder
from quicksand.utils.logging import get_logger

log = get_logger("kalshi_mm")


@dataclass
class MarketMakingConfig:
    """Configuration for the market making strategy."""

    min_spread_cents: int = 3  # Minimum spread to quote (in cents)
    quote_spread_cents: int = 2  # How wide our quotes are inside the spread
    max_position_per_market: int = 100  # Max contracts per market
    max_total_exposure: float = 2500  # Max total $ deployed across all markets
    max_markets: int = 8  # Max simultaneous markets to make (keep low for rate limits)
    min_volume: int = 50  # Minimum 24h volume to consider a market
    min_open_interest: int = 20  # Minimum open interest
    requote_interval_seconds: int = 60  # How often to check and re-quote
    inventory_skew: float = 0.3  # Skew quotes when inventory is one-sided


@dataclass
class MarketQuote:
    """Active quotes in a market."""

    ticker: str
    title: str
    bid_order: KalshiOrder | None = None  # Our buy YES order
    ask_order: KalshiOrder | None = None  # Our sell YES (buy NO) order
    yes_inventory: int = 0  # Net YES contracts held
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

    @property
    def name(self) -> str:
        return "kalshi_mm"

    async def initialize(self, starting_balance: float) -> None:
        """Initialize with starting capital."""
        self._paper_balance = starting_balance
        log.info("kalshi_mm_initialized", balance=starting_balance, paper=self.paper_mode)

    async def on_tick(self) -> None:
        """Main tick: scan markets, update quotes, manage inventory."""
        now = time.time()

        if now - self._last_scan < self.config.requote_interval_seconds:
            return
        self._last_scan = now

        # 1. Check fills on existing orders (live mode only)
        if not self.paper_mode:
            await self._check_fills()

        # 2. Scan for good markets to make
        await self._scan_markets()

        # 3. Update existing quotes
        await self._update_quotes()

        # 4. Log status
        total_exposure = sum(
            abs(q.yes_inventory) * 0.50  # Rough avg price per contract
            for q in self.active_quotes.values()
        )
        log.info(
            "mm_tick",
            active_markets=len(self.active_quotes),
            total_pnl=round(self._total_pnl, 2),
            total_trades=self._total_trades,
            total_exposure=round(total_exposure, 2),
        )

    async def _scan_markets(self) -> None:
        """Find liquid markets with wide spreads worth making."""
        all_markets = []

        try:
            # Focus on liquid series with known spreads (fewer API calls)
            for series in ["KXINX", "KXBTC", "KXETH", "KXFED", "KXGDP"]:
                try:
                    series_markets = await self.connector.get_markets(
                        status="open", limit=30, series_ticker=series
                    )
                    all_markets.extend(series_markets)
                except Exception:
                    continue

            # Also scan a few top events (limit to reduce API calls)
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

            markets = all_markets
            log.info("scan_fetched", total_fetched=len(markets))
        except Exception as e:
            log.warning("market_scan_failed", error=str(e))
            return

        # Debug: log first few markets to see what we're getting
        if markets:
            sample = markets[:3]
            for m in sample:
                log.info(
                    "market_sample",
                    ticker=m.ticker,
                    title=m.title[:40] if m.title else "",
                    yes_bid=m.yes_bid,
                    yes_ask=m.yes_ask,
                    spread=m.spread,
                    volume=m.volume,
                    oi=m.open_interest,
                    mid=m.mid_price,
                    status=m.status,
                )
        else:
            log.warning("no_markets_returned")

        # Filter and rank markets
        candidates = []
        skipped = {"status": 0, "no_spread": 0, "extreme": 0, "no_prices": 0}
        for market in markets:
            if market.status not in ("active", "open"):
                skipped["status"] += 1
                continue
            # Must have actual prices on both sides
            if market.yes_bid <= 0 or market.yes_ask <= 0:
                skipped["no_prices"] += 1
                continue
            if market.spread < self.config.min_spread_cents:
                skipped["no_spread"] += 1
                continue
            # Skip extreme prices (too close to 0 or 100)
            if market.mid_price < 10 or market.mid_price > 90:
                skipped["extreme"] += 1
                continue

            candidates.append(market)

        log.info("filter_stats", **skipped)

        log.info("scan_results", total=len(markets), candidates=len(candidates))

        # Rank by spread × volume (best opportunities first)
        candidates.sort(key=lambda m: m.spread * m.volume, reverse=True)

        # Add new markets up to our limit
        for market in candidates[:self.config.max_markets]:
            if market.ticker not in self.active_quotes:
                self.active_quotes[market.ticker] = MarketQuote(
                    ticker=market.ticker,
                    title=market.title,
                )
                log.info(
                    "market_added",
                    ticker=market.ticker,
                    title=market.title[:60],
                    spread=market.spread,
                    volume=market.volume,
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

        # Calculate our quote prices
        mid = market.mid_price
        half_spread = self.config.quote_spread_cents / 2

        # Inventory skew: if we're long YES, lower our bid and raise our ask
        skew = 0
        if abs(quote.yes_inventory) > 0:
            inventory_ratio = quote.yes_inventory / self.config.max_position_per_market
            skew = int(inventory_ratio * self.config.inventory_skew * 10)

        our_bid = int(mid - half_spread - skew)  # Buy YES price
        our_ask = int(mid + half_spread - skew)  # Sell YES price

        # Clamp to valid range
        our_bid = max(1, min(98, our_bid))
        our_ask = max(our_bid + 1, min(99, our_ask))

        # Check position limits
        if abs(quote.yes_inventory) >= self.config.max_position_per_market:
            # Only quote the reducing side
            if quote.yes_inventory > 0:
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
        """Simulate market making in paper mode.

        Simple simulation: if our bid >= market's yes_bid or our ask <= market's yes_ask,
        assume we'd get filled at a rate proportional to volume.
        """
        contracts_per_fill = min(10, self.config.max_position_per_market // 10)

        # Simulate bid fill (we buy YES)
        if bid_price > 0 and bid_price >= market.yes_bid and market.volume > 0:
            # Probability of fill based on how aggressive our price is
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
            # Approximate: each round trip captures the spread
            round_trips = quote.total_filled // 2
            spread_profit = round_trips * self.config.quote_spread_cents / 100
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
        )

        # Cancel existing orders for this market
        await self._cancel_quotes(ticker)

        contracts = min(
            self.config.max_position_per_market // 4,
            50,  # Conservative per-order size
        )

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

    async def _check_fills(self) -> None:
        """Check if any of our resting orders have been filled."""
        try:
            fills = await self.connector.get_fills(limit=50)
        except Exception as e:
            log.warning("fill_check_failed", error=str(e))
            return

        for fill in fills:
            ticker = fill.get("ticker", "")
            quote = self.active_quotes.get(ticker)
            if not quote:
                continue

            side = fill.get("side", "")
            action = fill.get("action", "")
            count = fill.get("count", 0)

            # Parse fill price (dollar string or cents)
            price_str = fill.get("yes_price_dollars", "")
            if price_str:
                try:
                    fill_price_cents = float(price_str) * 100
                except (ValueError, TypeError):
                    fill_price_cents = fill.get("yes_price", 0)
            else:
                fill_price_cents = fill.get("yes_price", 0)

            fill_id = fill.get("trade_id", fill.get("fill_id", ""))
            if not fill_id or fill_id in self._processed_fills:
                continue
            self._processed_fills.add(fill_id)

            if side == "yes" and action == "buy":
                quote.yes_inventory += count
                quote.total_filled += count
                cost = count * fill_price_cents / 100
                log.info("fill_yes_buy", ticker=ticker, count=count, price=fill_price_cents, cost=cost)
            elif side == "no" and action == "buy":
                quote.yes_inventory -= count
                quote.total_filled += count
                revenue = count * (100 - fill_price_cents) / 100
                log.info("fill_no_buy", ticker=ticker, count=count, price=fill_price_cents, revenue=revenue)

        # Recalculate totals
        self._total_trades = sum(q.total_filled for q in self.active_quotes.values())
        self._total_pnl = sum(
            q.total_filled // 2 * self.config.quote_spread_cents / 100
            for q in self.active_quotes.values()
        )

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
