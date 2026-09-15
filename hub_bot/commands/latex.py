from contextlib import suppress

import aiogram
import cachetools
from aiogram.types import Message, InputMediaPhoto

from common.externals.codecogs import Codecogs
from common.tg.filters import MetaInfo


class Latex:
    replies = cachetools.LRUCache(maxsize=128)
    error_url = Codecogs.url(r'\mathfrak{Invalid\;Equation}')

    @classmethod
    def cache_key(cls, message: Message) -> tuple:
        return message.chat.id, message.message_id

    @classmethod
    async def process(cls, message: Message, meta: MetaInfo):
        target, text = meta.extract_text()
        if not text:
            return True

        try:
            result = await target.reply_photo(Codecogs.url(text))
        except aiogram.exceptions.BadRequest:
            result = await message.reply_photo(cls.error_url)

        cls.replies[cls.cache_key(target)] = result.message_id
        return result

    @classmethod
    async def process_edited(cls, message: Message, meta: MetaInfo):
        target, text = meta.extract_text()
        if not text:
            return True

        if message_id := cls.replies.get(cls.cache_key(target)):
            with suppress(aiogram.exceptions.BadRequest):
                return await message.bot.edit_message_media(InputMediaPhoto(Codecogs.url(text)), message.chat.id, message_id)
            return await message.bot.edit_message_media(InputMediaPhoto(cls.error_url), message.chat.id, message_id)

        return await cls.process(message, meta)
