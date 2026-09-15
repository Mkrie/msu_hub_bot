import asyncio
from contextlib import suppress

import aiogram
from aiogram.types import ChatType, InlineQuery, InlineQueryResultArticle, InputTextMessageContent, Message
from aiogram.utils.exceptions import BadRequest
from aiogram.utils.markdown import hbold, hlink

from app import events_chat_id, em, db
from db import EcosystemChat
from events import EcosystemManager


async def process_create_infra_chat(message: Message):
    if ChatType.is_private(message):
        return True

    if await EcosystemChat.query(db).exist(message.chat.id):
        return True

    admins = await message.chat.get_administrators()

    for admin in admins:
        if admin.user.id == message.bot.id:
            if admin.can_delete_messages:
                await message.delete()

            if admin.can_promote_members:
                break
    else:
        return

    await EcosystemChat.query(db).insert(
        chat_id=message.chat.id,
        name=message.chat.full_name,
        section=EcosystemManager.ChatGroup.other.name,
        members=await message.chat.get_member_count(),
    )

    event = f'❇️ ' \
            f'{message.from_user.get_mention()} добавил в экосистему новый чат: ' \
            f'{hlink(message.chat.full_name, await message.chat.get_url())}.'

    return await message.bot.send_message(events_chat_id, event)


async def process_delete_infra_chat(message: Message):
    if ChatType.is_private(message):
        return True

    if not await EcosystemChat.query(db).exist(message.chat.id):
        return True

    admins = await message.chat.get_administrators()

    for admin in admins:
        if admin.user.id == message.bot.id:
            if admin.can_delete_messages:
                await message.delete()

    await EcosystemChat.query(db).delete(pk=message.chat.id)

    event = f'❎️ ' \
            f'{message.from_user.get_mention()} удалил чат из экосистемы: ' \
            f'{hlink(message.chat.full_name, await message.chat.get_url())}.'

    return await message.bot.send_message(events_chat_id, event)


async def process_update_pins(_message: Message):
    return await em.update_pins()


async def process_pin(message: Message):
    admins = await message.chat.get_administrators()

    for admin in admins:
        if admin.user.id == message.bot.id:
            if admin.can_delete_messages:
                await message.delete()

            if admin.can_pin_messages:
                return await em.pin(message.chat.id, forced=True)


async def process_pin_all(_message: Message):
    e_chats = await EcosystemChat.query(db).get_all_cached()
    coros = [em.pin(ec.chat_id) for ec in e_chats.values()]
    return await asyncio.gather(*coros)


async def process_links(message: Message):
    return await message.answer(await em.text())


async def process_status(message: Message):
    e_chats = await EcosystemChat.query(db).get_all_cached()
    self_id = (await message.bot.get_me()).id

    async def check(chat_id):
        try:
            me = await message.bot.get_chat_member(chat_id, self_id)
            if not me.is_chat_admin():
                return chat_id, '❌', '❌'
            if me.can_promote_members:
                return chat_id, '✅', '✅'
            return chat_id, '✅', '❌'
        except BadRequest:
            return chat_id, '💔', '💔'

    coros = [check(chat_id) for chat_id in e_chats]
    text = hbold('Status') + '\n\n'
    for coro in asyncio.as_completed(coros):
        chat_id, r_1, r_2 = await coro
        text += f'— {e_chats[chat_id].name}: {r_1} {r_2}\n'

    return await message.reply(text)


async def process_inline(inline_query: InlineQuery):
    text = await em.text()

    msg = InputTextMessageContent(text)
    item = InlineQueryResultArticle(id='0', title='Все чаты МГУ', input_message_content=msg)

    with suppress(aiogram.exceptions.InvalidQueryID):
        return await inline_query.answer(results=[item], is_personal=False, cache_time=10 * 60)

    return True
