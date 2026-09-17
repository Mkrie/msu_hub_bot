"""Admit media work before downloading and transfer immutable worker inputs."""

import io
from collections.abc import Callable
from typing import Any, TypeVar

from aiogram import Bot

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.media.limits import MAX_DOWNLOAD_BYTES
from msu_hub_bot.telegram.files import DownloadableMedia, download

ResultT = TypeVar("ResultT")


class DownloadUnavailable(Exception):
    """Telegram could not provide the selected media."""


async def run_downloaded(
    executor: TPExecutor,
    media: DownloadableMedia,
    func: Callable[..., ResultT],
    *args: Any,
    bot: Bot | None = None,
    timeout: float | None = 180,
) -> tuple[ResultT | None, bool]:
    async def prepare() -> tuple[bytes]:
        stream = await download(media, bot, max_bytes=MAX_DOWNLOAD_BYTES)
        if stream is None:
            raise DownloadUnavailable
        with stream:
            return (stream.getvalue(),)

    def convert(payload: bytes) -> ResultT:
        with io.BytesIO(payload) as stream:
            return func(stream, *args)

    return await executor.run_prepared(prepare, convert, timeout=timeout)
