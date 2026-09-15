from contextlib import suppress

import aiogram
from aiogram.dispatcher.handler import CancelHandler
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.types import Message


class Skip777000(BaseMiddleware):
    @staticmethod
    async def on_pre_process_message(message: Message, _results):
        # Skip Telegram forwards from channels
        if message.is_automatic_forward:
            with suppress(aiogram.exceptions.BadRequest):
                await message.unpin()
            raise CancelHandler()
