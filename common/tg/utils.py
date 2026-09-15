import asyncio
import io
from contextlib import suppress
from typing import Optional, Tuple, Iterable, Callable, List

import aiogram
from aiogram import Bot
from aiogram.types import Chat, ChatActions, ContentType, Document, Message, Update, User, MediaGroup, PhotoSize, MessageEntityType, ChatType
from aiogram.types.base import TelegramObject
from aiogram.utils.markdown import hide_link, hlink
from yarl import URL

from common.utils import one_liner, cut_long_text, FakeBytesIO


async def send_super_reply(message: Message,
                           text: str, web_preview: str = None, photos_urls: Iterable[str] = None, video_urls: Iterable[str] = None,
                           text_postprocess: Callable = None):
    return await send_super_message(message.bot,
                                    text, web_preview, photos_urls, video_urls,
                                    message.chat.id, message.message_id,
                                    text_postprocess)


async def send_super_message(bot: Bot,
                             text: str, web_preview: str, photos_urls: Iterable[str], video_urls: Iterable[str],
                             chat_id: int, reply_to: int = None,
                             text_postprocess: Callable = None):
    message = None
    text_postprocess = text_postprocess or (lambda x: x)

    if text:
        texts = [text_postprocess(t) for t in cut_long_text(text)]

        for t in texts[:-1]:
            message = await bot.send_message(chat_id=chat_id, text=t, disable_web_page_preview=True, reply_to_message_id=reply_to)
            reply_to = message.message_id

        t = (hide_link(web_preview) + texts[-1]) if web_preview else texts[-1]
        not_prev = not web_preview

        message = await bot.send_message(chat_id=chat_id, text=t, disable_web_page_preview=not_prev, reply_to_message_id=reply_to)
        reply_to = message.message_id

    if photos_urls:
        media = MediaGroup()
        for url in photos_urls:
            media.attach_photo(url)
        await bot.send_media_group(chat_id=chat_id, media=media, reply_to_message_id=reply_to)

    if video_urls:
        media = MediaGroup()
        for url in video_urls:
            media.attach_video(url)
        await bot.send_media_group(chat_id=chat_id, media=media, reply_to_message_id=reply_to)

    return message


def extract_urls(message: Message, include_text_link: bool = True) -> List[Tuple[URL, MessageEntityType]]:
    """
    Return list of tuples (URL, MessageEntityType)
    MessageEntityType can be
        - MessageEntityType.URL for simple urls
        - MessageEntityType.TEXT_LINK for urls in text
    """
    urls = []
    text, entities = message.text or message.caption, message.entities or message.caption_entities

    for entity in entities:
        url_text = None

        if entity.type == MessageEntityType.TEXT_LINK:
            if include_text_link:
                url_text = entity.url
        elif entity.type == MessageEntityType.URL:
            url_text = entity.get_text(text)

        if url_text:
            url = URL(url_text)
            if not url.scheme:
                url = URL('https://' + url_text)
            if not url.host:
                continue
            urls.append((url, entity.type))

    return urls


def user_info(user: User, sender_chat: Chat = None) -> str:
    if sender_chat:
        return chat_info(sender_chat)

    last_name = ' ' + user.last_name if user.last_name else ''
    username = ', @' + user.username if user.username else ''
    language_code = ', ' + user.language_code if user.language_code else ''
    return f'{user.first_name}{last_name} ({user.id}{username}{language_code})'


def chat_info(chat: Chat) -> str:
    if chat.type == 'private':
        return 'private'
    else:
        username = ', @' + chat.username if chat.username else ''
        return f'{chat.type} | {chat.title} ({chat.id}{username})'


def message_info(message: Message) -> str:
    prefix = f'{message.message_id} | '
    if message.text:
        return prefix + one_liner(message.text, cut_len=50)
    return prefix + f'type: {message.content_type}'


def decompose_update(update: Update) -> Tuple[TelegramObject, Optional[User], Optional[Chat], Optional[Chat], str]:
    user, sender_chat, chat = None, None, None

    if f := update.message:
        user = f.from_user
        sender_chat = f.sender_chat
        chat = f.chat
        info = message_info(f)
    elif f := update.edited_message:
        user = f.from_user
        sender_chat = f.sender_chat
        chat = f.chat
        info = message_info(f) + ' [edited]'
    elif f := update.channel_post:
        chat = f.chat
        info = message_info(f)
    elif f := update.edited_channel_post:
        chat = f.chat
        info = message_info(f) + ' [edited]'
    elif f := update.inline_query:
        user = f.from_user
        info = one_liner(f.query, cut_len=50)
    elif f := update.chosen_inline_result:
        user = f.from_user
        info = one_liner(f.query, cut_len=50)
    elif f := update.callback_query:
        if f.message:
            chat = f.message.chat
        user = f.from_user
        info = f.data
    elif f := update.shipping_query:
        user = f.from_user
        info = f.as_json()
    elif f := update.pre_checkout_query:
        user = f.from_user
        info = f.as_json()
    elif f := update.poll:
        info = f'{one_liner(f.question, cut_len=50)} ({f.id}), {[o.text for o in f.options]}, {f.total_voter_count} voter(s)'
    elif f := update.poll_answer:
        user = f.user
        info = f'{f.option_ids} ({f.poll_id})'
    elif f := (update.chat_member or update.my_chat_member):
        user = f.from_user
        chat = f.chat
        info = f'{user_info(f.new_chat_member.user)}: {f.old_chat_member.status} -> {f.new_chat_member.status}'
    else:
        f = update
        info = update.as_json()

    return f, user, sender_chat, chat, info


def parse_update(update: Update) -> Tuple[TelegramObject, Optional[str], Optional[str], str]:
    f, user, sender_chat, chat, info = decompose_update(update)
    user = user and user_info(user, sender_chat)
    chat = chat and chat_info(chat)
    return f, user, chat, info


def is_handled(results: list, data: dict) -> bool:
    if (results and results[0]) or len(data) > 2:
        return True
    return False


def username_link(user: User) -> str:
    if user.username:
        return '@' + user.username
    return user.get_mention()


def username_mention(user: User) -> str:
    name = None
    if user.username:
        name = '@' + user.username
    return user.get_mention(name)


def user_mention(user_id: int, name: str) -> str:
    return hlink(str(name), f'tg://user?id={user_id}')


async def chat_url(chat: Chat, force_link: bool = False) -> Optional[str]:
    if chat.type == ChatType.PRIVATE:
        return f'tg://user?id={chat.id}'

    if chat.username:
        return f'https://t.me/{chat.username}'

    if force_link:
        return await chat.get_url()

    return None


async def chat_link(chat: Chat, force_link: bool = False) -> str:
    url = await chat_url(chat, force_link)

    if url:
        return hlink(chat.full_name, url)

    return chat.full_name


def sender_mention(message: Message) -> str:
    if chat := message.sender_chat:
        if chat.username:
            url = f'https://t.me/{chat.username}'
            return hlink(chat.full_name, url)
        return chat.full_name
    return message.from_user.get_mention()


async def send_message_copy(message: Message, chat_id: int, disable_notification: bool = False) -> bool:
    return await send_message_copy_raw(message.bot, chat_id, message.chat.id, message.message_id, disable_notification)


async def send_message_copy_raw(bot: Bot, chat_id: int, from_chat_id: int, message_id: int, disable_notification: bool = False) -> bool:
    e = aiogram.exceptions

    try:
        await bot.copy_message(chat_id, from_chat_id, message_id, disable_notification=disable_notification)
    except (e.BotBlocked, e.ChatNotFound, e.UserDeactivated, e.TelegramAPIError):
        return False
    except e.RetryAfter as e:
        await asyncio.sleep(e.timeout)
        return await send_message_copy_raw(bot, chat_id, from_chat_id, message_id, disable_notification)

    return True


async def profile_photo(u: User) -> Optional[PhotoSize]:
    photos = await u.get_profile_photos(limit=1)
    if photos.total_count < 1:
        return None
    return photos.photos[0][-1]


async def extract_image(message: Message, with_reply=True, with_profile_photo=False):
    """
    TODO: DELETE THIS, REPLACE WITH Extractor

    Get image:
        - from message
        - from reply message
        - from reply user photo or author photo
    """

    async def extract_profile_photo(m: Message):
        if m.forward_from:
            if pp := await profile_photo(m.forward_from):
                return pp

        return await profile_photo(m.from_user)

    def extract_doc(d: Document):
        g, _, t = d.mime_type.partition('/')
        if g == 'image':
            if t.lower().endswith(('jpeg', 'png', 'tiff', 'bmp', 'gif', 'webp')):
                return d
        return None

    def extract(m: Message):
        if m.photo:
            return m.photo[-1]
        if m.document:
            return extract_doc(m.document)
        if m.sticker:
            if not m.sticker.is_animated:
                return m.sticker
        return None

    target = message
    dest = extract(target)

    if dest is None and with_reply:
        if target := message.reply_to_message:
            dest = extract(target)

    if dest is None and with_profile_photo:
        if with_reply:
            if target := message.reply_to_message:
                dest = await extract_profile_photo(target)

        if dest is None:
            target = message
            dest = await extract_profile_photo(target)

    return target, dest


async def download(o) -> Optional[io.BytesIO]:
    if o is not None:
        with suppress(aiogram.exceptions.BadRequest):
            return await o.download(destination_file=FakeBytesIO())
    return None


async def download_by_file_id(file_id: str) -> Optional[io.BytesIO]:
    with suppress(aiogram.exceptions.BadRequest):
        return await Bot.get_current().download_file_by_id(file_id, FakeBytesIO())
    return None


async def download_text(file_id: str) -> Optional[str]:
    with suppress(aiogram.exceptions.BadRequest):
        bytes_io = await download_by_file_id(file_id)
        return bytes_io and bytes_io.read().decode(encoding='utf-8')
    return None


def action_by_type(content_type: str) -> Optional[str]:
    ct = ContentType

    if content_type in (ct.TEXT, ct.STICKER, ct.POLL, ct.DICE):
        return ChatActions.TYPING

    if content_type in (ct.AUDIO, ct.VOICE):
        return ChatActions.UPLOAD_AUDIO

    if content_type == ct.DOCUMENT:
        return ChatActions.UPLOAD_DOCUMENT

    if content_type in (ct.ANIMATION, ct.VIDEO):
        return ChatActions.UPLOAD_VIDEO

    if content_type in (ct.PHOTO, 'list[photo]'):
        return ChatActions.UPLOAD_PHOTO

    if content_type == ct.VIDEO_NOTE:
        return ChatActions.UPLOAD_VIDEO_NOTE

    if content_type in (ct.LOCATION, ct.VENUE):
        return ChatActions.FIND_LOCATION

    if content_type in (
            ct.GAME, ct.CONTACT, ct.NEW_CHAT_MEMBERS, ct.LEFT_CHAT_MEMBER, ct.INVOICE, ct.SUCCESSFUL_PAYMENT, ct.CONNECTED_WEBSITE,
            ct.MIGRATE_TO_CHAT_ID, ct.MIGRATE_FROM_CHAT_ID, ct.PINNED_MESSAGE, ct.NEW_CHAT_TITLE, ct.NEW_CHAT_PHOTO, ct.DELETE_CHAT_PHOTO,
            ct.GROUP_CHAT_CREATED, ct.PASSPORT_DATA
    ):
        return None

    return None
