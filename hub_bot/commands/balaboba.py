from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ContentType, User, MessageEntityType
from aiogram.utils.callback_data import CallbackData
from aiogram.utils.markdown import hbold, hitalic, quote_html

from common.externals.exceptions import ExternalServiceError
from common.externals.other import balaboba
from common.tg.callbacks import CallbackCommandBase
from common.tg.chat_actioner import ChatActioner
from common.tg.filters import MetaInfo
from common.tg.utils import action_by_type


class Balaboba(CallbackCommandBase):
    callback_data = CallbackData('boba', 'style_id')

    style_to_id = {
        '✏️ Без стиля': 0,
        '🌀 Теории заговора': 1,
        '📺 ТВ-репортажи': 2,
        '🍻 Тосты': 3,
        '🧍🏻‍♂️ Пацанские цитаты': 4,
        '🎟 Рекламные слоганы': 5,
        '📝 Короткие истории': 6,
        '🏞 Подписи в Instagram': 7,
        '📰 Короче, Википедия': 8,
        '🎥 Синопсисы фильмов': 9,
        '♋️ Гороскоп': 10,
        '👴🏻 Народные мудрости': 11,
    }

    id_to_style = {v: k for k, v in style_to_id.items()}

    @classmethod
    def keyboard(cls) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardMarkup(row_width=6)
        for k, v in cls.style_to_id.items():
            keyboard.insert(InlineKeyboardButton(text=k.split()[0], callback_data=cls.callback_data.new(f'{v}')))
        return keyboard

    @classmethod
    async def request(cls, text: str, style_id: int = 0, user: User = None) -> str:
        query, result = await balaboba(text, style_id)
        from_user = f' для {user.get_mention()}' if user else ''
        return hitalic(cls.id_to_style[style_id]) + from_user + '\n\n' + hbold(text) + quote_html(query[len(text):]) + quote_html(result)

    @classmethod
    async def process(cls, message: Message, meta: MetaInfo):
        target, text = meta.extract_text()

        if not text:
            return True

        try:
            async with ChatActioner(message.chat, action_by_type(ContentType.TEXT)):
                answer = await cls.request(text)
        except ExternalServiceError as e:
            return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

        return await target.reply(answer, reply_markup=cls.keyboard() if text else None)

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: dict):
        message = query.message
        style_id = callback_data['style_id']
        if style_id.isdigit():
            style_id = int(style_id)
        if style_id not in cls.id_to_style:
            style_id = 0

        await query.answer(text='✅', cache_time=cls.cache_time_10s)
        text = [e.get_text(message.text) for e in message.entities if e.type == MessageEntityType.BOLD][0]

        try:
            async with ChatActioner(message.chat, action_by_type(ContentType.TEXT)):
                answer = await cls.request(text, style_id, user=query.from_user)
        except ExternalServiceError as e:
            return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

        return await message.reply(answer)
