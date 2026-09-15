from contextlib import suppress
from copy import copy

import aiogram
from PIL import Image
from aiogram.types import Message, InputMediaPhoto
from aiogram.utils.markdown import hbold

from common.externals.deep_ai import deep_dream
from common.tg.utils import download, extract_image
from common.utils import image_bytes_io


async def process_deepdream(message: Message):
    target, dest = await extract_image(message, with_profile_photo=True)
    file = await download(dest)
    if file is None:
        return True

    depth = 3
    file = image_bytes_io(Image.open(file), ext='png')

    def madness(level: int) -> str:
        return hbold('madness level') + f': {level} / {depth}'

    message = await target.reply_photo(copy(file), caption=madness(0))

    url = await deep_dream(file)
    message = await message.edit_media(InputMediaPhoto(url, caption=madness(1)))

    with suppress(aiogram.exceptions.BadRequest):
        for i in range(2, depth + 1):
            url = await deep_dream(url)
            message = await message.edit_media(InputMediaPhoto(url, caption=madness(i) if i != depth else ''))

    return message
