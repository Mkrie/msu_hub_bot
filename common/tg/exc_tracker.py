import asyncio
import re
from functools import cached_property
from typing import Optional

import aiogram
import aiohttp

from msu_hub_bot.settings import settings
from msu_hub_bot.redaction import redact


class TextNormalizer:
    types = re.compile(r'''
        (?P<hex>
            \b0[xX][0-9a-fA-F]+\b
        ) |
        (?P<float>
            -\d+\.\d+\b |
            \b\d+\.\d+\b
        ) |
        (?P<int>
            -\d+\b |
            \b\d+\b
        )
    ''', re.X)

    @classmethod
    def normalize(cls, text: str) -> str:
        def _handle_match(match):
            for key, value in match.groupdict().items():
                if value is not None:
                    return f'<{key}>'
            return ''

        return cls.types.sub(_handle_match, text)


class TelegramExceptionsTrackerAPI:
    api_url = settings.exception_tracker_url
    request_timer = 60
    timeout = 30

    def __init__(self):
        self.records = set()
        self.loop_task: Optional[asyncio.Task] = None

    @cached_property
    def session(self) -> aiohttp.ClientSession:
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        return aiohttp.ClientSession(timeout=timeout)

    async def close(self):
        if self.loop_task and not self.loop_task.cancelled():
            self.loop_task.cancel()
        if not self.session.closed:
            await self.session.close()

    def _extract_records(self) -> list:
        result = []
        for code, name, description in self.records:
            result.append(dict(code=code, name=name, description=description))

        self.records.clear()
        return result

    async def _loop(self):
        try:
            while True:
                await asyncio.sleep(self.request_timer)
                await self.send_accumulated()
        except asyncio.CancelledError:
            await self.send_accumulated()

    def patch_aiogram(self):
        if aiogram.bot.api.check_result.__name__ != 'check_result':
            raise RuntimeError('Call this patch exactly once')

        def decorator(check_result_real):
            def check_result_wrapper(method_name: str, content_type: str, status_code: int, body: str):
                try:
                    return check_result_real(method_name, content_type, status_code, body)
                except aiogram.exceptions.TelegramAPIError as exc:
                    name, description = type(exc).__name__, '; '.join(exc.args)
                    self.add_record(status_code, name, description)
                    raise

            return check_result_wrapper

        aiogram.bot.api.check_result = decorator(aiogram.bot.api.check_result)

    def add_record(self, code: int, exc_name: str, exc_description: str):
        """
        Exceptions tracker

        :param code: response code, for example: 400
        :param exc_name: aiogram exception name, for example: 'BadRequest'
        :param exc_description: aiogram exception description,
            for example: 'Reply message not found'
        :return:
        """
        if not self.api_url:
            return
        record = (code, exc_name, TextNormalizer.normalize(redact(exc_description)))
        self.records.add(record)

    async def send_accumulated(self):
        """
        Make request with collected records
        """
        if not self.api_url or len(self.records) == 0:
            return

        records = self._extract_records()
        async with self.session.post(self.api_url + 'exception', json=records) as response:
            if response.status != 200:
                pass

    async def start_loop(self):
        """
        Track exceptions in loop

        Usage:
            exc_tracker = TelegramExceptionsTrackerAPI()
            exc_tracker.patch_aiogram()
            await exc_tracker.start()
        """
        if self.api_url:
            self.loop_task = asyncio.create_task(self._loop())

    async def track(self, code: int, exc_name: str, exc_description: str):
        """Track one exception"""
        self.add_record(code, exc_name, exc_description)
        await self.send_accumulated()


class TelegramExceptionsTracker:
    def __init__(self):
        self.api = TelegramExceptionsTrackerAPI()

    async def on_startup(self):
        self.api.patch_aiogram()
        await self.api.start_loop()

    async def on_shutdown(self):
        return await self.api.close()
