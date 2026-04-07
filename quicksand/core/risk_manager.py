"""Risk manager — hard limits that gate every order."""

from __future__ import annotations

from dataclasses import dataclass

from quicksand.config import RiskConfig
from quicksand.core.portfolio import Portfolio
from quicksand.utils.logging import get_logger

log = get_logger("risk")


@dataclass
class RiskCheck:
    allowed: bool
    reason: str = ""


class RiskManager:
    """Enforces position limits, drawdown limits, and daily loss limits.

    Every order must pass through check_order() before submission.
    This is a hard gate, not advisory.
    """

    def __init__(self, config: RiskConfig, portfolio: Portfolio):
        self.config = config
        self.portfolio = portfolio
        self._circuit_breaker_tripped = False

    @property
    def is_killed(self) -> bool:
        return self._circuit_breaker_tripped

    def check_new_position(self, notional: float, leverage: float = 1.0) -> RiskCheck:
        """Check if a new position is allowed."""
        if self._circuit_breaker_tripped:
            return RiskCheck(False, "Circuit breaker is tripped — all trading halted")

        # Check daily loss limit
        if abs(self.portfolio.daily_pnl_pct) > self.config.daily_loss_limit_pct:
            if self.portfolio.daily_pnl < 0:
                self._trip_circuit_breaker("daily_loss_limit")
                return RiskCheck(False, f"Daily loss limit exceeded: {self.portfolio.daily_pnl_pct:.2%}")

        # Check max drawdown
        if self.portfolio.drawdown_pct > self.config.max_drawdown_pct:
            self._trip_circuit_breaker("max_drawdown")
            return RiskCheck(False, f"Max drawdown exceeded: {self.portfolio.drawdown_pct:.2%}")

        # Check max open positions
        if len(self.portfolio.arb_positions) >= self.config.max_open_positions:
            return RiskCheck(False, f"Max open positions reached: {self.config.max_open_positions}")

        # Check position size as % of equity
        equity = self.portfolio.equity
        if equity > 0 and notional / equity > self.config.max_position_pct:
            return RiskCheck(
                False,
                f"Position too large: {notional / equity:.1%} > {self.config.max_position_pct:.1%}",
            )

        # Check leverage
        if leverage > self.config.max_leverage:
            return RiskCheck(False, f"Leverage too high: {leverage}x > {self.config.max_leverage}x")

        return RiskCheck(True)

    def check_continuous(self) -> RiskCheck:
        """Continuous risk monitoring — call on every tick.

        Returns a risk check. If not allowed, all positions should be unwound.
        """
        self.portfolio.update_peak()

        if self._circuit_breaker_tripped:
            return RiskCheck(False, "Circuit breaker active")

        if self.portfolio.daily_pnl_pct < -self.config.daily_loss_limit_pct:
            self._trip_circuit_breaker("daily_loss_limit")
            return RiskCheck(False, f"Daily loss: {self.portfolio.daily_pnl_pct:.2%}")

        if self.portfolio.drawdown_pct > self.config.max_drawdown_pct:
            self._trip_circuit_breaker("max_drawdown")
            return RiskCheck(False, f"Drawdown: {self.portfolio.drawdown_pct:.2%}")

        return RiskCheck(True)

    def reset_circuit_breaker(self) -> None:
        """Manually reset the circuit breaker. Use with caution."""
        log.warning("circuit_breaker_reset")
        self._circuit_breaker_tripped = False

    def reset_daily(self) -> None:
        """Reset daily tracking. Does NOT reset circuit breaker."""
        self.portfolio.reset_daily()

    def _trip_circuit_breaker(self, reason: str) -> None:
        if not self._circuit_breaker_tripped:
            self._circuit_breaker_tripped = True
            log.critical(
                "circuit_breaker_tripped",
                reason=reason,
                equity=self.portfolio.equity,
                daily_pnl=self.portfolio.daily_pnl,
                drawdown=self.portfolio.drawdown_pct,
            )
