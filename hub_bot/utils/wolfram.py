from msu_hub_bot.settings import MissingIntegration

import random
from contextlib import suppress
from functools import cached_property
from io import BytesIO
from typing import Final, Tuple

import aiogram
import aiohttp
from PIL import Image
from aiogram.types import InputFile, InputMediaDocument, InputMediaPhoto, Message
from aiogram.utils.markdown import hcode

from common.utils import valid_filename, image_bytes_io


class WolframAPIError(Exception):
    pass


class WolframAPI:
    """
    Docs: https://products.wolframalpha.com/simple-api/documentation/
    """
    api_url = 'https://api.wolframalpha.com/v1/simple'

    def __init__(self, token: str):
        self.token = token

    @cached_property
    def session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession()

    async def close(self):
        await self.session.close()

    async def _request(self, **params) -> bytes:
        params = {
            'appid': self.token,
            'layout': 'labelbar',
            'width': 600,
            'units': 'metric',
            'timeout': 8,
            **params,
        }
        async with self.session.get(self.api_url, params=params) as response:
            if response.status == 200:
                result = await response.read()
                return result
            else:
                raise WolframAPIError(f'[{response.status}] {response.reason}')

    async def request(self, query: str) -> Tuple[BytesIO, float]:
        content = await self._request(i=query)

        def crop(img: Image, from_top: int = 0, from_bottom: int = 0) -> Image:
            box = (0, from_top, img.width, img.height - from_bottom)
            return img.crop(box)

        image = crop(Image.open(BytesIO(content)), from_top=75, from_bottom=45)
        return image_bytes_io(image, f'wolfram_{valid_filename(query)}', 'png'), image.height / image.width

    async def process_wolfram(self, message: Message):
        if not self.token:
            raise MissingIntegration("wolfram_token")
        animations_for_waiting: Final = (
            'https://giant.gfycat.com/PossibleGrouchyDeer.mp4',
            'https://giant.gfycat.com/EqualGargantuanKingbird.mp4',
            'https://giant.gfycat.com/CrispSlowAustrianpinscher.mp4',
            'https://giant.gfycat.com/TimelyRawApatosaur.mp4',
            'https://zippy.gfycat.com/WildCheapChicken.mp4',
            'https://zippy.gfycat.com/PopularBlackandwhiteGroundbeetle.mp4',
            'https://zippy.gfycat.com/NarrowEvenChinesecrocodilelizard.mp4',
            'https://zippy.gfycat.com/AmazingNecessaryGalapagosdove.mp4',
            'https://giant.gfycat.com/AggressiveRecentAntarcticfurseal.mp4',
        )

        query = message.get_args()
        if not query:
            if reply_to := message.reply_to_message:
                query = reply_to.text or reply_to.caption

        if not query:
            return await message.reply('Использование: ' + hcode('/wf sum 1/n^2, n=1..inf'))

        target = await message.reply_video(random.choice(animations_for_waiting), caption='🔄 Ожидание...')

        try:
            image, ratio = await self.request(query=query)
        except WolframAPIError:
            with suppress(aiogram.exceptions.MessageToDeleteNotFound):
                await target.delete()
            return await message.reply(f'🤷🏻‍♂️ По запросу {hcode(query)} ничего не найдено')

        with suppress(aiogram.exceptions.MessageToEditNotFound):
            if ratio > 2.1:
                return await target.edit_media(InputMediaDocument(InputFile(image)))

            return await target.edit_media(InputMediaPhoto(InputFile(image)))

        return True
