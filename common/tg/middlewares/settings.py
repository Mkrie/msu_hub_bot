import cachetools
from aiogram.dispatcher.middlewares import LifetimeControllerMiddleware
from aiogram.types import Chat
from pydantic import BaseSettings, root_validator, Extra
from throttler import ThrottlerSimultaneous

from common import json
from common.db.edb import EdgeDB, ChatDB


class Settings(BaseSettings):
    auto_speech_recognition: bool = True
    auto_video_links: bool = True
    with_nsfw: bool = False

    _is_dirty: bool = False
    _chat_id: int = None

    class Config:
        validate_all = True
        extra = Extra.allow
        validate_assignment = True
        json_loads = json.loads
        json_dumps = json.dumps

    @classmethod
    async def create(cls, db: EdgeDB, chat_id: int) -> 'Settings':
        chat_db = await ChatDB.query(db).get(chat_id)
        if isinstance(chat_db.metadata, str):
            # баг бля с '{}'
            chat_db.metadata = {}
        settings = chat_db.metadata.get('settings', {})
        settings['_chat_id'] = chat_id
        obj = cls.parse_obj(settings)
        obj.__dict__['_is_dirty'] = False  # to skip validator
        return obj

    @root_validator
    def set_as_dirty(cls, values):
        values['_is_dirty'] = True
        return values

    async def save(self, db: EdgeDB, force=False) -> 'Settings':
        if self._is_dirty or force:
            chat_db = await ChatDB.query(db).get(self._chat_id)
            if isinstance(chat_db.metadata, str):
                # баг бля с '{}'
                chat_db.metadata = {}
            chat_db.metadata['settings'] = self.dict(exclude={'_is_dirty', '_chat_id'})
            await ChatDB.query(db).update(self._chat_id, metadata=chat_db.metadata)
        self.__dict__['_is_dirty'] = False  # to skip validator
        return self


class SettingsMiddleware(LifetimeControllerMiddleware):
    skip_patterns = ['error', 'update']

    def __init__(self, db: EdgeDB):
        super().__init__()
        self.db = db
        self.proxies = cachetools.LRUCache(maxsize=128)
        self.throttler = ThrottlerSimultaneous(count=1)

    async def proxy(self, chat_id: int) -> Settings:
        async with self.throttler:
            if chat_id not in self.proxies:
                self.proxies[chat_id] = await Settings.create(self.db, chat_id)
        return self.proxies[chat_id]

    async def pre_process(self, obj, data, *args):
        chat = Chat.get_current()
        if not isinstance(chat, Chat):
            return
        data['settings'] = await self.proxy(chat.id)

    async def post_process(self, obj, data, *args):
        proxy = data.get('settings', None)
        if isinstance(proxy, Settings):
            await proxy.save(self.db)
