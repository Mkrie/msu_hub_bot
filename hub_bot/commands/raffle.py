import asyncio
import random

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.callback_data import CallbackData
from aiogram.utils.markdown import hbold, hitalic

from common.tg.callbacks import CallbackCommandBase
from common.tg.utils import profile_photo, username_mention
from common.utils import outdated


def sample(population: list, k: int):
    if len(population) < k:
        return random.choices(population, k=k)
    return random.sample(population, k=k)


class Raffle(CallbackCommandBase):
    callback_data = CallbackData('raffle', 'action')

    @classmethod
    def keyboard(cls) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardMarkup().row(
            InlineKeyboardButton(text='Участвую! 🎈', callback_data=cls.callback_data.new('reg')),
        ).row(
            InlineKeyboardButton(text='Выбрать победителя 👑', callback_data=cls.callback_data.new('winner')),
        )
        return keyboard

    @classmethod
    async def process(cls, message: Message):
        target = message.reply_to_message if message.reply_to_message else message
        text = '✨ ' + hbold('Конкурс') + f' от {username_mention(message.from_user)}\n\nУчастники:'
        return await target.reply(text, reply_markup=cls.keyboard())

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: dict):
        m = query.message

        if outdated(m.date + cls.cache_time_long_td):
            await query.answer(text='♻️ Этому розыгрышу больше недели, он закрывается', show_alert=True)
            return await m.delete_reply_markup()

        action = callback_data['action']

        if action == 'reg':
            await query.answer(text='🎈 Вы участвуете в розыгрыше!', cache_time=cls.cache_time_long)
            text_part = f'\n— {query.from_user.get_mention()}'
            text = await cls.cached_text(m, text_part)
            async with cls.lock(m):
                return await m.edit_text(text, reply_markup=m.reply_markup)

        creator, *users = (e.user for e in m.entities if e.user)
        if query.from_user != creator:
            return await query.answer('🤷🏻‍♂️ Только создатель розыгрыша может завершить его', cache_time=cls.cache_time_long)
        if len(users) == 0:
            return await query.answer('🤷🏻‍♂️ Нельзя завершить розыгрыш без участников', cache_time=3)

        await query.answer('👑 Запущено определение победителя!', cache_time=cls.cache_time_long)
        await m.delete_reply_markup()

        async with cls.lock(m):
            message = await m.reply(hitalic('👑 Запущено определение победителя!'))

            u1, u2 = sample(users, 2)
            winner = random.choice(users)
            photo = await profile_photo(winner)
            text = hbold('👑 Победитель:') + f' {winner.get_mention()}'

            for t in (hitalic('👑 Может это будет') + f' {u1.get_mention()}?',
                      hitalic('👑 Или') + f' {u2.get_mention()}?',
                      hitalic('👑 Сейчас и узнаем! Итак, победитель...')):
                await asyncio.sleep(3.)
                await message.edit_text(t)

            await asyncio.sleep(3.)
            await message.delete()
            if photo:
                message = await m.reply_photo(photo.file_id, caption=text)
            else:
                message = await m.reply(text)

            await m.edit_text(m.html_text + f'\n\n{text}')

        return message
