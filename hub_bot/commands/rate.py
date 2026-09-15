import asyncio
from typing import Tuple

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.callback_data import CallbackData
from aiogram.utils.markdown import hbold

from common.tg.callbacks import CallbackCommandBase
from common.tg.filters import MetaInfo


class Rate(CallbackCommandBase):
    callback_data = CallbackData('rate', 'is_up')

    @classmethod
    def keyboard(cls, up: int = 0, down: int = 0) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardMarkup().row(
            InlineKeyboardButton(text=f'{up} 👍🏻', callback_data=cls.callback_data.new('+')),
            InlineKeyboardButton(text=f'{down} 👎🏻', callback_data=cls.callback_data.new('-')),
        )
        return keyboard

    @classmethod
    async def process(cls, _message: Message, meta: MetaInfo):
        target = meta.reply()
        msg = await target.reply(hbold('🤔'), reply_markup=cls.keyboard())
        return msg

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: dict):
        def extract_numbers(m: Message) -> Tuple[int, int]:
            k = m.reply_markup.inline_keyboard[0]
            return int(k[0].text.split()[0]), int(k[1].text.split()[0])

        message = query.message
        is_up = callback_data['is_up'] == '+'

        key = cls.cache_key(message)
        if key in cls.cache:
            if is_up:
                cls.cache[key][0] += 1
            else:
                cls.cache[key][1] += 1
            up, down = cls.cache[key]
        else:
            up, down = extract_numbers(message)
            if is_up:
                up += 1
            else:
                down += 1
            cls.cache[key] = [up, down]

        await query.answer(text=f'{up} 👍🏻' if is_up else f'{down} 👎🏻', cache_time=cls.cache_time_long)

        lock = cls.lock(key)

        if lock.locked():
            return True

        async with lock:
            await asyncio.sleep(1.)
            return await message.edit_reply_markup(cls.keyboard(*cls.cache.get(key, [up, down])))
