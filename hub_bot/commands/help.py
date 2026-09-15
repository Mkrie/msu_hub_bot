import asyncio
from contextlib import suppress

import aiogram
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, ChatType
from aiogram.utils.callback_data import CallbackData

from common.tg.callbacks import CallbackCommandBase
from common.tg.filters import MetaInfo
from texts import cmd_help


class HelpMessage(CallbackCommandBase):
    callback_data = CallbackData('help', 'action')
    compressed_text = '📝 Команды и возможности бота'

    @classmethod
    def keyboard(cls) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardMarkup().add(
            InlineKeyboardButton(text='⏬ Развернуть помощь', callback_data=cls.callback_data.new('open'))
        )
        return keyboard

    @classmethod
    async def process(cls, message: Message, meta: MetaInfo):
        target = meta.reply()
        result = await target.reply(cmd_help, disable_web_page_preview=True)
        if message.chat.type != ChatType.PRIVATE:
            asyncio.create_task(cls.edit(result))
        return result

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: dict):
        action = callback_data['action']

        await query.answer(text='✅', cache_time=1 * 60)

        if action == 'open':
            with suppress(aiogram.exceptions.BadRequest):
                result = await query.message.edit_text(cmd_help, disable_web_page_preview=True)
                return asyncio.create_task(cls.edit(result))

        return True

    @classmethod
    async def edit(cls, message: Message):
        await asyncio.sleep(60.)
        with suppress(aiogram.exceptions.BadRequest):
            return await message.edit_text(cls.compressed_text, reply_markup=cls.keyboard(), disable_web_page_preview=True)
