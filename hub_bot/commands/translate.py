from aiogram.types import Message
from aiogram.utils.markdown import hcode, hitalic, quote_html
from googletrans import Translator

from common.tg.filters import MetaInfo


async def translate(message: Message, meta: MetaInfo, dest: str):
    target, text = meta.extract_text()
    if not text:
        return True

    note = ''
    if len(text) > 2048:
        text = text[:2048]
        note = '\n\n' + hitalic('Переведены только первые 2000 символов')

    try:
        t = Translator(raise_exception=True)
        translated = t.translate(text, dest).text
    except Exception:
        return await message.reply(hcode('🤷🏻‍♂️ Не удалось выполнить запрос'))

    return await target.reply(quote_html(translated) + note)


async def process_en(message: Message, meta: MetaInfo):
    return await translate(message, meta, 'en')


async def process_ru(message: Message, meta: MetaInfo):
    return await translate(message, meta, 'ru')


async def process_uz(message: Message, meta: MetaInfo):
    return await translate(message, meta, 'uz')


async def process_translate(message: Message, meta: MetaInfo):
    args = meta.arguments
    lang = args[0] if args else 'en'
    return await translate(message, meta, lang)
