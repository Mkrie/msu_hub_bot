from msu_hub_bot.settings import settings

from contextlib import suppress
from datetime import timedelta

import aiogram
import aiohttp
import pendulum
from aiocache import cached
from aiogram.types import CallbackQuery
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.callback_data import CallbackData
from aiogram.utils.markdown import hbold, hlink, hitalic

from common.tg.callbacks import CallbackCommandBase
from common.utils import outdated


class StatHat(CallbackCommandBase):
    access_token = settings.stathat_access_token

    callback_data = CallbackData('stathat', 'provider')

    @classmethod
    def keyboard(cls) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardMarkup().row(
            InlineKeyboardButton(text='ГЗ и ДСЛ', callback_data=cls.callback_data.new('umos')),
            InlineKeyboardButton(text='ДАС', callback_data=cls.callback_data.new('imt')),
        )
        return keyboard

    @classmethod
    @cached(ttl=30, noself=True)
    async def text(cls, provider: str) -> str:
        settings.require("stathat_access_token")
        if provider == 'imt':
            stats_id = '84TT/Gcky/PSuD'
            endpoint = 'das'
            title = 'Мониторинг интернета в общежитии ДАС'
        else:
            stats_id = '75DP/HJhm/H1KY'
            endpoint = ''
            title = 'Мониторинг интернета в общежитиях ГЗ и ДСЛ'

        async with aiohttp.ClientSession() as session:
            async with session.get(f'https://www.stathat.com/x/{cls.access_token}/data/{stats_id}?t=20m10m') as request:
                result = await request.json()

        pings, downtimes, submissions = result[0]['points'], result[1]['points'], result[2]['points']

        title_link = hlink(title, f'https://internet.msut.me/{endpoint}')

        text = f'📊 {title_link}\n\n'
        for ping, downtime, submission in zip(pings, downtimes, submissions):
            t = pendulum.from_timestamp(ping['time'], 'UTC').in_timezone('Europe/Moscow').format('DD MMMM, HH:mm:ss', locale='ru')
            text += f'{hbold(t)}:\n'
            text += f'— {hitalic("пинг")}: ' + hbold(f'{ping["value"]:.1f}') + ' мс\n'
            text += f'— {hitalic("проблем с сетью")}: ' + hbold(f'{downtime["value"]}') + '\n'
            text += f'— {hitalic("участников")}: ' + hbold(f'{submission["value"] / 10:.1f}') + ' чел.\n'
            text += f'\n'

        text += '📝 ' + hlink('Как присоединиться', 'https://internet.msut.me/join') + ':\n'
        text += '— скачать ' + hlink('DMonitor', 'https://github.com/uburuntu/dmonitor/releases') + ' и запустить его'
        return text

    @classmethod
    async def process(cls, message: Message):
        provider = 'imt' if message.chat.id == settings.stathat_chat_id else 'umos'
        text = await cls.text(provider)
        return await message.reply(text, reply_markup=cls.keyboard(), disable_web_page_preview=True)

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: dict):
        td = timedelta(days=7)
        if outdated(query.message.date + td):
            await query.answer(text='♻️ Слишком старое сообщение', show_alert=True)
            return await query.message.delete_reply_markup()

        provider = callback_data['provider']

        text = await cls.text(provider)
        await query.answer('✅ Обновлено', cache_time=5)

        if query.message.html_text != text:
            with suppress(aiogram.exceptions.BadRequest):
                return await query.message.edit_text(text, reply_markup=cls.keyboard(), disable_web_page_preview=True)

        return True
