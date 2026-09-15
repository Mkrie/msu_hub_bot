from msu_hub_bot.settings import settings

import asyncio
import base64
from typing import Tuple

import aiohttp

from common.utils import download_content


async def anticaptcha(url: str) -> Tuple[str, int]:
    token = settings.require('anticaptcha_token')

    content = await download_content(url)
    if not content:
        raise ValueError('Can\'t download captcha')

    body = base64.b64encode(content).decode('utf-8')
    data = {
        'clientKey': token,
        'task': {
            'type': 'ImageToTextTask',
            'body': body,
            'minLength': 4,
            'maxLength': 4,
        },
    }

    async with aiohttp.ClientSession() as session:
        url = 'https://api.anti-captcha.com/createTask'
        async with session.post(url, json=data) as response:
            response.raise_for_status()
            result = await response.json()

        if 'taskId' not in result:
            raise ValueError(f'`{result}` does not contain `taskId`')

        await asyncio.sleep(5.)
        data = {
            'clientKey': token,
            'taskId': result['taskId'],
        }

        while True:
            url = 'https://api.anti-captcha.com/getTaskResult'
            async with session.post(url, json=data) as response:
                response.raise_for_status()
                captcha = await response.json()
                if captcha['status'] == 'ready':
                    return captcha['solution']['text'], result['taskId']
            await asyncio.sleep(1.)


async def anticaptcha_fail(task_id: int):
    token = settings.require('anticaptcha_token')

    data = {
        'clientKey': token,
        'taskId': task_id
    }

    async with aiohttp.ClientSession() as session:
        url = 'https://api.anti-captcha.com/reportIncorrectImageCaptcha'
        async with session.post(url, json=data) as response:
            return await response.json()
