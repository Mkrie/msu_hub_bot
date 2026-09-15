from functools import cached_property

import aiojobs
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.types import Update

from common.db.edb import EdgeDB
from common.tg.utils import decompose_update, is_handled


class UpdatesMiddleware(BaseMiddleware):
    def __init__(self, db: EdgeDB):
        super().__init__()
        self.db = db

    @cached_property
    def scheduler(self) -> aiojobs.Scheduler:
        return aiojobs.Scheduler(close_timeout=0.3, limit=100, pending_limit=10_000, exception_handler=None)

    async def close(self):
        await self.scheduler.close()

    async def on_post_process_update(self, update: Update, results, data: dict):
        _, user, sender_chat, chat, _ = decompose_update(update)

        if user:
            coro = self.db.upsert('telegram::User', 'user_id',
                                  user_id=user.id,
                                  is_bot=user.is_bot,
                                  first_name=user.first_name,
                                  last_name=user.last_name,
                                  username=user.username,
                                  language_code=user.language_code)
            await self.scheduler.spawn(coro)

        if sender_chat:
            coro = self.db.upsert('telegram::Chat', 'chat_id',
                                  chat_id=sender_chat.id,
                                  type=sender_chat.type,
                                  title=sender_chat.title,
                                  username=sender_chat.username,
                                  first_name=sender_chat.first_name,
                                  last_name=sender_chat.last_name)
            await self.scheduler.spawn(coro)

        if chat:
            coro = self.db.upsert('telegram::Chat', 'chat_id',
                                  chat_id=chat.id,
                                  type=chat.type,
                                  title=chat.title,
                                  username=chat.username,
                                  first_name=chat.first_name,
                                  last_name=chat.last_name)
            await self.scheduler.spawn(coro)

        coro = self.db.insert('telegram::BotUpdate', data=update.to_python(), handled=is_handled(results, data))
        await self.scheduler.spawn(coro)
