import asyncio
from contextlib import suppress
from typing import List

import aiogram
from aiogram.types import ChatMember, ChatPermissions, Message
from aiogram.utils.markdown import hcode, hlink, hpre

from app import bot, events_chat_id, db
from db import EcosystemChat


async def process_ban(message: Message):
    arg = message.get_args()
    if not arg.isdigit():
        return

    user_id = int(arg)
    e_chats = await EcosystemChat.query(db).get_all()

    coros = [bot.kick_chat_member(chat.chat_id, user_id) for chat in e_chats]
    return await asyncio.gather(*coros)


async def process_restrict(message: Message):
    arg = message.get_args()
    if not arg.isdigit():
        return

    user_id = int(arg)
    e_chats = await EcosystemChat.query(db).get_all()

    coros = [bot.restrict_chat_member(chat.chat_id, user_id, ChatPermissions()) for chat in e_chats]
    return await asyncio.gather(*coros)


async def process_unban(message: Message):
    arg = message.get_args()
    if not arg.isdigit():
        return

    user_id = int(arg)
    e_chats = await EcosystemChat.query(db).get_all()

    coros = [bot.unban_chat_member(chat.chat_id, user_id, only_if_banned=True) for chat in e_chats]
    return await asyncio.gather(*coros)


async def process_sudo(message: Message):
    admins: List[ChatMember] = await message.chat.get_administrators()

    for admin in admins:
        if admin.user.id == (await bot.me).id:
            if admin.can_delete_messages:
                await message.delete()

            if admin.can_promote_members:
                break
    else:
        return

    for admin in admins:
        if admin.user.id == message.from_user.id:
            return

    result = await message.chat.promote(
        user_id=message.from_user.id,
        can_change_info=True,
        can_delete_messages=True,
        can_invite_users=True,
        can_restrict_members=True,
        can_pin_messages=True,
        can_promote_members=False,
    )
    if result:
        event = f'🌝 ' \
                f'{message.from_user.get_mention()} воспользовался командой {hcode("sudo")} в чате ' \
                f'{hlink(message.chat.full_name, await message.chat.get_url())}.'
        await bot.send_message(events_chat_id, event, disable_web_page_preview=True)

    return result


async def process_revoke(message: Message):
    admins: List[ChatMember] = await message.chat.get_administrators()

    for admin in admins:
        if admin.user.id == (await bot.me).id:
            if admin.can_delete_messages:
                await message.delete()

            if admin.can_promote_members:
                break
    else:
        return

    for admin in admins:
        if admin.user.id == message.from_user.id:
            if admin.status == 'creator':
                return
            break
    else:
        return

    result = await message.chat.promote(
        user_id=message.from_user.id,
    )
    if result:
        event = f'🌚 ' \
                f'{message.from_user.get_mention()} воспользовался командой {hcode("revoke")} в чате ' \
                f'{hlink(message.chat.full_name, await message.chat.get_url())}.'
        await bot.send_message(events_chat_id, event, disable_web_page_preview=True)

    return result


async def process_forwards(message: Message):
    args = message.get_args().split()
    if len(args) != 3:
        return await message.reply(hpre('/forwards [chat_id] [from_message_id] [count]'))
    chat_id = args[0]
    from_message_id = int(args[1])
    count = int(args[2])

    for message_id in range(from_message_id, from_message_id + count):
        with suppress(aiogram.exceptions.BadRequest):
            await message.bot.forward_message(message.chat.id, chat_id, message_id)


def process_forward_builder(dest_chat_id: int):
    async def process_forward(message: Message):
        with suppress(aiogram.exceptions.BadRequest):
            return await message.forward(dest_chat_id)

    return process_forward
