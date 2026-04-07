"""Main trading engine — async event loop coordinating everything."""

from __future__ import annotations

import asyncio
import signal

from quicksand.config import Config
from quicksand.connectors.exchange import ExchangeConnector
from quicksand.core.order_manager import OrderManager
from quicksand.core.portfolio import Portfolio
from quicksand.core.risk_manager import RiskManager
from quicksand.strategies.base import BaseStrategy
from quicksand.strategies.funding_arb import FundingArbStrategy
from quicksand.utils.logging import get_logger

log = get_logger("engine")


class Engine:
    """Orchestrates exchange connections, strategies, and risk management."""

    def __init__(self, config: Config):
        self.config = config
        self.exchanges: dict[str, ExchangeConnector] = {}
        self.portfolio = Portfolio()
        self.risk_manager = RiskManager(config.risk, self.portfolio)
        self.order_manager = OrderManager()
        self.strategies: list[BaseStrategy] = []
        self._running = False

    async def start(self) -> None:
        """Initialize exchanges, strategies, and start the main loop."""
        log.info("engine_starting", mode=self.config.mode)

        # Set paper mode on order manager
        self.order_manager.set_paper_mode(self.config.is_paper)

        # Connect to exchanges
        await self._connect_exchanges()

        # Fetch initial balances and set portfolio capital
        await self._init_portfolio()

        # Initialize strategies
        self._init_strategies()

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        # Main loop
        self._running = True
        log.info(
            "engine_started",
            exchanges=list(self.exchanges.keys()),
            strategies=[s.name for s in self.strategies],
            equity=self.portfolio.equity,
            mode=self.config.mode,
        )

        await self._run_loop()

    async def stop(self) -> None:
        """Graceful shutdown."""
        if not self._running:
            return
        self._running = False
        log.info("engine_stopping")

        # Shutdown strategies (they may close positions)
        for strategy in self.strategies:
            try:
                await strategy.on_shutdown()
            except Exception as e:
                log.error("strategy_shutdown_error", strategy=strategy.name, error=str(e))

        # Close exchange connections
        for name, connector in self.exchanges.items():
            try:
                await connector.close()
            except Exception as e:
                log.error("exchange_close_error", exchange=name, error=str(e))

        log.info("engine_stopped", final_equity=self.portfolio.equity)

    async def _connect_exchanges(self) -> None:
        """Connect to all configured exchanges."""
        for name, exchange_config in self.config.exchanges.items():
            connector = ExchangeConnector(
                exchange_id=name,
                api_key=exchange_config.api_key,
                secret=exchange_config.secret,
                sandbox=exchange_config.sandbox,
            )
            try:
                await connector.connect()
                self.exchanges[name] = connector
            except Exception as e:
                log.error("exchange_connect_failed", exchange=name, error=str(e))

        if not self.exchanges:
            raise RuntimeError("No exchanges connected. Check your config.")

    async def _init_portfolio(self) -> None:
        """Fetch balances from all exchanges and set initial capital."""
        total_usdt = 0.0
        for name, connector in self.exchanges.items():
            try:
                balance = await connector.fetch_balance()
                total_usdt += balance.total_usdt
                log.info(
                    "balance_fetched",
                    exchange=name,
                    total_usdt=balance.total_usdt,
                    assets=balance.total,
                )
            except Exception as e:
                log.warning("balance_fetch_failed", exchange=name, error=str(e))

        self.portfolio.initial_capital = total_usdt
        self.portfolio.cash = total_usdt
        self.portfolio._peak_equity = total_usdt
        self.portfolio._daily_start_equity = total_usdt

        log.info("portfolio_initialized", total_capital=total_usdt)

    def _init_strategies(self) -> None:
        """Initialize enabled strategies."""
        if self.config.strategies.funding_arb.enabled:
            strategy = FundingArbStrategy(
                config=self.config,
                portfolio=self.portfolio,
                risk_manager=self.risk_manager,
                order_manager=self.order_manager,
                exchanges=self.exchanges,
            )
            self.strategies.append(strategy)
            log.info("strategy_loaded", name="funding_arb")

    async def _run_loop(self) -> None:
        """Main event loop — tick strategies and check risk."""
        tick_interval = 10  # seconds between ticks

        while self._running:
            try:
                # Continuous risk check
                risk_check = self.risk_manager.check_continuous()
                if not risk_check.allowed:
                    log.warning("risk_check_failed", reason=risk_check.reason)
                    # If circuit breaker tripped, stop opening new positions
                    # but keep monitoring (strategies handle their own shutdown)

                # Tick all strategies
                for strategy in self.strategies:
                    try:
                        await strategy.on_tick()
                    except Exception as e:
                        log.error("strategy_tick_error", strategy=strategy.name, error=str(e))

                # Log portfolio summary periodically
                log.info("tick", **self.portfolio.summary())

            except Exception as e:
                log.error("loop_error", error=str(e))

            await asyncio.sleep(tick_interval)
