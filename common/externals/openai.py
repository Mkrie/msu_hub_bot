from msu_hub_bot.settings import settings

import aiohttp

from common.externals.exceptions import BadRequestError


async def completion(text: str) -> str:
    # Docs: https://beta.openai.com/docs/api-reference/completions

    headers = {
        'Authorization': settings.require('openai_authorization'),
    }

    data = {
        'model': 'text-davinci-002',
        'prompt': text,
        'max_tokens': 80,
        'temperature': 1.2,
    }

    async with aiohttp.ClientSession() as session:
        url = 'https://api.openai.com/v1/completions'
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json()

    return result['choices'][0]['text']


async def completion_chat(text: str) -> str:
    # Docs: https://platform.openai.com/docs/api-reference/completions

    headers = {
        'Authorization': settings.require('openai_chat_authorization'),
    }

    data = {
        # 'model': 'gpt-4',
        'model': 'gpt-3.5-turbo',
        'prompt': text,
        'max_tokens': 80,
        'temperature': 1.2,
    }

    async with aiohttp.ClientSession() as session:
        url = 'https://api.openai.com/v1/chat/completions'
        async with session.post(url, headers=headers, json=data) as response:
            response.raise_for_status()
            if response.status != 200:
                raise BadRequestError()
            result = await response.json()

    return result['choices'][0]['text']
