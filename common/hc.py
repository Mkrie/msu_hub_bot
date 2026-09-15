import asyncio
from contextlib import suppress
from typing import Optional

import aiohttp
from throttler import ExecutionTimer


class HealthCheck:
    repeat_time = 60

    def __init__(self, url: str):
        self.url = url
        self._task: Optional[asyncio.Task] = None

    async def ping(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.post(self.url, ssl=None) as response:
                return await response.read()

    async def _loop(self):
        et = ExecutionTimer(period=self.repeat_time)
        while True:
            async with et:
                with suppress(asyncio.CancelledError, asyncio.TimeoutError, aiohttp.ClientError):
                    await self.ping()

    async def start(self):
        if not self.url:
            return
        if self._task:
            await self.stop()
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            if not self._task.cancelled():
                self._task.cancel()
            self._task = None

    async def __aenter__(self):
        await self.start()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
