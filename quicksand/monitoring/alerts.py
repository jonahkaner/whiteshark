"""Telegram alert bot for trade notifications.

Sends you alerts when:
- A new position is opened
- A position is closed (with P&L)
- Daily P&L summary
- Circuit breaker trips (something went wrong)
- Bot starts/stops

Setup:
1. Message @BotFather on Telegram, create a bot, get the token
2. Message your bot, then visit: https://api.telegram.org/bot<TOKEN>/getUpdates
3. Find your chat_id in the response
4. Add token + chat_id to config.yaml
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx

from quicksand.utils.logging import get_logger

log = get_logger("alerts")


class TelegramAlerter:
    """Sends alerts via Telegram Bot API."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._client = httpx.AsyncClient(timeout=10)
        self._enabled = bool(bot_token and chat_id)

    async def send(self, message: str) -> None:
        """Send a message to the configured chat."""
        if not self._enabled:
            return

        try:
            await self._client.post(
                f"{self._base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
        except Exception as e:
            log.warning("telegram_send_failed", error=str(e))

    async def notify_start(self, mode: str, equity: float) -> None:
        await self.send(
            f"🟢 <b>Quicksand Started</b>\n"
            f"Mode: {mode.upper()}\n"
            f"Balance: ${equity:,.2f}"
        )

    async def notify_stop(self, equity: float, total_pnl: float) -> None:
        emoji = "🟢" if total_pnl >= 0 else "🔴"
        await self.send(
            f"⏹ <b>Quicksand Stopped</b>\n"
            f"Final Balance: ${equity:,.2f}\n"
            f"Total P&L: {emoji} ${total_pnl:+,.2f}"
        )

    async def notify_position_opened(
        self, pair: str, exchange: str, direction: str,
        notional: float, funding_rate: float,
    ) -> None:
        await self.send(
            f"📈 <b>New Position</b>\n"
            f"{pair} on {exchange}\n"
            f"Direction: {direction}\n"
            f"Size: ${notional:,.2f}\n"
            f"Funding Rate: {funding_rate:.4%} (annualized: {funding_rate * 3 * 365:.1%})"
        )

    async def notify_position_closed(
        self, pair: str, pnl: float, funding: float,
        fees: float, reason: str, duration_hours: float,
    ) -> None:
        emoji = "✅" if pnl >= 0 else "❌"
        await self.send(
            f"{emoji} <b>Position Closed</b>\n"
            f"{pair} — {reason}\n"
            f"P&L: ${pnl:+,.2f}\n"
            f"Funding Collected: ${funding:,.2f}\n"
            f"Fees: ${fees:,.2f}\n"
            f"Duration: {duration_hours:.1f}h"
        )

    async def notify_daily_summary(
        self, equity: float, daily_pnl: float, daily_pnl_pct: float,
        total_pnl: float, positions: int, trades_today: int,
    ) -> None:
        emoji = "🟢" if daily_pnl >= 0 else "🔴"
        await self.send(
            f"📊 <b>Daily Summary</b> — {datetime.now().strftime('%b %d')}\n"
            f"\n"
            f"Balance: ${equity:,.2f}\n"
            f"Today: {emoji} ${daily_pnl:+,.2f} ({daily_pnl_pct:+.2f}%)\n"
            f"Total P&L: ${total_pnl:+,.2f}\n"
            f"Open Positions: {positions}\n"
            f"Trades Today: {trades_today}"
        )

    async def notify_circuit_breaker(self, reason: str, equity: float) -> None:
        await self.send(
            f"🚨 <b>CIRCUIT BREAKER TRIPPED</b>\n"
            f"Reason: {reason}\n"
            f"Balance: ${equity:,.2f}\n"
            f"All trading halted. Manual review required."
        )

    async def close(self) -> None:
        await self._client.aclose()
