import csv

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import Message
from aiogram.utils.callback_data import CallbackData
from aiogram.utils.markdown import hitalic

from common.tg.callbacks import CallbackCommandBase
from common.tg.utils import sender_mention
from common.utils import RandomizerForDay
from resources import debate


def generate_resolutions():
    class ExcelDialect:
        delimiter = ';'
        quotechar = '"'
        escapechar = None
        doublequote = True
        skipinitialspace = False
        lineterminator = '\r\n'
        quoting = csv.QUOTE_MINIMAL

    rows = csv.reader(debate.open(encoding='utf-8'), dialect=ExcelDialect)
    next(rows)

    result = {}

    for uid, round_, tournament, format_, city, date, resolution in rows:
        result[uid.strip()] = (round_.strip(), tournament.strip(), format_.strip(), city.strip(), date.strip(), resolution.strip())

    return result


resolutions = generate_resolutions()
resolution_ids = tuple(resolutions)


class Debate(CallbackCommandBase):
    callback_data = CallbackData('debate', 'action', 'uid')

    @classmethod
    def keyboard(cls, uid: str) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardMarkup().row(
            InlineKeyboardButton(text='🏁 Из турнира', callback_data=cls.callback_data.new('tournament', uid)),
        ).row(
            InlineKeyboardButton(text='📜 Правила', callback_data=cls.callback_data.new('rules', uid)),
            InlineKeyboardButton(text='🔤 Сокращения', callback_data=cls.callback_data.new('abbreviations', uid)),
        )
        return keyboard

    @classmethod
    async def process(cls, message: Message):
        target = message.reply_to_message or message

        name = sender_mention(target)
        uid = RandomizerForDay.random(target.sender_chat and target.sender_chat.id or target.from_user.id).choice(resolution_ids)
        resolution = resolutions[uid][-1]

        return await target.reply(f'🗣 {name}, твоя резолюция на сегодня:\n\n{hitalic(resolution)}',
                                  reply_markup=cls.keyboard(uid), disable_web_page_preview=True)

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: dict):
        action, uid = callback_data['action'], callback_data['uid']

        if action == 'rules':
            return await query.answer('📜 Нужно отстоять резолюцию, а остальным её опровергнуть', cache_time=cls.cache_time_long, show_alert=True)

        if action == 'abbreviations':
            return await query.answer('ЭП — Эта палата\n\nЭПСЧ — Эта палата считает, что', cache_time=cls.cache_time_long, show_alert=True)

        round_, tournament, _, city, date, _ = resolutions[uid]
        return await query.answer(f'🏁 Турнир: {tournament}, {city}, {date}, раунд: {round_}', cache_time=cls.cache_time_long, show_alert=True)
