import asyncio
from contextlib import suppress
from typing import Optional

from aiogram.types import Chat


class ChatActioner:
    """
    Example usage:

        async with ChatActioner(message.chat, ChatActions.UPLOAD_PHOTO):
            image = await long_operation(arg)
        return await message.reply_photo(image)
    """
    repeat_time = 4.8

    def __init__(self, chat: Chat, action: str):
        self.chat = chat
        self.action = action

        self._task: Optional[asyncio.Task] = None

    async def _loop(self):
        with suppress(asyncio.CancelledError):
            while True:
                await self.chat.do(self.action)
                await asyncio.sleep(self.repeat_time)

    async def start(self):
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
