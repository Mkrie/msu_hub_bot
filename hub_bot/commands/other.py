import re
from contextlib import suppress

import aiogram
import transliterate
from aiogram.types import Message, User, Chat
from aiogram.utils.markdown import hcode, quote_html, hbold, hpre, hlink

from common.tg.filters import MetaInfo
from common.tg.utils import username_link, chat_url


async def process_me(message: Message, meta: MetaInfo):
    _, text = meta.extract_text()
    if not text:
        return True

    with suppress(aiogram.exceptions.MessageError):
        await message.delete()

    return await message.answer(f'{username_link(message.from_user)} {quote_html(text)}')


async def process_copy(message: Message):
    target = message.reply_to_message or message
    return await target.copy_to(message.chat.id, reply_to_message_id=message.message_id)


async def process_transliterate(_message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if not text:
        return True

    lang = transliterate.detect_language(text, heavy_check=True) or 'ru'
    text = transliterate.translit(text, lang)

    return await target.reply(quote_html(text))


async def process_punto(_message: Message, meta: MetaInfo):
    ru_tab = 'ЁёАБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдежзийклмнопрстуфхцчшщъыьэюя'
    en_tab = '~`F<DULT:PBQRKVYJGHCNEA{WXIO}SM">Zf,dult;pbqrkvyjghcnea[wxio]sm\'.z'
    ru_en = str.maketrans(ru_tab, en_tab)
    en_ru = str.maketrans(en_tab, ru_tab)

    target, text = meta.extract_text()
    if not text:
        return True

    lang = transliterate.detect_language(text, heavy_check=True)
    if lang not in ('en', 'ru'):
        lang = 'en'

    if lang == 'ru':
        return await target.reply(quote_html(text.translate(ru_en)))

    text = text.translate(en_ru)
    return await target.reply(quote_html(text))


async def process_id(message: Message):
    text = ''

    for target in (message.reply_to_message, message):
        if target:
            for t in (target.forward_from, target.forward_from_chat, target.forward_signature, target.forward_sender_name,
                      target.sender_chat, target.from_user, target.chat, target.via_bot):
                if isinstance(t, str):
                    name = t
                    id_ = '🤷🏻‍♂️'
                    url = None
                elif isinstance(t, User):
                    name = t.full_name
                    id_ = t.id
                    url = t.url
                elif isinstance(t, Chat):
                    name = t.full_name
                    id_ = t.id
                    url = await chat_url(t)
                else:
                    continue

                link = f", ({hlink('🔗', url)})" if url else ''
                text += f'{hbold(name)}{link}:\n└ {hcode(id_)}\n\n'

    return await message.reply(text, disable_notification=True, disable_web_page_preview=True)


async def process_md(_message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if not text:
        return True
    return await target.reply(hpre(target.md_text))


async def process_html(_message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if not text:
        return True
    return await target.reply(hpre(target.html_text))


async def process_file_id(message: Message, meta: MetaInfo):
    t, *file_ids = re.split(r'\s', meta.text)

    if t == 'photo':
        for file_id in file_ids:
            await message.reply_photo(file_id)

    elif t == 'audio':
        for file_id in file_ids:
            await message.reply_audio(file_id)

    elif t == 'document':
        for file_id in file_ids:
            await message.reply_document(file_id)

    elif t == 'video':
        for file_id in file_ids:
            await message.reply_video(file_id)

    elif t == 'animation':
        for file_id in file_ids:
            await message.reply_animation(file_id)

    elif t == 'sticker':
        for file_id in file_ids:
            await message.reply_sticker(file_id)

    elif t == 'video_note':
        for file_id in file_ids:
            await message.reply_video_note(file_id)

    elif t == 'voice':
        for file_id in file_ids:
            await message.reply_voice(file_id)

    else:
        await message.reply_photo(t)
        for file_id in file_ids:
            await message.reply_photo(file_id)
