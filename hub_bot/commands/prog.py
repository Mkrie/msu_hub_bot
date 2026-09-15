from contextlib import suppress
from functools import partial
from typing import Optional, Tuple

import aiogram
import cachetools
from aiogram import Dispatcher
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import StatesGroup, State
from aiogram.types import Message, ChatActions, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, MessageEntityType, ContentType
from aiogram.utils.callback_data import CallbackData
from aiogram.utils.markdown import hpre, hbold, hcode, hitalic

from app import jdoodle
from common.tg.callbacks import CallbackCommandBase
from common.tg.filters import MetaCommand, MetaInfo
from common.tg.utils import download_text
from utils.jdoodle import LANGUAGES, JDoodleError


def register_code_submitters(dp: Dispatcher):
    method = partial(dp.register_message_handler, content_types=ContentType.ANY)
    edited = partial(dp.register_edited_message_handler, content_types=ContentType.ANY)

    for lang in LANGUAGES:
        method(ProgCompiler.process_builder(lang), MetaCommand(lang))
        edited(ProgCompiler.process_builder(lang), MetaCommand(lang))

    method(ProgCompiler.process_builder('python2'), MetaCommand('py2'))
    edited(ProgCompiler.process_builder('python2'), MetaCommand('py2'))
    method(ProgCompiler.process_builder('python3'), MetaCommand('py', 'python'))
    edited(ProgCompiler.process_builder('python3'), MetaCommand('py', 'python'))
    method(ProgCompiler.process_builder('nodejs'), MetaCommand('js', 'javascript'))
    edited(ProgCompiler.process_builder('nodejs'), MetaCommand('js', 'javascript'))
    return dp


def register_code_submitters_with_stdin(dp: Dispatcher):
    method = partial(dp.register_message_handler, content_types=ContentType.ANY)
    edited = partial(dp.register_edited_message_handler, content_types=ContentType.ANY)

    def with_stdin(s: str) -> Tuple[str, str]:
        return s + '_stdin', s + 's'

    for lang in LANGUAGES:
        method(ProgCompiler.process_stdin_builder(lang), MetaCommand(*with_stdin(lang)))
        edited(ProgCompiler.process_stdin_builder(lang), MetaCommand(*with_stdin(lang)))

    method(ProgCompiler.process_stdin_builder('python2'), MetaCommand(*with_stdin('py2')))
    edited(ProgCompiler.process_stdin_builder('python2'), MetaCommand(*with_stdin('py2')))
    method(ProgCompiler.process_stdin_builder('python3'), MetaCommand(*with_stdin('py'), *with_stdin('python')))
    edited(ProgCompiler.process_stdin_builder('python3'), MetaCommand(*with_stdin('py'), *with_stdin('python')))
    method(ProgCompiler.process_stdin_builder('nodejs'), MetaCommand(*with_stdin('js'), *with_stdin('javascript')))
    edited(ProgCompiler.process_stdin_builder('nodejs'), MetaCommand(*with_stdin('js'), *with_stdin('javascript')))
    return dp


async def process_code(message: Message):
    text = f'{hbold("Доступные языки")}:\n\n'
    for lang, (name, versions) in LANGUAGES.items():
        text += f'— {hbold(name)} | #{lang}, {versions[-1][0]}\n'
    text += f'\n'

    text += f'{hbold("Некоторые синонимы")}:\n'
    text += f'— Python 2 | #py2\n'
    text += f'— Python 3 | #py, #python\n'
    text += f'— NodeJS | #js, #javascript\n'
    text += f'\n'

    text += f'{hbold("Примечание")}: Чтобы получить возможность задать пользовательский ввод, ' \
            f'к команде нужно сделать приписку {hcode("_stdin")} или {hcode("s")}. ' \
            f'Например, #py_stdin или #pys.\n\n'

    text += f'{hbold("Примечание")}: Вы можете редактировать сообщение с кодом, ' \
            f'бот автоматически обновит результат на запуске с исправленным кодом.\n\n'

    text += f'{hbold("Как пользоваться")}: вместе с кодом программы нужно прислать хештег, ' \
            f'чтобы бот понял, что сообщение нужно исполнить на указанном языке. ' \
            f'Хештег будет вырезан из текста и не повлияет на расчёт.\n\n'
    return await message.reply(text)


async def code_submit(source_code: str, stdin: str = '', lang: str = 'python3') -> Optional[str]:
    with suppress(JDoodleError):
        result = await jdoodle.instance.request_and_parse(source_code, stdin, lang)
        return result
    return None


class ProgStates(StatesGroup):
    stdin = State()


class ProgCompiler(CallbackCommandBase):
    callback_data = CallbackData('prog', 'action')
    replies = cachetools.LRUCache(maxsize=128)

    @classmethod
    def keyboard(cls) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardMarkup().add(
            InlineKeyboardButton(text='⌨️ Задать ввод', callback_data=cls.callback_data.new('input')),
            InlineKeyboardButton(text='✖️ Отмена ввода', callback_data=cls.callback_data.new('cancel')),
        )
        return keyboard

    @classmethod
    def process_builder(cls, lang: str):
        async def process(message: Message, meta: MetaInfo):
            target, text = await meta.extract_text_with_doc_plain()
            if not text:
                return True

            await message.chat.do(ChatActions.TYPING)
            header = hbold(lang) + ' | ' + hbold(LANGUAGES[lang][1][-1][0]) + '\n\n'

            advance = None
            if message_id := cls.replies.get(cls.cache_key(target)):
                with suppress(aiogram.exceptions.BadRequest):
                    advance = await message.bot.edit_message_text(header + hitalic('🔄 Ожидание...'), message.chat.id, message_id)

            if advance is None:
                advance = await target.reply(header + hitalic('🔄 Ожидание...'))
                cls.replies[cls.cache_key(target)] = advance.message_id

            result = await code_submit(text, lang=lang)
            if result:
                return await advance.edit_text(header + hpre(result))

            return await advance.edit_text(hcode('🤷🏻‍♂️ Произошла какая-то ошибка'))

        return process

    @classmethod
    def process_stdin_builder(cls, lang: str):
        async def process_code_submit_with_stdin(message: Message, meta: MetaInfo):
            return await ProgCompiler.process_stdin(message, meta, lang)

        return process_code_submit_with_stdin

    @classmethod
    async def process_stdin(cls, message: Message, meta: MetaInfo, lang: str):
        target, text, doc = meta.extract_text_with_doc()
        if text:
            text = hpre(text)
        else:
            if not doc:
                return True
            text = hcode(doc.file_name)

        text = f'{hbold(lang)} | {hbold(LANGUAGES[lang][1][-1][0])} | with stdin\n\n{text}'

        result = None
        if message_id := cls.replies.get(cls.cache_key(target)):
            with suppress(aiogram.exceptions.BadRequest):
                result = await message.bot.edit_message_text(text, message.chat.id, message_id, reply_markup=cls.keyboard())

        if result is None:
            result = await target.reply(text, reply_markup=cls.keyboard())
            cls.replies[cls.cache_key(target)] = result.message_id

        return result

    @classmethod
    async def process_stdin_cb(cls, query: CallbackQuery, state: FSMContext, callback_data: dict):
        action = callback_data['action']

        if action == 'cancel':
            if await state.get_state() != ProgStates.stdin.state:
                return await query.answer('💁🏻‍♂️ Вы не в процессе ввода', cache_time=1)

            async with state.proxy() as data:
                with suppress(aiogram.exceptions.BadRequest):
                    await query.bot.delete_message(data.get('chat_id'), data.get('inform_message_id'))
            await state.finish()
            return await query.answer('🆗 Ввод отменён', cache_time=3)

        if await state.get_state() == ProgStates.stdin.state:
            return await query.answer('🔄 Бот уже ждёт твой ввод', cache_time=3, show_alert=True)

        m = query.message
        await query.answer('⬇️ Теперь ожидаю ввод', cache_time=3)

        code = [e.get_text(m.text) for e in m.entities if e.type == MessageEntityType.PRE]
        if code:
            code = code[0]
        else:
            if m.reply_to_message:
                code = await download_text(m.reply_to_message.document.file_id)
            else:
                return await m.edit_text(m.html_text + '\n\n⚠️ Сообщение с исходным кодом удалено')
        lang = [e.get_text(m.text) for e in m.entities if e.type == MessageEntityType.BOLD][0]

        reply = await m.reply(f'{query.from_user.get_mention(query.from_user.first_name)}, ожидаю ввод ⬇️, или /cancel')
        await ProgStates.stdin.set()

        async with state.proxy() as data:
            data['chat_id'] = reply.chat.id
            data['inform_message_id'] = reply.message_id
            data['prog_lang'] = lang
            data['prog_code'] = code

        return True

    @classmethod
    async def process_stdin_run(cls, message: Message, state: FSMContext):
        await message.chat.do(ChatActions.TYPING)

        async with state.proxy() as data:
            with suppress(aiogram.exceptions.BadRequest):
                await message.bot.delete_message(data.get('chat_id'), data.get('inform_message_id'))
            lang = data['prog_lang']
            code = data['prog_code']

        await state.finish()

        header = hbold(lang) + ' | ' + hbold(LANGUAGES[lang][1][-1][0]) + '\n\n'
        advance = await message.reply(header + hitalic('🔄 Ожидание...'))

        result = await code_submit(code, stdin=message.text or message.caption or '', lang=lang)
        if result:
            return await advance.edit_text(header + hpre(result))

        return await advance.edit_text(hcode('🤷🏻‍♂️ Произошла какая-то ошибка'))
