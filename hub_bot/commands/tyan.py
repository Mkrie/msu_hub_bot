import random

import aiogram
import aiohttp
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.callback_data import CallbackData
from aiogram.utils.markdown import hbold

from common import json
from common.tg.callbacks import CallbackCommandBase
from common.tg.filters import MetaInfo
from common.tg.middlewares.settings import Settings


class Tyan(CallbackCommandBase):
    callback_data = CallbackData('tyan', 'type', 'category')

    @classmethod
    def keyboard(cls, with_nsfw: bool) -> InlineKeyboardMarkup:
        categories_swf = (
            'waifu', 'neko', 'shinobu', 'megumin', 'bully', 'cuddle', 'cry', 'hug', 'awoo', 'kiss',
            'lick', 'pat', 'smug', 'bonk', 'yeet', 'blush', 'smile', 'wave', 'highfive', 'handhold',
            'nom', 'bite', 'glomp', 'slap', 'kill', 'kick', 'happy', 'wink', 'poke', 'dance', 'cringe', 'neuro',
        )
        categories_nswf = ('waifu', 'neko', 'trap', 'blowjob')

        keyboard = InlineKeyboardMarkup(row_width=4)
        keyboard.add(*[InlineKeyboardButton(text=c.title(), callback_data=cls.callback_data.new('sfw', c)) for c in categories_swf])
        if with_nsfw:
            keyboard.add(InlineKeyboardButton(text='⬇️ NSFW', callback_data=cls.callback_data.new('nsfw', 'nsfw')))
            keyboard.add(*[InlineKeyboardButton(text=c.title(), callback_data=cls.callback_data.new('nsfw', c)) for c in categories_nswf])

        return keyboard

    @classmethod
    async def process(cls, _message: Message, meta: MetaInfo, settings: Settings):
        target = meta.reply()
        return await target.reply(hbold('База аниме тяночек 👩🏻‍🦰👱🏻‍♀️👩🏻'), reply_markup=cls.keyboard(settings.with_nsfw))

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: dict, settings: Settings):
        message = query.message
        type_, category = callback_data['type'], callback_data['category']

        if type_ == category:
            if type_ == 'sfw':
                return await query.answer(text='📝 Safe for work — без эротики', cache_time=cls.cache_time_long, show_alert=True)
            return await query.answer(text='🔞 Not safe for work — может содержать эротику', cache_time=cls.cache_time_long, show_alert=True)

        if not settings.with_nsfw and type_ == 'nsfw':
            return await query.answer(text='🚫', cache_time=cls.cache_time_10s)

        await query.answer(text='✅', cache_time=1)

        if category == 'neuro':
            url = cls.request_neuro_tyan()
            return await message.reply_photo(url, caption=f'{hbold("Нейротянка")} для {query.from_user.get_mention()}')

        for _ in range(3):
            try:
                url = await cls.request_tyan(type_, category)
                caption = hbold(category.title()) + (f' для {query.from_user.get_mention()}' if message.chat.type != 'private' else '')

                if url.endswith(('gif', 'mp4')):
                    return await message.reply_video(url, caption=caption)
                return await message.reply_photo(url, caption=caption)
            except (aiogram.exceptions.InvalidHTTPUrlContent, aiogram.exceptions.WrongFileIdentifier):
                pass

    @classmethod
    async def request_tyan(cls, type_: str, category: str) -> str:
        async with aiohttp.ClientSession() as session:
            async with session.get(f'https://api.waifu.pics/{type_}/{category}') as response:
                result = await response.json(loads=json.loads)
                return result['url']

    @classmethod
    def request_neuro_tyan(cls) -> str:
        number = ''.join(random.choices('0123456789', k=5))
        return f'https://thisanimedoesnotexist.ai/results/psi-1.0/seed{number}.png'
