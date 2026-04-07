"""Web dashboard for Quicksand trading bot.

A simple FastAPI app that shows:
- Current balance and P&L
- Equity curve chart
- Recent trades
- Start/stop controls
- Live status

Designed to be bookmarked on a phone — responsive, minimal, works everywhere.
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
from quicksand.core.engine import Engine
from quicksand.utils.logging import setup_logging, get_logger

log = get_logger("web")

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Global state
_engine: Engine | None = None
_engine_task: asyncio.Task | None = None
_config: Config | None = None
_trade_history: list[dict] = []
_equity_history: list[dict] = []
_start_time: float | None = None


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
    await _stop_engine()


app = FastAPI(title="Quicksand", lifespan=lifespan)


# ── Pages ──────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ── API ────────────────────────────────────────────────────────────────────


@app.get("/api/status")
async def get_status():
    """Current bot status, balance, and P&L."""
    if _engine is None or not _engine._running:
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

    p = _engine.portfolio
    uptime = time.time() - _start_time if _start_time else 0

    return {
        "running": True,
        "mode": _config.mode if _config else "paper",
        "equity": round(p.equity, 2),
        "cash": round(p.cash, 2),
        "initial_capital": round(p.initial_capital, 2),
        "daily_pnl": round(p.daily_pnl, 2),
        "daily_pnl_pct": round(p.daily_pnl_pct * 100, 3),
        "total_pnl": round(p.equity - p.initial_capital, 2),
        "total_pnl_pct": round((p.equity / p.initial_capital - 1) * 100, 2) if p.initial_capital > 0 else 0,
        "positions": len(p.arb_positions),
        "utilization_pct": round(p.utilization_pct * 100, 1),
        "drawdown_pct": round(p.drawdown_pct * 100, 2),
        "uptime": int(uptime),
    }


@app.get("/api/positions")
async def get_positions():
    """Current open positions."""
    if _engine is None:
        return []

    positions = []
    for pos_id, arb in _engine.portfolio.arb_positions.items():
        positions.append({
            "id": pos_id,
            "pair": arb.pair,
            "exchange": arb.exchange,
            "direction": "Long Spot / Short Perp" if arb.entry_funding_rate > 0 else "Short Spot / Long Perp",
            "notional": round(arb.notional, 2),
            "pnl": round(arb.total_pnl, 2),
            "funding_collected": round(arb.funding_collected, 2),
            "entry_rate": f"{arb.entry_funding_rate * 100:.4f}%",
            "opened": int(arb.opened_at),
        })
    return positions


@app.get("/api/trades")
async def get_trades():
    """Recent trade history."""
    return _trade_history[-50:]  # Last 50 trades


@app.get("/api/equity")
async def get_equity():
    """Equity curve data for charting."""
    return _equity_history[-500:]  # Last 500 data points


@app.post("/api/start")
async def start_bot(capital: float = 0):
    """Start the trading engine."""
    global _engine, _engine_task, _start_time, _config

    if _engine is not None and _engine._running:
        return JSONResponse({"error": "Bot is already running"}, status_code=400)

    try:
        _config = load_config("config.yaml")
    except FileNotFoundError:
        return JSONResponse({"error": "config.yaml not found"}, status_code=400)

    _engine = Engine(_config)
    _start_time = time.time()

    # Start engine in background task
    _engine_task = asyncio.create_task(_run_engine_with_tracking())

    return {"status": "started", "mode": _config.mode}


@app.post("/api/stop")
async def stop_bot():
    """Stop the trading engine gracefully."""
    await _stop_engine()
    return {"status": "stopped"}


# ── Internal ───────────────────────────────────────────────────────────────


async def _run_engine_with_tracking():
    """Run the engine while recording equity snapshots."""
    global _engine

    try:
        # Initialize
        _engine.order_manager.set_paper_mode(_engine.config.is_paper)
        await _engine._connect_exchanges()
        await _engine._init_portfolio()
        _engine._init_strategies()
        _engine._running = True

        log.info("engine_started_via_dashboard", equity=_engine.portfolio.equity)

        # Main loop with equity tracking
        while _engine._running:
            try:
                risk_check = _engine.risk_manager.check_continuous()
                if not risk_check.allowed:
                    log.warning("risk_check_failed", reason=risk_check.reason)

                for strategy in _engine.strategies:
                    try:
                        await strategy.on_tick()
                    except Exception as e:
                        log.error("strategy_tick_error", strategy=strategy.name, error=str(e))

                # Record equity snapshot
                _equity_history.append({
                    "time": int(time.time() * 1000),
                    "equity": round(_engine.portfolio.equity, 2),
                })

                # Track closed positions as trades
                # (In a production system, we'd hook into the portfolio's close events)

            except Exception as e:
                log.error("loop_error", error=str(e))

            await asyncio.sleep(10)

    except Exception as e:
        log.error("engine_crash", error=str(e))
    finally:
        if _engine:
            _engine._running = False


async def _stop_engine():
    """Stop the engine and cancel the task."""
    global _engine, _engine_task

    if _engine is not None:
        await _engine.stop()
        _engine = None

    if _engine_task is not None and not _engine_task.done():
        _engine_task.cancel()
        _engine_task = None
