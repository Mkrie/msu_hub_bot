import asyncio
import random
from collections import defaultdict
from functools import cached_property, partial
from typing import AnyStr

import aiohttp
from aiogram.utils.markdown import hbold, hitalic, hlink, hunderline

from common import json
from common.mixins import LoggerMixin
from common.utils import clear_html


class OrfogrammkaError(Exception):
    pass


class OrfogrammkaAPIError(Exception):
    def __init__(self, code: int, reason: AnyStr):
        self.code = code
        self.reason = reason

    def __repr__(self):
        return f'[{self.code}] {self.reason}'


class Orfogrammka(LoggerMixin):
    api_base = 'https://orfogrammka.ru/'

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password

    @cached_property
    def session(self) -> aiohttp.ClientSession:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:82.0) Gecko/20100101 Firefox/82.0',
            'X-Requested-With': 'XMLHttpRequest',
            'referrer': f'{self.api_base}cabinet/',
        }
        return aiohttp.ClientSession(headers=headers)

    async def close(self):
        return await self.session.close()

    async def _request(self, endpoint: str, **params) -> dict:
        data = aiohttp.FormData(quote_fields=False, charset='UTF-8')
        data.add_fields(*params.items())

        async with self.session.post(self.api_base + endpoint, data=data) as response:
            if response.status == 401:
                await self.login()
                return await self._request(endpoint, **params)
            if response.status != 200:
                raise OrfogrammkaAPIError(response.status, response.reason)
            result = json.loads(await response.read())
            if result.get('status') != 'OK':
                raise OrfogrammkaError('Request Error', result)

        return result['resp']

    async def login(self) -> dict:
        return await self._request('cabinet/ajax/auth.jsp', action='LOGIN', email=self.email, password=self.password)

    async def submit(self, text: str, profile: str) -> dict:
        request = partial(self._request, 'cabinet/ajax/documents.jsp')

        result = await request(action='PASTE', title=text[:31] + '...', html=text.replace('\n', '<br>'), text=text, profile=profile)

        uid = result['_id']
        result = await request(action='CHECK_DOC_STATE', document=uid)

        if result['state'] == 'ESTIMATED_SUCCESS':
            await request(action='START_CHECK', document=uid, profile=profile)

            while True:
                await asyncio.sleep(1)
                result = await request(action='CHECK_DOC_STATE', document=uid)
                if result['state'] != 'CHECKING':
                    break

        result = await request(action='ANNOTATED', document=uid)
        return result['annotations']

    async def submit_common(self, text: str) -> dict:
        return await self.submit(text, 'COMMON')

    async def submit_cicero(self, text: str) -> dict:
        return await self.submit(text, 'CICERO')

    async def submit_quality(self, text: str) -> dict:
        return await self.submit(text, 'QUALITY')

    @staticmethod
    def humanize(result: dict) -> str:
        annotations_by_type = defaultdict(list)
        for a in result['annotations']:
            annotations_by_type[a['kind']].append(a)

        text = ''
        for kind in result['kindsOrder']:
            annotation_type = result['kinds'][kind]
            annotations = annotations_by_type[kind]

            text += f'{hbold(annotation_type.capitalize())}:\n'

            texts = {}
            for a in annotations:
                t = '— ' + hitalic(a['selection'])
                if description := a.get('description'):
                    t += f': {description.strip().replace("<br>", " ")}'
                if suggestion := a.get('suggestion'):
                    t += f', {hunderline("совет")}: {clear_html(suggestion).strip().replace("<br>", " ")}'
                if rule := a.get('rule'):
                    t += f' (' + hlink('?', f'https://orfogrammka.ru/OGL/{rule}.html') + ')'
                texts[t] = True

            text += '\n'.join(texts) + '\n\n'

        water = result['water']['content'] * 100
        text += hbold('Процент «воды»') + f': {water:.2f}%\n\n'

        emoji = random.choice(('👩🏻‍🎓', '🧑🏻‍🎓', '👨🏻‍🎓'))
        text += f'{emoji} Проверено ' + hlink('Орфограммкой', 'https://orfogrammka.ru/?campaign=tpsjcbvj&page=home')
        return text
