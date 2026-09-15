from collections import defaultdict

from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.callback_data import CallbackData
from aiogram.utils.markdown import hitalic

from common.utils import is_finished

rate_query = CallbackData('rate', 'is_up')


def rate_keyboard(up: int = 0, down: int = 0) -> InlineKeyboardMarkup:
    button_1 = InlineKeyboardButton(text=f'{up} 👍🏻', callback_data=rate_query.new('+'))
    button_2 = InlineKeyboardButton(text=f'{down} 👎🏻', callback_data=rate_query.new('-'))
    keyboard = InlineKeyboardMarkup().add(button_1, button_2)
    return keyboard


class HaikuMiddleware(BaseMiddleware):
    def __init__(self):
        super().__init__()

    @staticmethod
    def count_syllables(text: str) -> int:
        d = defaultdict(int)
        for c in text.lower():
            d[c] += 1
        return sum(d.get(c, 0) for c in ('а', 'о', 'у', 'э', 'и', 'ы', 'е', 'ё', 'ю', 'я'))

    async def on_post_process_message(self, message: Message, results, data: dict):
        if message.is_forward():
            return

        text = message.text or message.caption
        if not text:
            return

        words = text.strip().split()
        words_it = iter(words)
        result = []

        for dest in (5, 7, 5):
            curr = []
            count = 0

            for word in words_it:
                curr.append(word)
                count += self.count_syllables(word)

                if dest <= count <= dest:
                    result.append(curr)
                    break

                if count > dest:
                    return

        if not is_finished(words_it) or len(result) != 3:
            return

        haiku = hitalic('\n'.join(' '.join(c) for c in result))
        # if not (15 <= self.count_syllables(haiku) <= 18):
        #     return

        haiku += '\n\n🌸 ' + message.from_user.get_mention()
        return await message.answer(haiku, reply_markup=rate_keyboard(), disable_web_page_preview=True)
