"""Photo and video captions with declared inputs and application media admission."""

import io
from collections.abc import Callable
from contextlib import closing

from PIL import Image
from aiogram.enums import ChatAction, ChatType
from aiogram.types import Animation, BufferedInputFile, Document, Message, Sticker, Video, VideoNote
from teleforge import Feature, command
from teleforge.context import MessageContext
from teleforge.inputs import MediaInput, TextInput

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.features.command import HubCommand
from msu_hub_bot.media.caption_layout import CaptionLayoutError, CaptionStyle, caption_image
from msu_hub_bot.media.caption_video import caption_video
from msu_hub_bot.telegram.chat_actioner import ChatActioner
from msu_hub_bot.telegram.files import DownloadableMedia
from msu_hub_bot.telegram.keyboards import rate_keyboard
from msu_hub_bot.telegram.media_jobs import DownloadUnavailable, run_downloaded
from msu_hub_bot.utils import image_bytes_io


class Captions(Feature, key="captions"):
    def __init__(self, executor: TPExecutor) -> None:
        self.executor = executor

    @command(
        "meme",
        filter=HubCommand("meme"),
        text=TextInput(),
        media=MediaInput(kinds=("video", "image"), avatar=True),
        flags={"handler_key": "process_meme", "fsm_release": True},
    )
    async def meme(self, ctx: MessageContext, text: str, media: DownloadableMedia) -> None:
        await self.render(ctx, text, media, "meme")

    @command(
        "lobster",
        "l",
        "л",
        "лобстер",
        filter=HubCommand("lobster", "l", "л", "лобстер"),
        text=TextInput(),
        media=MediaInput(kinds=("video", "image"), avatar=True),
        flags={"handler_key": "process_lobster", "fsm_release": True},
    )
    async def lobster(self, ctx: MessageContext, text: str, media: DownloadableMedia) -> None:
        await self.render(ctx, text, media, "lobster")

    @command(
        "demotivator",
        "de",
        "д",
        "де",
        filter=HubCommand("demotivator", "de", "д", "де"),
        text=TextInput(),
        media=MediaInput(kinds=("video", "image"), avatar=True),
        flags={"handler_key": "process_demotivator", "fsm_release": True},
    )
    async def demotivator(self, ctx: MessageContext, text: str, media: DownloadableMedia) -> None:
        await self.render(ctx, text, media, "demotivator")

    async def render(self, ctx: MessageContext, text: str, media: DownloadableMedia, style: CaptionStyle) -> None:
        assert isinstance(ctx.message, Message)
        video = (
            isinstance(media, (Video, Animation, VideoNote))
            or isinstance(media, Sticker)
            and media.is_video
            or isinstance(media, Document)
            and (media.mime_type or "").startswith("video/")
        )
        renderer: Callable[[io.BytesIO, str, CaptionStyle], Image.Image | io.BytesIO | None]
        renderer = caption_video if video else caption_image
        async with ChatActioner(ctx.message, ChatAction.UPLOAD_VIDEO if video else ChatAction.UPLOAD_PHOTO):
            try:
                result, timed_out = await run_downloaded(self.executor, media, renderer, text, style, bot=ctx.bot)
            except DownloadUnavailable:
                await ctx.reply("🤷🏻‍♂️ Не удалось скачать файл. Попробуй прислать его ещё раз.", to=ctx.message)
                return
            except CaptionLayoutError as error:
                await ctx.reply(str(error), to=ctx.message)
                return
            if timed_out:
                if result is not None:
                    result.close()
                await ctx.reply("🤷🏻‍♂️ Обработка заняла слишком много времени. Попробуй файл поменьше.", to=ctx.message)
                return
            if result is None:
                await ctx.reply("🤷🏻‍♂️ Не удалось обработать видео" if video else "🤷🏻‍♂️ Не удалось обработать картинку", to=ctx.message)
                return
            keyboard = rate_keyboard() if ctx.message.chat.type != ChatType.PRIVATE else None
            if isinstance(result, io.BytesIO):
                with result:
                    await ctx.reply(
                        video=BufferedInputFile(result.getvalue(), filename=f"{style}.mp4"), reply_markup=keyboard, supports_streaming=True
                    )
            else:
                with closing(result), image_bytes_io(result, ext="png") as output:
                    await ctx.reply(photo=BufferedInputFile(output.getvalue(), filename=f"{style}.png"), reply_markup=keyboard)
