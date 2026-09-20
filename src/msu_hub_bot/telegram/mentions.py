"""Explicit bot mentions in a new plain-text invocation."""

from typing import Any

from aiogram import Bot
from aiogram.enums import ContentType, MessageEntityType
from aiogram.filters import Filter
from aiogram.types import Message


class IntentMention(Filter):
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled

    async def __call__(self, message: Message, bot: Bot) -> bool | dict[str, Any]:
        text = message.text
        if (
            not self.enabled
            or not text
            or message.content_type != ContentType.TEXT
            or message.from_user is None
            or message.from_user.is_bot
            or text.lstrip().startswith(("/", "#"))
        ):
            return False
        mentions = [
            entity for entity in message.entities or () if entity.type in {MessageEntityType.MENTION, MessageEntityType.TEXT_MENTION}
        ]
        if not mentions:
            return False
        username = (await bot.me()).username
        matched = [
            entity
            for entity in mentions
            if (
                entity.type == MessageEntityType.MENTION
                and username is not None
                and entity.extract_from(text).casefold() == f"@{username}".casefold()
            )
            or (entity.type == MessageEntityType.TEXT_MENTION and entity.user is not None and entity.user.id == bot.id)
        ]
        if not matched:
            return False
        # Telegram entity offsets count UTF-16 units, including astral emoji.
        encoded = text.encode("utf-16-le")
        for entity in sorted(matched, key=lambda item: item.offset, reverse=True):
            encoded = encoded[: entity.offset * 2] + encoded[(entity.offset + entity.length) * 2 :]
        return {"intent_request": encoded.decode("utf-16-le").strip(" \n\t,:;—–-")}
