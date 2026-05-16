import asyncio
import logging
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("async_manager")


class AsyncResourceManager:
    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._cleanup_tasks: List[asyncio.Task] = []
        self._resources: Dict[str, Any] = {}
        self._closed = False

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "PELE-Bot/1.0"}
            )
        return self._session

    def register_resource(self, name: str, resource: Any) -> None:
        self._resources[name] = resource

    def register_cleanup_task(self, task: asyncio.Task) -> None:
        self._cleanup_tasks.append(task)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        for name, resource in self._resources.items():
            if hasattr(resource, "stop"):
                try:
                    await resource.stop()
                except Exception:
                    logger.exception("Error stopping %s", name)
            elif hasattr(resource, "close"):
                try:
                    await resource.close()
                except Exception:
                    logger.exception("Error closing %s", name)

        for task in self._cleanup_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

        logger.info("AsyncResourceManager: all resources cleaned up")
