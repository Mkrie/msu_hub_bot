import asyncio
import time
from contextlib import suppress
from typing import Optional

import aiogram
from aiogram import Bot
from aiogram.contrib.fsm_storage.redis import RedisStorage2
from aiogram.types import Message
from pendulum import DateTime
from redis import asyncio as aioredis


class RedisStorageBase(RedisStorage2):
    async def redis(self) -> aioredis.Redis:
        return self._redis

    async def exists(self, key: str) -> bool:
        redis = await self.redis()
        value = await redis.exists(key)
        return bool(value)

    async def get(self, key: str, default: str = None) -> str:
        redis = await self.redis()
        value = await redis.get(key)
        return value or default

    async def set(self, key: str, value: str) -> bool:
        redis = await self.redis()
        count = await redis.set(key, value)
        return bool(count)

    async def dict_get(self, key: str, field: str, default: str = None) -> str:
        redis = await self.redis()
        value = await redis.hget(key, field)
        return value or default

    async def dict_set(self, key: str, field: str, value: str) -> bool:
        redis = await self.redis()
        count = await redis.hset(key, field, value)
        return bool(count)

    async def dict_set_many(self, key: str, d: dict) -> bool:
        redis = await self.redis()
        count = await redis.hset(key, mapping=d)
        return bool(count)

    async def dict_remove(self, key: str, field: str) -> bool:
        redis = await self.redis()
        count = await redis.hdel(key, field)
        return bool(count)

    async def dict_all(self, key: str) -> dict:
        redis = await self.redis()
        return await redis.hgetall(key)

    async def get_config(self, config: str, default: str = '') -> str:
        key = self.generate_key('global', 'config')
        return await self.dict_get(key, config, default)

    async def set_config(self, config: str, value: str) -> bool:
        key = self.generate_key('global', 'config')
        return await self.dict_set(key, config, value)


class RedisStorage(RedisStorageBase):
    async def get_dt(self, config: str, default: DateTime = None) -> Optional[DateTime]:
        value = await self.get_config(config)
        if value:
            return DateTime.fromisoformat(value)
        return default

    async def set_dt(self, config: str, dt: DateTime = None) -> bool:
        dt = dt or DateTime.now()
        return await self.set_config(config, dt.isoformat())

    async def mark_message_to_delete_raw(self, chat_id: int, message_id: int, after: int) -> bool:
        key = self.generate_key('bot', 'to_delete')
        at_ts = int(time.time() + after)
        return await self.dict_set(key, f'{chat_id}_{message_id}', f'{at_ts}')

    async def mark_message_to_delete(self, message: Message, after: int) -> bool:
        return await self.mark_message_to_delete_raw(message.chat.id, message.message_id, after)

    async def process_messages_to_delete(self, bot: Bot):
        key = self.generate_key('bot', 'to_delete')
        d = await self.dict_all(key)

        async def delete(field: str, wait: int):
            chat_id, message_id = map(int, field.split('_'))
            with suppress(aiogram.exceptions.TelegramAPIError):
                await asyncio.sleep(wait)
                await bot.delete_message(chat_id, message_id)
            return await self.dict_remove(key, field)

        curr_ts = int(time.time())
        for chat_message, ts in d.items():
            diff = max(int(ts) - curr_ts, 0)
            if diff < 60:
                asyncio.create_task(delete(chat_message, wait=diff))

        return True
