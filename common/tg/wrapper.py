import asyncio
from typing import Iterable, List

import aiogram
import cachetools
from aiogram import Bot
from aiogram.types import MediaGroup, Chat, ChatType
from aiogram.utils.markdown import hide_link
from throttler import ThrottlerSimultaneous

from common.constants import TELEGRAM_CAPTION_MAX_LEN
from common.logger import LoggerBuilder
from common.utils import cut_long_text, retry_async
from msu_hub_bot.health import mark_poll_success


class RetryAtThisError(Exception):
    pass


class BotWrapper(Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.logger = LoggerBuilder.get_logger('SafeBot')
        self.throttlers = cachetools.LRUCache(maxsize=128)

    @retry_async(RetryAtThisError, retries_count=2, sleep_for=1.)
    async def request(self, *args, **kwargs):
        ex = aiogram.exceptions

        try:
            result = await super().request(*args, **kwargs)
            method = args[0] if args else kwargs.get('method')
            if method == 'getUpdates':
                mark_poll_success()
            return result

        except ex.RetryAfter as e:
            await asyncio.sleep(e.timeout)
            return await self.request(*args, **kwargs)

        except ex.BadRequest as e:
            error = e.args[0]
            chat = Chat.get_current()
            if error == 'Have no rights to send a message':
                if chat.type != ChatType.PRIVATE:
                    await chat.leave()
            if error in ('Not enough rights to send stickers to the chat',
                         'Chat_send_gifs_forbidden',
                         'Not enough rights to send polls to the chat'):
                pass
            raise

        except ex.TelegramAPIError as e:
            error = e.args[0]
            if error == 'Gateway Timeout':
                raise RetryAtThisError()
            raise

        except asyncio.exceptions.TimeoutError:
            raise RetryAtThisError()

    def throttler(self, chat_id: int) -> ThrottlerSimultaneous:
        if chat_id in self.throttlers:
            throttler = self.throttlers[chat_id]
        else:
            throttler = ThrottlerSimultaneous(count=1)
            self.throttlers[chat_id] = throttler
        return throttler

    @retry_async(aiogram.exceptions.TelegramAPIError, retries_count=5, sleep_for=1.)
    async def safe_send_message(self, *args, **kwargs):
        async with self.throttler(kwargs['chat_id']):
            return await self.send_message(*args, **kwargs)

    @retry_async(aiogram.exceptions.TelegramAPIError, retries_count=10, sleep_for=1.5)
    async def safe_send_media_group(self, *args, **kwargs):
        async with self.throttler(kwargs['chat_id']):
            return await self.send_media_group(*args, **kwargs)

    async def send_super_message(self,
                                 text: str, web_preview: str, photos_urls: Iterable[str], video_urls: Iterable[str],
                                 chat_id: int, reply_to: int = None):
        message = None

        if text:
            texts = cut_long_text(text)

            for t in texts[:-1]:
                message = await self.safe_send_message(chat_id=chat_id, text=t, disable_web_page_preview=True, reply_to_message_id=reply_to)
                reply_to = message.message_id

            t = (hide_link(web_preview) + texts[-1]) if web_preview else texts[-1]
            not_prev = not web_preview

            message = await self.safe_send_message(chat_id=chat_id, text=t, disable_web_page_preview=not_prev, reply_to_message_id=reply_to)
            reply_to = message.message_id

        if photos_urls:
            media = MediaGroup()
            for url in photos_urls:
                media.attach_photo(url)
            await self.safe_send_media_group(chat_id=chat_id, media=media, reply_to_message_id=reply_to)

        if video_urls:
            media = MediaGroup()
            for url in video_urls:
                media.attach_video(url)
            await self.safe_send_media_group(chat_id=chat_id, media=media, reply_to_message_id=reply_to)

        return message

    async def send_super_message_prefer_album(self,
                                              text: str, web_preview: str, photos_urls: List[str], video_urls: List[str],
                                              chat_id: int, reply_to: int = None):
        message = None

        if len(text) > TELEGRAM_CAPTION_MAX_LEN or len(photos_urls) + len(video_urls) == 0:
            if len(photos_urls) + len(video_urls) == 1:
                web_preview = photos_urls[0] if photos_urls else video_urls[0]
                photos_urls, video_urls = [], []
            return await self.send_super_message(text, web_preview, photos_urls, video_urls, chat_id, reply_to)

        if photos_urls:
            media = MediaGroup()
            for url in photos_urls:
                media.attach_photo(url, caption=text)
                text = None
            messages = await self.safe_send_media_group(chat_id=chat_id, media=media, reply_to_message_id=reply_to)
            message = messages[0]

        if video_urls:
            media = MediaGroup()
            for url in video_urls:
                media.attach_video(url, caption=text)
                text = None
            messages = await self.safe_send_media_group(chat_id=chat_id, media=media, reply_to_message_id=reply_to)
            message = messages[0]

        return message
