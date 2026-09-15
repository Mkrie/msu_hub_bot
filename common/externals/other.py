from msu_hub_bot.settings import settings

import asyncio
import base64
import io
import random
import re
from typing import List, Tuple, Union

import aiohttp
import httpx

from common import json
from common.externals.exceptions import BadRequestError, CantFindFaceError, BadExpressionError
from common.utils import bytes_io, retry_async_


async def mask_toon(file: io.BytesIO) -> io.BytesIO:
    data = aiohttp.formdata.FormData()
    data.add_field('image', file, content_type='image/jpeg', filename='file.jpg')

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0',
    }

    async with aiohttp.ClientSession() as session:
        url = 'https://nm3prlo5jff---hellotoon-zqdxg7cmaa-ue.a.run.app/'
        async with session.post(url, headers=headers, data=data) as response:
            if response.status != 200:
                raise BadRequestError()
            html = await response.read()

    result = re.findall(b'<div class="col"><img src="data:image/jpeg;base64,(.*)" class="img-fluid" /></div>', html)
    if len(result) != 2:
        raise CantFindFaceError()

    return bytes_io(base64.b64decode(result[1]))


async def agile_gan(file: io.BytesIO) -> List[io.BytesIO]:
    data = aiohttp.formdata.FormData()
    data.add_field('myFile', file, content_type='image/jpeg', filename='file.jpg')

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0',
        'referrer': 'http://www.agilegan.com/',
    }

    async with aiohttp.ClientSession() as session:
        url = 'http://www.agilegan.com/'
        async with session.post(url, headers=headers, data=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = json.loads(await response.read())

    if not result['noface']:
        raise CantFindFaceError()

    return [bytes_io(base64.b64decode(result[s])) for s in ('cartoon_img_decode',
                                                            'oil_painting_img_decode',
                                                            'comic_img_decode',
                                                            'celebrity_img_decode')]


async def mask_anime(file: io.BytesIO) -> io.BytesIO:
    data = {
        'data': ['data:image/jpeg;base64,' + base64.b64encode(file.read()).decode(encoding='utf-8'), False],
    }

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Accept-Language': 'ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3',
        'referrer': 'https://gradio.app/g/AK391/GANsNRoses',
        'X-Requested-With': 'XMLHttpRequest',
    }

    async with aiohttp.ClientSession() as session:
        url = 'https://gradio.app/hub_api/AK391/GANsNRoses/api/predict/'
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = json.loads(await response.read())

    return bytes_io(base64.b64decode(result['data'][0][0][len('data:image/png;base64,'):]))


async def mask_zombie(file: io.BytesIO) -> io.BytesIO:
    data = aiohttp.formdata.FormData()
    data.add_field('image', file, content_type='image/jpeg', filename='file.jpg')

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0',
        'referrer': 'https://makemeazombie.com/',
        'X-Requested-With': 'XMLHttpRequest',
    }

    async with aiohttp.ClientSession() as session:
        url = 'https://deepgrave-image-processor-no7pxf7mmq-uc.a.run.app/transform'
        async with session.post(url, headers=headers, data=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.read()

    if result == b'No face found':
        raise CantFindFaceError()

    return bytes_io(base64.b64decode(result))


async def random_tyan() -> io.BytesIO:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:86.0) Gecko/20100101 Firefox/86.0',
        'referrer': 'https://thisanimedoesnotexist.ai/',
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        number = ''.join(random.choices('0123456789', k=5))
        url = f'https://thisanimedoesnotexist.ai/results/psi-1.0/seed{number}.png'
        async with session.get(url) as response:
            if response.status != 200:
                raise BadRequestError()
            img = await response.read()

    return bytes_io(img, f'{number}.png')


async def duckduckgo(query: str) -> dict:
    headers = {
        'Accept-Language': 'ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3',
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        url = f'https://api.duckduckgo.com/'
        async with session.get(url, params=dict(q=query, format='json', no_redirect=1, t='https://t.me/msu_hub_bot')) as response:
            if response.status != 200:
                raise BadRequestError()
            result = json.loads(await response.read())

    return result


async def bored() -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get('https://www.boredapi.com/api/activity', ssl=None) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json(loads=json.loads)

    return result


async def remove_bg(file: io.BytesIO) -> str:
    data = aiohttp.formdata.FormData()
    data.add_field('source_image_file', file, content_type='image/jpeg', filename='bg.jpg')

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0',
    }

    async with aiohttp.ClientSession() as session:
        async with session.get('https://www.slazzer.com/upload') as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.text()
            csrf = re.findall(r'<meta name="csrf-token" content="(\S+)">', result)[0]

        headers['X-CSRFToken'] = csrf
        headers['X-Requested-With'] = 'XMLHttpRequest'
        headers['Referer'] = 'https://www.slazzer.com/upload'

        async with session.post('https://www.slazzer.com/upload_image', headers=headers, data=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.read()

    result = json.loads(result)
    return 'https://slazzer.com' + result['preview_size_output_image']


async def remove_bg_api(file: Union[io.BytesIO, str]) -> io.BytesIO:
    data = aiohttp.formdata.FormData()
    if isinstance(file, str):
        data.add_field('source_image_url', file)
    else:
        data.add_field('source_image_file', file, content_type='image/jpeg', filename='bg.jpg')

    data.add_field('crop', 'true')
    data.add_field('preview', 'true')

    headers = {
        'API-KEY': settings.require('remove_bg_api_key'),
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post('https://api.slazzer.com/v2.0/remove_image_background', data=data) as response:
            # if response.status != 200:
            #     raise BadRequestError()
            print(response.status, response.reason, await response.text())
            response.raise_for_status()
            result = await response.read()

    return bytes_io(result, filename='removed_bg.png')


async def imgur_upload(file: io.BytesIO, image_or_video: str = 'image') -> dict:
    # Docs: https://apidocs.imgur.com/#c85c9dfc-7487-4de2-9ecd-66f727cf3139

    data = aiohttp.formdata.FormData()
    data.add_field(image_or_video, file)

    async with aiohttp.ClientSession(headers={'Authorization': settings.require('imgur_authorization')}) as session:
        async with session.post('https://api.imgur.com/3/upload', data=data) as response:
            result = json.loads(await response.read())['data']

        while result.get('processing', {}).get('status') in ('pending', 'started'):
            await asyncio.sleep(1)

            async with session.get('https://api.imgur.com/3/image/' + result['id']) as response:
                result = json.loads(await response.read())['data']

    return result


async def badwiki(text: str) -> str:
    data = aiohttp.formdata.FormData()
    data.add_field('text_in', text)

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0',
    }

    async with aiohttp.ClientSession() as session:
        url = 'https://badwiki.textgen.cloud/'
        async with session.post(url, headers=headers, data=data) as response:
            if response.status != 200:
                raise BadRequestError()
            return await response.text()


@retry_async_(retries_count=3, sleep_for=5.)
async def gpt3(text: str) -> str:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:95.0) Gecko/20100101 Firefox/95.0',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.5',
        'Referer': 'https://russiannlp.github.io/',
        'Origin': 'https://russiannlp.github.io',
    }
    data = {
        'text': text,
    }

    async with aiohttp.ClientSession(headers=headers, json_serialize=json.dumps) as session:
        url = 'https://api.aicloud.sbercloud.ru/public/v1/public_inference/gpt3/predict'

        async with session.post(url, json=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = json.loads(await response.read())

    return result['predictions']


async def gptn(text: str) -> str:
    async with aiohttp.ClientSession(json_serialize=json.dumps) as session:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:99.0) Gecko/20100101 Firefox/99.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "referrer": "https://news.ycombinator.com/",
        }
        url = 'https://textsynth.com/playground.html'

        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.text(encoding='utf-8')
            api_key = re.search(r'var textsynth_api_key = "(.*?)"', result).group(1)

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:99.0) Gecko/20100101 Firefox/99.0",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.5",
            "Authorization": f"Bearer {api_key}",
        }
        data = {
            "prompt": text,
            "temperature": 1,
            "top_k": 40,
            "top_p": 0.9,
            "max_tokens": 200,
            "stream": False,
            "stop": None,
        }
        url = 'https://api.textsynth.com/v1/engines/gptneox_20B/completions'

        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = json.loads(await response.read())

        return text + result['text']


async def balaboba(text: str, style_id: int = 0) -> Tuple[str, str]:
    data = {
        'query': text,
        'intro': style_id,
        'filter': 1,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3",
        "Referer": "https://yandex.ru/",
        "Content-Type": "application/json",
        "Origin": "https://yandex.ru",
        "DNT": "1",
        "Connection": "keep-alive",
        "Accept-Encoding": "gzip, deflate, br",
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), verify=False) as session:
        url = 'https://zeapi.yandex.net/lab/api/yalm/text3'
        response = await session.post(url, headers=headers, json=data)
        if response.status_code != 200:
            raise BadRequestError()
        result = json.loads(response.read())

    if result.get('bad_query'):
        raise BadExpressionError()

    if result.get('error'):
        raise BadRequestError()

    return result['query'], result['text']


async def porfirevich(text: str) -> str:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0',
    }
    data = {
        'prompt': text, 'length': 60,
    }

    async with aiohttp.ClientSession() as session:
        url = 'https://pelevin.gpt.dobro.ai/generate/'
        async with session.post(url, headers=headers, data=json.dumps(data)) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json()

    return result['replies'][-1]


async def cheatsheet(lang: str, query: str) -> str:
    headers = {
        'User-Agent': 'curl',
    }

    async with aiohttp.ClientSession() as session:
        url = f'https://cht.sh/{lang}' + (f'/{query.replace(" ", "+")}' if query else '')
        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.text()

    result = re.sub(r'\u001b[\[;\d]+m', '', result)
    return result
