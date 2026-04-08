"""Web dashboard for Quicksand trading bot.

One-page app: balance, P&L, equity chart, positions, trades, start/stop.
Designed to be bookmarked on a phone.

Runs the Kalshi market maker as the primary strategy.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from quicksand.config import Config, load_config
from quicksand.connectors.kalshi import KalshiConnector
from quicksand.strategies.kalshi_mm import KalshiMarketMaker, MarketMakingConfig
from quicksand.utils.logging import setup_logging, get_logger

log = get_logger("web")

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Global state
_mm: KalshiMarketMaker | None = None
_connector: KalshiConnector | None = None
_task: asyncio.Task | None = None
_config: Config | None = None
_equity_history: list[dict] = []
_start_time: float | None = None
_initial_balance: float = 0
_running = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config
    try:
        _config = load_config("config.yaml")
    except FileNotFoundError:
        _config = Config()
    setup_logging(level=_config.logging.level)
    log.info("dashboard_ready")
    yield
    await _stop_bot()


app = FastAPI(title="Quicksand", lifespan=lifespan)


# ── Pages ──────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html")


# ── API ────────────────────────────────────────────────────────────────────


@app.get("/api/status")
async def get_status():
    """Current bot status, balance, and P&L."""
    if not _running or _mm is None:
        return {
            "running": False,
            "mode": _config.mode if _config else "paper",
            "equity": 0,
            "daily_pnl": 0,
            "daily_pnl_pct": 0,
            "total_pnl": 0,
            "total_pnl_pct": 0,
            "positions": 0,
            "uptime": 0,
        }

    # Get portfolio breakdown with realized/unrealized P&L
    realized_pnl = 0.0
    unrealized_pnl = 0.0
    total_fees = 0.0
    if _config.is_paper:
        equity = _mm._paper_balance
    else:
        try:
            summary = await _connector.get_portfolio_summary()
            equity = summary["total"]
            realized_pnl = summary["realized_pnl"]
            total_fees = summary["total_fees"]
            unrealized_pnl = (equity - _initial_balance) - realized_pnl
        except Exception:
            equity = _initial_balance + _mm._total_pnl

    total_pnl = equity - _initial_balance
    uptime = time.time() - _start_time if _start_time else 0

    return {
        "running": True,
        "mode": _config.mode if _config else "paper",
        "equity": round(equity, 2),
        "initial_capital": round(_initial_balance, 2),
        "daily_pnl": round(total_pnl, 2),
        "daily_pnl_pct": round(total_pnl / max(_initial_balance, 1) * 100, 3),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / max(_initial_balance, 1) * 100, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_fees": round(total_fees, 2),
        "positions": len(_mm.active_quotes),
        "utilization_pct": round(len(_mm.active_quotes) / max(_mm.config.max_markets, 1) * 100, 1),
        "drawdown_pct": 0,
        "uptime": int(uptime),
        "total_trades": _mm._total_trades,
    }


@app.get("/api/positions")
async def get_positions():
    """Current active market quotes."""
    if _mm is None:
        return []

    positions = []
    for ticker, quote in _mm.active_quotes.items():
        positions.append({
            "id": ticker,
            "pair": quote.title[:50] if quote.title else ticker,
            "exchange": "Kalshi",
            "direction": f"Inventory: {quote.yes_inventory:+d} YES",
            "notional": round(abs(quote.yes_inventory) * 0.50, 2),  # Approx
            "pnl": round(quote.total_filled // 2 * _mm.config.quote_spread_cents / 100, 2),
            "funding_collected": 0,
            "entry_rate": f"{quote.total_filled} fills",
            "opened": int(quote.last_update),
        })
    return positions


@app.get("/api/trades")
async def get_trades():
    """Recent fills from Kalshi."""
    if _connector is None or _config is None or _config.is_paper:
        return []

    try:
        fills = await _connector.get_fills(limit=50)
        return [
            {
                "symbol": f.get("ticker", ""),
                "direction": f"{f.get('side', '')} {f.get('action', '')}",
                "pnl": 0,
                "funding": 0,
                "fees": 0,
            }
            for f in fills
        ]
    except Exception:
        return []


@app.get("/api/debug/markets")
async def debug_markets():
    """Debug: show raw market data from Kalshi."""
    if _connector is None:
        return {"error": "Not connected"}
    try:
        # Fetch events to find active ones with liquidity
        events_data = await _connector._request("GET", "/events", params={
            "status": "open", "limit": 20,
        })
        events = events_data.get("events", [])
        event_info = [{"ticker": e.get("event_ticker", ""), "title": e.get("title", "")[:60], "category": e.get("category", "")} for e in events[:10]]

        # Try fetching markets from specific popular series
        raw_markets = []
        for series in ["KXINX", "KXBTC", "KXETH", "KXFED", "KXWEATHER", "KXGDP"]:
            try:
                d = await _connector._request("GET", "/markets", params={
                    "status": "open", "limit": 10, "series_ticker": series
                })
                raw_markets.extend(d.get("markets", []))
            except Exception:
                pass

        # Also try fetching from events
        for e in events[:5]:
            try:
                d = await _connector._request("GET", "/markets", params={
                    "status": "open", "limit": 10, "event_ticker": e.get("event_ticker", "")
                })
                raw_markets.extend(d.get("markets", []))
            except Exception:
                pass

        if not raw_markets:
            data = await _connector._request("GET", "/markets", params={
                "status": "open", "limit": 5,
            })
            raw_markets = data.get("markets", [])
        # Show price-relevant fields for each market
        summaries = []
        for m in raw_markets:
            summaries.append({
                "ticker": m.get("ticker", "")[:60],
                "title": (m.get("title") or m.get("no_sub_title", ""))[:60],
                "yes_bid": m.get("yes_bid_dollars"),
                "yes_ask": m.get("yes_ask_dollars"),
                "no_bid": m.get("no_bid_dollars"),
                "no_ask": m.get("no_ask_dollars"),
                "prev_yes_bid": m.get("previous_yes_bid_dollars"),
                "prev_yes_ask": m.get("previous_yes_ask_dollars"),
                "volume_24h": m.get("volume_24h_fp"),
                "oi": m.get("open_interest_fp"),
                "bid_size": m.get("yes_bid_size_fp"),
                "ask_size": m.get("yes_ask_size_fp"),
                "status": m.get("status"),
                "structure": m.get("price_level_structure"),
            })

        # Also try fetching an orderbook for the first market
        orderbook = None
        if raw_markets:
            ticker = raw_markets[0].get("ticker", "")
            try:
                orderbook = await _connector._request(
                    "GET", f"/markets/{ticker}/orderbook", params={"depth": 5}
                )
            except Exception as e:
                orderbook = {"error": str(e)}

        return {
            "total": len(raw_markets),
            "markets": summaries,
            "orderbook_sample": orderbook,
            "events": event_info,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/debug/portfolio")
async def debug_portfolio():
    """Debug: show raw balance and positions data from Kalshi API."""
    if _connector is None:
        return {"error": "Not connected"}
    try:
        balance_raw = await _connector._request("GET", "/portfolio/balance")
        positions_raw = await _connector._request("GET", "/portfolio/positions")
        return {
            "balance_raw": balance_raw,
            "positions_raw": positions_raw,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/debug/orders")
async def debug_orders():
    """Debug: show resting orders and recent fills from Kalshi."""
    if _connector is None:
        return {"error": "Not connected"}
    try:
        orders = await _connector.get_orders(status="resting")
        fills = await _connector.get_fills(limit=20)
        return {
            "resting_orders": [
                {
                    "order_id": o.order_id,
                    "ticker": o.ticker,
                    "side": o.side,
                    "action": o.action,
                    "count": o.count,
                    "price": o.price,
                    "status": o.status,
                    "filled": o.filled_count,
                }
                for o in orders
            ],
            "recent_fills": fills[:20],
            "active_quotes": {
                ticker: {
                    "bid_order": q.bid_order.order_id if q.bid_order else None,
                    "ask_order": q.ask_order.order_id if q.ask_order else None,
                    "inventory": q.yes_inventory,
                    "total_filled": q.total_filled,
                }
                for ticker, q in (_mm.active_quotes if _mm else {}).items()
            },
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/equity")
async def get_equity():
    return _equity_history[-500:]


@app.post("/api/start")
async def start_bot():
    """Start the Kalshi market maker."""
    global _mm, _connector, _task, _start_time, _config, _initial_balance, _running

    if _running:
        return JSONResponse({"error": "Bot is already running"}, status_code=400)

    try:
        _config = load_config("config.yaml")
    except FileNotFoundError:
        return JSONResponse({"error": "config.yaml not found"}, status_code=400)

    # Connect to Kalshi
    kalshi_cfg = _config.kalshi
    _connector = KalshiConnector(
        api_key_id=kalshi_cfg.api_key_id,
        private_key_path=kalshi_cfg.private_key_path,
        demo=kalshi_cfg.demo,
    )

    try:
        await _connector.connect()
    except Exception as e:
        return JSONResponse({"error": f"Failed to connect to Kalshi: {e}"}, status_code=500)

    # Get starting balance
    if not _config.is_paper and kalshi_cfg.private_key_path:
        try:
            _initial_balance = _config.initial_capital
        except Exception:
            _initial_balance = _config.initial_capital
    else:
        _initial_balance = _config.initial_capital

    # Create market maker
    mm_cfg = _config.strategies.kalshi_mm
    _mm = KalshiMarketMaker(
        connector=_connector,
        config=MarketMakingConfig(
            min_spread_cents=mm_cfg.min_spread_cents,
            quote_spread_cents=mm_cfg.quote_spread_cents,
            max_position_per_market=mm_cfg.max_position_per_market,
            max_total_exposure=mm_cfg.max_total_exposure,
            max_markets=mm_cfg.max_markets,
            min_volume=mm_cfg.min_volume,
            min_open_interest=mm_cfg.min_open_interest,
            requote_interval_seconds=mm_cfg.requote_interval_seconds,
            max_expiry_days=getattr(mm_cfg, 'max_expiry_days', 7),
            order_size=getattr(mm_cfg, 'order_size', 50),
        ),
        paper_mode=_config.is_paper,
    )
    await _mm.initialize(_initial_balance)

    _start_time = time.time()
    _running = True
    _task = asyncio.create_task(_run_loop())

    log.info("bot_started", mode=_config.mode, balance=_initial_balance, demo=kalshi_cfg.demo)
    return {"status": "started", "mode": _config.mode, "balance": _initial_balance}


@app.post("/api/stop")
async def stop_bot():
    await _stop_bot()
    return {"status": "stopped"}


# ── Internal ───────────────────────────────────────────────────────────────


async def _run_loop():
    """Main loop: tick the market maker and record equity."""
    global _running

    try:
        while _running:
            try:
                await _mm.on_tick()

                # Record equity
                if _config.is_paper:
                    equity = _mm._paper_balance
                else:
                    equity = _initial_balance + _mm._total_pnl

                _equity_history.append({
                    "time": int(time.time() * 1000),
                    "equity": round(equity, 2),
                })

            except Exception as e:
                log.error("tick_error", error=str(e))

            await asyncio.sleep(5)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error("loop_crash", error=str(e))
    finally:
        _running = False


async def _stop_bot():
    """Stop everything gracefully."""
    global _mm, _connector, _task, _running

    _running = False

    if _mm is not None:
        await _mm.on_shutdown()
        _mm = None

    if _connector is not None:
        await _connector.close()
        _connector = None

    if _task is not None and not _task.done():
        _task.cancel()
        _task = None

    log.info("bot_stopped")
