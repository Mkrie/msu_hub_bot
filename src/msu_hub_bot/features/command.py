"""Keep the bot's slash and hashtag grammar at its application boundary."""

from typing import Any

from aiogram import Bot
from aiogram.types import Message

from msu_hub_bot.telegram.filters import MetaCommand, MetaInfo


class HubCommand(MetaCommand):
    async def __call__(self, message: Message, bot: Bot) -> bool | dict[str, Any]:
        result = await super().__call__(message, bot)
        if not isinstance(result, dict):
            return result
        meta = result["meta"]
        assert isinstance(meta, MetaInfo)
        # Hashtag arguments are encoded after underscores, separately from text.
        prefix = " ".join(meta.arguments) if meta.hashtag else ""
        return {**result, "_teleforge_tail": " ".join(part for part in (prefix, meta.text) if part)}
