"""Explicit ownership of Telegram downloads and multipart uploads."""

import io
from collections.abc import Buffer
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    Animation,
    Audio,
    BufferedInputFile,
    Document,
    FSInputFile,
    InputFile,
    PhotoSize,
    Sticker,
    Video,
    VideoNote,
    Voice,
)

from msu_hub_bot.telegram.context import bot_for

DownloadableMedia = Animation | Audio | Document | PhotoSize | Sticker | Video | VideoNote | Voice


class DownloadTooLarge(ValueError):
    """The actual Telegram download exceeds the caller's byte budget."""


class _BoundedDownload(io.BytesIO):
    def __init__(self, max_bytes: int) -> None:
        super().__init__()
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self.max_bytes = max_bytes

    def write(self, buffer: Buffer, /) -> int:
        with memoryview(buffer) as view:
            if self.tell() + view.nbytes > self.max_bytes:
                raise DownloadTooLarge("Telegram download exceeded its byte limit")
        return super().write(buffer)


def input_file(value: bytes | io.BytesIO | Path | str, filename: str | None = None) -> InputFile:
    """Snapshot in-memory content; leave local-file lifetime with the caller."""
    if isinstance(value, io.BytesIO):
        return BufferedInputFile(value.getvalue(), filename or "file")
    if isinstance(value, bytes):
        return BufferedInputFile(value, filename or "file")
    return FSInputFile(value, filename=filename)


async def download(media: DownloadableMedia | None, bot: Bot | None = None, *, max_bytes: int | None = None) -> io.BytesIO | None:
    if media is None:
        return None
    if max_bytes is not None and (media.file_size or 0) > max_bytes:
        raise DownloadTooLarge("Telegram download exceeded its byte limit")
    return await download_by_file_id(media.file_id, bot or bot_for(media), max_bytes=max_bytes)


async def download_by_file_id(file_id: str, bot: Bot, *, max_bytes: int | None = None) -> io.BytesIO | None:
    destination = io.BytesIO() if max_bytes is None else _BoundedDownload(max_bytes)
    try:
        await bot.download(file_id, destination=destination)
    except TelegramBadRequest:
        destination.close()
        return None
    except BaseException:
        destination.close()
        raise
    destination.seek(0)
    return destination


async def download_text(file_id: str, bot: Bot) -> str | None:
    stream = await download_by_file_id(file_id, bot)
    if stream is None:
        return None
    with stream:
        return stream.read().decode("utf-8")
