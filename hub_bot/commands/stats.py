from msu_hub_bot.settings import settings

import asyncio
import datetime
from contextlib import suppress
from textwrap import dedent

import aiogram
import httpx
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from aiogram.utils.callback_data import CallbackData
from aiogram.utils.markdown import hbold

from app import db
from common.db.edb import UserDB, ChatDB, UpdateDB
from common.tg.callbacks import CallbackCommandBase


class Stats(CallbackCommandBase):
    callback_data = CallbackData('stats', 'action')

    @classmethod
    def keyboard(cls) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardMarkup().row(
            InlineKeyboardButton(text='🔄 Обновить', callback_data=cls.callback_data.new('update')),
        )
        return keyboard

    @classmethod
    async def text(cls):
        yesterday = datetime.datetime.utcnow() - datetime.timedelta(days=1)

        coros = [
            UserDB.query(db).count(),
            ChatDB.query(db).count(),
            UpdateDB.query(db).count(f'.created > to_datetime({int(yesterday.timestamp())}) and .handled = true'),
            UpdateDB.query(db).count(f'.created > to_datetime({int(yesterday.timestamp())})'),
        ]

        users, chats, updates_handled, updates = await asyncio.gather(*coros)

        text = dedent(f"""
            {hbold("Статистика @msu_hub_bot")}
            
            • Знаю {hbold(chats)} чатов
            • Видел {hbold(users)} пользователей
            • За последний день обработал {hbold(updates_handled)} команд
            • И увидел {hbold(updates)} сообщений
        """).strip()
        return text

    @classmethod
    async def process(cls, message: Message):
        return await message.reply(await cls.text(), reply_markup=cls.keyboard())

    @classmethod
    async def process_cb(cls, query: CallbackQuery):
        await query.answer('✅', cache_time=1)

        lock = cls.lock(query.message)

        if lock.locked():
            return True

        async with lock:
            await query.message.edit_text(await cls.text(), reply_markup=cls.keyboard())
            await asyncio.sleep(1.)

        return True


class StatsVPN(CallbackCommandBase):
    # Docs: https://redocly.github.io/redoc/?url=https://raw.githubusercontent.com/Jigsaw-Code/outline-server/master/src/shadowbox/server/api.yml

    callback_data = CallbackData('vpn', 'action')
    id_to_name = settings.outline_names

    @classmethod
    def keyboard(cls) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardMarkup().row(
            InlineKeyboardButton(text='🔄 Обновить', callback_data=cls.callback_data.new('update')),
        )
        return keyboard

    @classmethod
    async def text(cls):
        url = settings.require('outline_url')
        result = await httpx.AsyncClient(verify=False).get(url)
        transferred = result.json()['bytesTransferredByUserId']
        text = f'{hbold("Статистика Outline VPN")} (за 30 дней):\n\n• ' + '\n• '.join(
            f'{name} — {hbold(f"{transferred[id_] / 1024 / 1024 / 1024:.2f}")} GB' for id_, name in
            sorted(cls.id_to_name.items(), key=lambda x: transferred[x[0]], reverse=True))
        return text

    @classmethod
    async def process(cls, message: Message):
        return await message.reply(await cls.text(), reply_markup=cls.keyboard())

    @classmethod
    async def process_cb(cls, query: CallbackQuery):
        await query.answer('✅', cache_time=1)

        lock = cls.lock(query.message)

        if lock.locked():
            return True

        text = await cls.text()

        async with lock:
            if query.message.html_text != text:
                with suppress(aiogram.exceptions.BadRequest):
                    await query.message.edit_text(text, reply_markup=cls.keyboard())
                    await asyncio.sleep(1.)

        return True
