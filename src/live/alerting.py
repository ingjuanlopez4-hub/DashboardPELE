import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import aiohttp

logger = logging.getLogger("alerting")

ALERT_RATE_LIMIT_SECONDS = 60
MAX_QUEUE_SIZE = 100


class AlertManager:
    def __init__(
        self,
        discord_webhook_url: str = "",
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
        alert_on_critical: bool = True,
        alert_on_warning: bool = False,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._discord_url = discord_webhook_url
        self._telegram_token = telegram_bot_token
        self._telegram_chat_id = telegram_chat_id
        self._alert_critical = alert_on_critical
        self._alert_warning = alert_on_warning
        self._session = session

        self._rate_limiter: dict[str, float] = {}
        self._alert_queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self._sender_task: asyncio.Task | None = None
        self._running = False

    def set_session(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def send_alert(
        self,
        severity: str,
        title: str,
        message: str,
        alert_type: str = "general",
    ) -> None:
        if severity == "CRITICAL" and not self._alert_critical:
            return
        if severity == "WARNING" and not self._alert_warning:
            return

        now = time.time()
        last = self._rate_limiter.get(alert_type, 0)
        if now - last < ALERT_RATE_LIMIT_SECONDS:
            return
        self._rate_limiter[alert_type] = now

        payload = {
            "severity": severity,
            "title": title,
            "message": message,
            "timestamp": time.time(),
        }

        try:
            await asyncio.wait_for(
                self._alert_queue.put(payload), timeout=1.0
            )
        except asyncio.TimeoutError:
            logger.warning("Alert queue full — alert dropped: %s", title)

    async def _sender_loop(self) -> None:
        while self._running:
            try:
                payload = await asyncio.wait_for(
                    self._alert_queue.get(), timeout=1.0
                )
                await self._send_to_channels(payload)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in alert sender")

    async def _send_to_channels(self, payload: dict[str, Any]) -> None:
        severity = payload.get("severity", "INFO")
        title = payload.get("title", "")
        message = payload.get("message", "")

        if self._discord_url:
            try:
                await self._send_discord(severity, title, message)
            except Exception:
                logger.exception("Discord alert failed")

        if self._telegram_token and self._telegram_chat_id:
            try:
                await self._send_telegram(severity, title, message)
            except Exception:
                logger.exception("Telegram alert failed")

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _send_discord(self, severity: str, title: str, message: str) -> None:
        color_map = {
            "CRITICAL": 0xFF0000,
            "WARNING": 0xFFA500,
            "INFO": 0x00FF00,
        }
        payload = {
            "embeds": [{
                "title": title,
                "description": message[:2000],
                "color": color_map.get(severity, 0x0000FF),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }],
        }
        session = await self._ensure_session()
        async with session.post(
            self._discord_url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status not in (200, 204):
                logger.warning("Discord alert HTTP %d", resp.status)

    async def _send_telegram(self, severity: str, title: str, message: str) -> None:
        text = f"[{severity}] {title}\n\n{message[:3000]}"
        url = f"https://api.telegram.org/bot{self._telegram_token}/sendMessage"
        payload = {
            "chat_id": self._telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        session = await self._ensure_session()
        async with session.post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("Telegram alert HTTP %d", resp.status)

    async def start(self) -> None:
        self._running = True
        self._sender_task = asyncio.create_task(self._sender_loop())
        logger.info("AlertManager started")

    async def stop(self) -> None:
        self._running = False
        if self._sender_task and not self._sender_task.done():
            self._sender_task.cancel()
            try:
                await self._sender_task
            except asyncio.CancelledError:
                pass
        logger.info("AlertManager stopped")
