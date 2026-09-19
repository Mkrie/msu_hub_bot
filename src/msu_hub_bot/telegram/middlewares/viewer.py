"""Automatic message previews, after routing and without holding FSM isolation."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.dispatcher.flags import get_flag
from aiogram.types import Message, TelegramObject

from msu_hub_bot.media.limits import MAX_DOWNLOAD_BYTES
from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.providers.pdf import convert_to_pdf
from msu_hub_bot.providers.vk.api import VkApi
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.files import DownloadTooLarge, download, input_file
from msu_hub_bot.telegram.links.service import LinkService, PreviewExecutor
from msu_hub_bot.telegram.middlewares.settings import Settings
from msu_hub_bot.telegram.state import UpdateStateContext, release_state_isolation
from msu_hub_bot.telegram.wrapper import BotWrapper
from msu_hub_bot.telemetry import Telemetry
from msu_hub_bot.utils import megabytes


@dataclass(slots=True)
class _PreviewContext:
    suppressed: bool = False
    x_suppressed: bool = False


async def preview_policy(
    handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]
) -> Any:
    """Carry a completed handler's explicit policy across router data copies."""
    result = await handler(event, data)
    context = data.get("preview_context")
    if isinstance(context, _PreviewContext) and result is not UNHANDLED:
        context.suppressed = get_flag(data, "automatic_previews") is False
        context.x_suppressed |= "command" in data or "meta" in data
    return result


class ViewerMiddleware(BaseMiddleware):
    def __init__(self, bot: BotWrapper, vk_api: VkApi, executor: PreviewExecutor, *, telemetry: Telemetry | None = None) -> None:
        self.telemetry = telemetry or Telemetry()
        self.links = LinkService(bot, vk_api, executor, telemetry=self.telemetry)

    async def view(self, message: Message, preferences: Settings, *, x_previews: bool = True) -> None:
        await self.links.view(message, preferences, x_previews=x_previews)
        destination = message.document
        extensions = tuple(
            f".{extension}"
            for extension in (
                "azw",
                "azw3",
                "azw4",
                "cbr",
                "cbz",
                "cgm",
                "chm",
                "djv",
                "djvu",
                "doc",
                "docx",
                "epub",
                "fb2",
                "lit",
                "lrf",
                "mobi",
                "odg",
                "odm",
                "odp",
                "ppt",
                "pptx",
                "rb",
                "sda",
                "sdc",
                "sdd",
                "sdp",
                "sdw",
                "uof",
                "uop",
                "uos",
                "wks",
                "wmf",
                "wpd",
                "wps",
                "xbm",
                "xps",
            )
        )
        if destination is None or not destination.file_name or not destination.file_name.endswith(extensions):
            return
        if destination.file_size is not None and destination.file_size >= megabytes(20):
            return
        try:
            file = await download(destination, bot_for(message), max_bytes=MAX_DOWNLOAD_BYTES)
            if file is None:
                return
            with file:
                if file.getbuffer().nbytes >= megabytes(20):
                    return
                with await convert_to_pdf(file, destination.file_name, destination.mime_type or "application/octet-stream") as converted:
                    result = input_file(converted, str(getattr(converted, "name", "document.pdf")))
        except ExternalServiceError, TimeoutError, DownloadTooLarge:
            return
        await message.reply_document(result)

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        preview_context = _PreviewContext(x_suppressed=data.get("raw_state") is not None)
        data["preview_context"] = preview_context
        result = await handler(event, data)
        if isinstance(event, Message):
            preferences = data.get("settings")
            if not isinstance(preferences, Settings):
                raise RuntimeError("Viewer middleware requires chat preferences")
            context = data.get("state_context")
            if isinstance(context, UpdateStateContext):
                release_state_isolation(context)
            if not preview_context.suppressed:
                await self.view(event, preferences, x_previews=not preview_context.x_suppressed)
        return result
