"""Abstract base strategy interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from quicksand.config import Config
from quicksand.connectors.exchange import ExchangeConnector
from quicksand.core.order_manager import OrderManager
from quicksand.core.portfolio import Portfolio
from quicksand.core.risk_manager import RiskManager


class BaseStrategy(ABC):
    """All strategies implement this interface."""

    def __init__(
        self,
        config: Config,
        portfolio: Portfolio,
        risk_manager: RiskManager,
        order_manager: OrderManager,
        exchanges: dict[str, ExchangeConnector],
    ):
        self.config = config
        self.portfolio = portfolio
        self.risk = risk_manager
        self.orders = order_manager
        self.exchanges = exchanges

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy name for logging."""
        ...

    @abstractmethod
    async def on_tick(self) -> None:
        """Called on each engine tick. Scan for opportunities, manage positions."""
        ...

    @abstractmethod
    async def on_shutdown(self) -> None:
        """Graceful shutdown — close/flatten positions if needed."""
        ...
