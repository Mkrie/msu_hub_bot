import asyncio
from collections import defaultdict
from contextlib import suppress
from enum import Enum

import aiogram
from aiocache import cached
from aiogram import Bot
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.types import Chat, Message, ChatType
from aiogram.utils.markdown import hbold, hitalic, hlink, hcode, hide_link

from common.db.edb import EdgeDB
from db import EcosystemChat
from common.mixins import LoggerMixin
from common.utils import random_cycle


class EventsMiddleware(BaseMiddleware, LoggerMixin):
    def __init__(self, bot: Bot, db: EdgeDB, events_chat_id: int):
        super().__init__()
        self.bot = bot
        self.db = db
        self.events_chat_id = events_chat_id
        self.em = EcosystemManager(bot, db)

    async def send_event(self, text: str, preview: bool = False, keyboard=None):
        if not self.events_chat_id:
            return
        return await self.bot.send_message(self.events_chat_id, text,
                                           disable_web_page_preview=not preview, reply_markup=keyboard)

    async def log_event_from_ic(self, message: Message):
        if not await EcosystemChat.query(self.db).exist_cached(message.chat.id):
            return

        chat = message.chat
        chat_link = hlink(chat.full_name, await chat.get_url())

        if message.new_chat_members:
            text = f'👤 Новый пользователь ' if len(message.new_chat_members) == 1 else f'👥 Новые пользователи '
            text += f'в {chat_link}:\n'
            for member in message.new_chat_members:
                text += f'— {member.get_mention(as_html=True)}, #{member.id}\n'

            event = await self.send_event(text)
            await self.em.update_pins()
            return event

        if message.left_chat_member:
            text = f'👣 Ушел пользователь из {chat_link}:\n' \
                   f'— {message.left_chat_member.get_mention(as_html=True)}, #{message.left_chat_member.id}'

            event = await self.send_event(text)
            await self.em.update_pins()
            return event

        if message.new_chat_title:
            text = f'🔄 Новое название чата: {chat_link}'

            return await self.send_event(text)

        if message.new_chat_photo:
            text = f'🔄 Новое фото у чата: {chat_link}'

            return await self.send_event(text)

        if message.delete_chat_photo:
            text = f'🔄 Удалено фото чата: {chat_link}'

            return await self.send_event(text)

        if message.pinned_message:
            text = f'📍 Новый закреп в {chat_link}: ' \
                   f'{message.pinned_message.link(str(message.pinned_message.message_id))}'

            return await self.send_event(text)

    async def log_event(self, message: Message):
        if message.new_chat_members:
            for member in message.new_chat_members:
                if member.id == (await self.bot.me).id:
                    user_link = message.from_user.get_mention(as_html=True)
                    chat_link = hlink(message.chat.full_name, await message.chat.get_url())
                    members = await message.chat.get_member_count()
                    text = f'❇️ {user_link} добавил бота в чат:\n— {chat_link}, {members} уч.'
                    await self.send_event(text)
                    break

        if message.group_chat_created or message.supergroup_chat_created or message.channel_chat_created:
            user_link = message.from_user.get_mention(as_html=True)
            chat_link = hlink(message.chat.full_name, await message.chat.get_url())
            members = await message.chat.get_member_count()
            chat_type = 'канал' if message.chat.type == ChatType.CHANNEL else 'чат'
            text = f'❇️ {user_link} создал {chat_type} с ботом:\n— {chat_link}, {members} уч.'
            await self.send_event(text)

        if message.migrate_from_chat_id:
            await message.answer(f'✳️ Этот чат мигрировал в супергруппу, предыдущий номер: {hcode(message.migrate_from_chat_id)}, '
                                 f'текущий номер: {hcode(message.chat.id)}.\n\n'
                                 f'{hitalic("Примечание")}: реплаи на сообщения сверху работать не будут.\n\n'
                                 f'{hitalic("Примечание")}: если вы создавали стикеры чата, то сейчас они мне будут недоступны. '
                                 f'Но это не помешает мне создать новый пак.')

    async def on_pre_process_message(self, message: Message, _data: dict):
        await self.log_event(message)
        return await self.log_event_from_ic(message)


class EcosystemManager:
    def __init__(self, bot: Bot, db: EdgeDB):
        self.bot = bot
        self.db = db

        self.images = random_cycle(
            'https://i.imgur.com/AA3fgbf.png',
            'https://i.imgur.com/1bi3Aki.png',
            'https://i.imgur.com/PRZpGY0.png',
            'https://i.imgur.com/RKdrRVo.jpeg',
            'https://i.imgur.com/Cuem492.jpeg',
            'https://i.imgur.com/vuCnUnI.jpeg',
            'https://i.imgur.com/VjkoGa3.jpeg',
            'https://i.imgur.com/1TKmsDQ.jpeg',
            'https://i.imgur.com/8i3wjV4.jpeg',
            'https://i.imgur.com/ygG7bhD.jpeg',
        )

    async def pin(self, chat_id: int, forced: bool = False):
        e_chat = await EcosystemChat.query(self.db).get_cached(chat_id)

        if e_chat and e_chat.pinned_message_id:
            if not forced:
                return True

            with suppress(aiogram.exceptions.MessageError):
                await self.bot.delete_message(chat_id, e_chat.pinned_message_id)

        text = await self.text(chat_id)
        pin_msg = await self.bot.send_message(chat_id, text)
        await pin_msg.pin(disable_notification=True)
        await EcosystemChat.query(self.db).update(pk=chat_id, pinned_message_id=pin_msg.message_id)
        return pin_msg

    @cached(ttl=10 * 60, noself=True)
    async def update_pins(self):
        await self.update_ic_members()

        e_chats = await EcosystemChat.query(self.db).get_all_cached()
        with suppress(aiogram.exceptions.MessageError):
            coros = [self.bot.edit_message_text(await self.text(ec.chat_id), ec.chat_id, ec.pinned_message_id)
                     for ec in e_chats.values() if ec.pinned_message_id]
            await asyncio.gather(*coros)

    async def update_ic_members(self):
        e_chats = await EcosystemChat.query(self.db).get_all_cached()

        async def update_single(ec):
            chat = await self.get_chat(ec.chat_id)
            members = await chat.get_member_count()
            if members != ec.members:
                return await EcosystemChat.query(self.db).update(pk=ec.chat_id, members=members)

        coros = [update_single(ec) for ec in e_chats.values()]
        return await asyncio.gather(*coros)

    @cached(ttl=10 * 60, noself=True)
    async def get_chat(self, chat_id: int) -> Chat:
        return await self.bot.get_chat(chat_id)

    @cached(ttl=10 * 60, noself=True)
    async def link(self, chat_id: int):
        chat = await self.get_chat(chat_id)
        if chat.username:
            return '@' + chat.username

        e_chats = await EcosystemChat.query(self.db).get_all_cached()
        if e_chats[chat_id].username_alias:
            return '@' + e_chats[chat_id].username_alias

        return hlink('ссылка', await chat.get_url())

    class ChatGroup(Enum):
        main = '⚜️ Основные ресурсы'
        dormitory = '🏘 По общежитиям'
        faculty = '👨🏻‍🎓 По факультетам'
        filial = '🗺 По филиалам'
        interest = '🗿 Тематика'
        camp = '🏖 Лагеря'
        other = '🍒 Всякие разные'
        channel = '📜 Каналы'

    async def text(self, chat_id: int = None):
        e_chats = await EcosystemChat.query(self.db).get_all_cached()

        groups = defaultdict(list)
        for e_chat in e_chats.values():
            groups[e_chat.section].append(e_chat)

        text = hide_link(next(self.images)) + hbold('Экосистема чатов МГУ ✨\n')
        text += '— это сообщество студентов и выпускников МГУ\n' \
                '— здесь приветствуется взаимопомощь в любом виде\n' \
                '— спам удаляется, а агрессия не одобряется\n\n'

        if chat_id is not None and e_chats[chat_id].members < 30:
            text += hitalic('Disclaimer: ') + \
                    'этот чат развивается, поэтому здесь еще мало людей, ' \
                    'но если приглашать друзей и вести интересные обсуждения, ' \
                    'то совсем скоро он оживет.\n\n'

        for group in self.ChatGroup:
            chats = sorted(filter(lambda ec: not ec.is_hidden, groups[group.name]), key=lambda x: x.members or 0, reverse=True)
            if chats:
                text += f'{hbold(group.value)}:\n'
                for chat in chats:
                    text += '— {name}{members} | {link}\n'.format(
                        name=chat.name,
                        members=' | ' + hitalic(f'{chat.members} уч.') if chat.members else '',
                        link=await self.link(chat.chat_id),
                    )
                text += '\n'

        text += hbold('Приятного общения ✌🏻')

        return text

