"""Validated, shared chat preferences with snapshot-safe persistence."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Chat, TelegramObject
from msu_hub_bot.telemetry import Backend, Boundary, Telemetry

from pydantic import JsonValue, PrivateAttr, TypeAdapter

from msu_hub_bot.storage.application import ChatPreferences
from msu_hub_bot.storage.base import BotRepository
from msu_hub_bot.storage.observations import chat_observation

_JSON_SETTINGS = TypeAdapter(dict[str, JsonValue])


class Settings(ChatPreferences):
    _chat_id: int | None = PrivateAttr(default=None)
    _saved_snapshot: dict[str, Any] = PrivateAttr(default_factory=dict)
    _save_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)

    @property
    def _is_dirty(self) -> bool:
        return self.model_dump() != self._saved_snapshot

    @classmethod
    async def create(cls, db: BotRepository, chat: Chat) -> Settings:
        values = dict(await db.load_settings(chat_observation(chat)))
        for internal in ("_chat_id", "_is_dirty", "_saved_snapshot", "_save_lock"):
            values.pop(internal, None)
        settings = cls.model_validate(values)
        settings._chat_id = chat.id
        settings._saved_snapshot = settings.model_dump()
        return settings

    async def save(self, db: BotRepository, force: bool = False) -> Settings:
        async with self._save_lock:
            if not self._is_dirty and not force:
                return self
            if self._chat_id is None:
                raise RuntimeError("Chat preferences have no persistence identity")
            snapshot = _JSON_SETTINGS.validate_python(self.model_dump())
            changes = {
                key: value
                for key, value in snapshot.items()
                if force or key not in self._saved_snapshot or value != self._saved_snapshot[key]
            }
            await db.patch_settings(self._chat_id, changes)
            self._saved_snapshot = snapshot
        return self

    async def refresh(self, db: BotRepository, chat: Chat) -> None:
        """Apply external changes in place while retaining unsaved handler edits."""
        async with self._save_lock:
            persisted = (await Settings.create(db, chat)).model_dump()
            current = self.model_dump()
            dirty = {key: value for key, value in current.items() if key not in self._saved_snapshot or value != self._saved_snapshot[key]}
            merged = persisted | dirty
            for key in current.keys() - merged.keys():
                delattr(self, key)
            for key, value in merged.items():
                setattr(self, key, value)
            self._saved_snapshot = persisted


class SettingsMiddleware(BaseMiddleware):
    def __init__(
        self,
        db: BotRepository,
        *,
        cache_size: int = 128,
        telemetry: Telemetry | None = None,
        backend: Backend = Backend.SUPABASE,
    ) -> None:
        if cache_size < 1:
            raise ValueError("Preference cache size must be positive")
        self.db = db
        self.telemetry = telemetry or Telemetry()
        self.backend = backend
        self.cache_size = cache_size
        self.proxies: OrderedDict[int, Settings] = OrderedDict()
        self._active: dict[int, int] = {}
        self._load_lock = asyncio.Lock()
        self._loading_chat_id: int | None = None
        self._stale: set[int] = set()

    async def invalidate(self, chat_id: int) -> None:
        """Notify a confirmed external write without discarding shared objects."""
        if chat_id in self.proxies or self._loading_chat_id == chat_id:
            # A first read already in flight may predate the external commit.
            self._stale.add(chat_id)

    async def proxy(self, chat: Chat) -> Settings:
        if chat.id in self.proxies and chat.id not in self._stale and chat.id != self._loading_chat_id:
            self.proxies.move_to_end(chat.id)
            return self.proxies[chat.id]
        with self.telemetry.operation(Boundary.STORAGE, "settings.load", backend=self.backend, trace=False):
            return await self._load(chat)

    async def _load(self, chat: Chat) -> Settings:
        async with self._load_lock:
            self._loading_chat_id = chat.id
            try:
                if chat.id not in self.proxies:
                    self.proxies[chat.id] = await Settings.create(self.db, chat)
                elif chat.id in self._stale:
                    self._stale.discard(chat.id)
                    try:
                        await self.proxies[chat.id].refresh(self.db, chat)
                    except BaseException:
                        self._stale.add(chat.id)
                        raise
            finally:
                self._loading_chat_id = None
                if chat.id not in self.proxies:
                    self._stale.discard(chat.id)
            self.proxies.move_to_end(chat.id)
            return self.proxies[chat.id]

    def _trim(self) -> None:
        # Active or unsaved objects must remain shared until their handlers finish.
        for chat_id in list(self.proxies):
            if len(self.proxies) <= self.cache_size:
                break
            if chat_id != self._loading_chat_id and not self._active.get(chat_id) and not self.proxies[chat_id]._is_dirty:
                del self.proxies[chat_id]
                self._stale.discard(chat_id)

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        data.pop("settings", None)
        chat = getattr(event, "chat", None) or getattr(getattr(event, "message", None), "chat", None)
        if not isinstance(chat, Chat):
            return await handler(event, data)
        preferences = await self.proxy(chat)
        self._active[chat.id] = self._active.get(chat.id, 0) + 1
        data["settings"] = preferences
        try:
            try:
                result = await handler(event, data)
            except BaseException as original:
                try:
                    await self._save(preferences)
                except Exception:
                    original.add_note("Chat preferences also failed to save during cleanup")
                raise
            await self._save(preferences)
            return result
        finally:
            self._active[chat.id] -= 1
            if not self._active[chat.id]:
                del self._active[chat.id]
            self._trim()

    async def close(self) -> None:
        failures: list[Exception] = []
        for preferences in list(self.proxies.values()):
            try:
                await self._save(preferences)
            except Exception as error:
                failures.append(error)
        if failures:
            raise ExceptionGroup("Chat preferences failed to save during shutdown", failures)

    async def _save(self, preferences: Settings) -> None:
        if preferences._is_dirty:
            with self.telemetry.operation(Boundary.STORAGE, "settings.save", backend=self.backend):
                await preferences.save(self.db)
