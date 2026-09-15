from msu_hub_bot.settings import settings

from functools import cached_property
from typing import Optional, AnyStr

import aiohttp
from pydantic import validator, ValidationError
from pydantic.main import BaseModel
from transliterate import translit

from common import json
from common.mixins import LoggerMixin


class PyNativeResponseStatus(BaseModel):
    id: int
    description: str


class PyNativeResponse(BaseModel):
    stdout: Optional[str]
    stderr: Optional[str]

    @validator('stdout', 'stderr', pre=True)
    def cut(cls, v):
        return v and '\n'.join(v.split('\n')[:100])[:2048]

    compile_output: Optional[str]
    message: Optional[str]

    token: Optional[str]
    time: Optional[str]
    memory: Optional[str]
    status: Optional[PyNativeResponseStatus]

    class Config:
        anystr_strip_whitespace = True


class PyNativeError(Exception):
    def __init__(self, code: int, reason: AnyStr):
        self.code = code
        self.reason = reason

    def __repr__(self):
        return f'[{self.code}] {self.reason}'


class PyNative(LoggerMixin):
    api_base = 'https://pynative.com/'

    @cached_property
    def session(self) -> aiohttp.ClientSession:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3',
            'Referer': 'https://pynative.com/online-python-code-editor-to-execute-python-code/',
            'Origin': 'https://pynative.com/',
            'X-Requested-With': 'XMLHttpRequest',
            'Cookie': settings.require('pynative_cookie'),
        }
        return aiohttp.ClientSession(headers=headers)

    async def close(self):
        return await self.session.close()

    async def _request(self, endpoint: str, method: str = 'POST', headers: dict = None, data=None, **params) -> bytes:
        async with self.session.request(method, self.api_base + endpoint, headers=headers, data=data, params=params) as response:
            if response.status != 200:
                raise PyNativeError(response.status, response.reason)
            return await response.read()

    async def request(self, source_code: str, stdin: str = '') -> PyNativeResponse:
        source_code, stdin = translit(source_code, 'ru', reversed=True), translit(stdin, 'ru', reversed=True)

        data = aiohttp.formdata.FormData()
        data.add_field('data', json.dumps({'source_code': source_code, 'language_id': 10, 'stdin': stdin}))
        result = await self._request('editor.php', data=data, base64_encoded='true')
        try:
            return PyNativeResponse.parse_raw(result)
        except ValidationError:
            raise PyNativeError(200, result)

    async def request_and_parse(self, source_code: str, stdin: str = ''):
        r = await self.request(source_code, stdin)

        if r.status.id == 2:
            return '🤷🏻‍♂️ Timeout'

        text = ''

        if r.stdout:
            text += r.stdout + '\n\n'

        if r.stderr:
            text += r.stderr + '\n\n'

        if r.message:
            text += r.message + '\n\n'

        if r.time and r.memory:
            text += f'Executed in: {r.time} secs\nMemory: {r.memory} KB'

        return text
