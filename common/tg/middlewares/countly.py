import json
import time
import uuid
from functools import cached_property

import aiohttp
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.types import Update

from common.tg.utils import is_handled, decompose_update, profile_photo
from msu_hub_bot.settings import settings


# async def profile_photo(u: User) -> Optional[PhotoSize]:
#     photos = await u.get_profile_photos(limit=1)
#     if photos.total_count < 1:
#         return None
#     return photos.photos[0][-1]


class Countly:
    """
    Docs: https://api.count.ly/reference/i
    """
    api_url = settings.countly_url
    request_headers = {"Accept": "application/json"}

    def __init__(self, app_key: str):
        self.app_key = app_key

    @cached_property
    def session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(headers=self.request_headers)

    async def close(self):
        await self.session.close()

    async def track(self, update: Update, handled: bool):
        settings.require('countly_url')
        f, user, sender_chat, chat, _ = decompose_update(update)

        device_id = user and user.id or str(uuid.uuid4())

        events = [{
            'key': type(f).__name__,
            'count': 1,
            'timestamp': int(getattr(f, 'date', None) and getattr(f, 'date').timestamp() or time.time()),
            "segmentation": {
                "handled": handled,
            }
        }]

        user_details = {}
        if user:
            pic = await profile_photo(user)

            user_details = {
                "name": user.full_name,
                "username": user.username,
                # "picture": await pic.get_url(),
                # "custom": {
                #     "user_id": user.id,
                #     "language_code": user.language_code,
                #     "chat_ids": {"$addToSet": chat.id},
                # }
            }

        params = {
            'app_key': self.app_key,
            'device_id': device_id,
            'begin_session': 1,
            'session_duration': 60,
            'events': json.dumps(events),
            'user_details': json.dumps(user_details),
            'country_code': user and user.language_code and user.language_code[-2:]
        }

        async with self.session.get(url=self.api_url, params=params) as response:
            response.raise_for_status()
            print(await response.read())


class CountlyMiddleware(BaseMiddleware):
    def __init__(self, app_key: str):
        super().__init__()

        self.countly = Countly(app_key)

    async def close(self):
        await self.countly.close()

    async def on_post_process_update(self, update: Update, results, data: dict):
        await self.countly.track(update, handled=is_handled(results, data))
