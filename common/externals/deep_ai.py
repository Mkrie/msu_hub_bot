import io
from typing import Union, Optional, Tuple

import aiohttp
import js2py

from common.externals.exceptions import BadRequestError

js_random = js2py.eval_js('function random() {return Math.round(1E11 * Math.random()) + ""}')
js_hash = js2py.eval_js(
    'function hash(){for(var a=[],b=0;64>b;)a[b]=0|4294967296*Math.sin(++b%Math.PI);return function(c)'
    '{var d,e,f,h=[d=1732584193,e=4023233417,~d,~e],k=[],g=unescape(encodeURI(c))+"\u0080",l=g.length;c=--l/4+2|15;'
    'for(k[--c]=8*l;~l;)k[l>>2]|=g.charCodeAt(l)<<8*l--;for(b=g=0;b<c;b+=16){for(l=h;64>g;l=[f=l[3],d+((f=l[0]+[d&e|~d&f,'
    'f&d|~f&e,d^e^f,e^(d|~f)][l=g>>4]+a[g]+~~k[b|[g,5*g+1,3*g+5,7*g][l]&15])<<(l=[7,12,17,22,5,9,14,20,4,11,16,23,6,10,15,21]'
    '[4*l+g++%4])|f>>>-l),d,e])d=l[1]|0,e=l[2];for(g=4;g;)h[--g]+=l[g]}for(c="";32>g;)c+=(h[g>>3]>>4*(1^g++)&15).toString(16);'
    'return c.split("").reverse().join("")}}'
)()


def api_key(user_agent: str) -> str:
    e = js_random()
    return f'tryit-{e}-{js_hash(user_agent + js_hash(user_agent + js_hash(user_agent + e)))}'


async def deep_dream(file: Union[str, io.BytesIO]) -> Optional[str]:
    if file is None:
        return None

    data = aiohttp.formdata.FormData()

    if isinstance(file, str):
        data.add_field('image', file)
    else:
        data.add_field('image', file, content_type='image/png', filename='file.png')

    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0'
    headers = {
        'User-Agent': user_agent,
        'referrer': 'https://deepai.org/machine-learning-model/deepdream',
        'client-library': 'deepai-js-client',
        'api-key': api_key(user_agent),
    }

    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.deepai.org/api/deepdream', headers=headers, data=data) as response:
            if response.status == 200:
                result = await response.json()
                return result.get('output_url')


async def super_resolution(file: Union[str, io.BytesIO]) -> str:
    data = aiohttp.formdata.FormData()

    if isinstance(file, str):
        data.add_field('image', file)
    else:
        data.add_field('image', file, content_type='image/jpeg', filename='file.jpeg')

    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0'
    headers = {
        'User-Agent': user_agent,
        'referrer': 'https://deepai.org/machine-learning-model/torch-srgan',
        'client-library': 'deepai-js-client',
        'api-key': api_key(user_agent),
    }

    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.deepai.org/api/torch-srgan', headers=headers, data=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json()

    return result['output_url']


async def waifu2x(file: Union[str, io.BytesIO]) -> str:
    data = aiohttp.formdata.FormData()

    if isinstance(file, str):
        data.add_field('image', file)
    else:
        data.add_field('image', file, content_type='image/jpeg', filename='file.jpeg')

    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0'
    headers = {
        'User-Agent': user_agent,
        'referrer': 'https://deepai.org/machine-learning-model/waifu2x',
        'client-library': 'deepai-js-client',
        'api-key': api_key(user_agent),
    }

    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.deepai.org/api/waifu2x', headers=headers, data=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json()

    return result['output_url']


async def colorizer(file: Union[str, io.BytesIO]) -> str:
    data = aiohttp.formdata.FormData()

    if isinstance(file, str):
        data.add_field('image', file)
    else:
        data.add_field('image', file, content_type='image/jpeg', filename='file.jpeg')

    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0'
    headers = {
        'User-Agent': user_agent,
        'referrer': 'https://deepai.org/machine-learning-model/colorizer',
        'client-library': 'deepai-js-client',
        'api-key': api_key(user_agent),
    }

    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.deepai.org/api/colorizer', headers=headers, data=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json()

    return result['output_url']


async def photo_description(file: Union[str, io.BytesIO]) -> Optional[str]:
    data = aiohttp.formdata.FormData()

    if isinstance(file, str):
        data.add_field('image', file)
    else:
        data.add_field('image', file, content_type='image/jpeg', filename='file.jpeg')

    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0'
    headers = {
        'User-Agent': user_agent,
        'referrer': 'https://deepai.org/machine-learning-model/neuraltalk',
        'client-library': 'deepai-js-client',
        'api-key': api_key(user_agent),
    }

    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.deepai.org/api/neuraltalk', headers=headers, data=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json()

    return result['output']


async def nudity_detection(file: Union[str, io.BytesIO]) -> Optional[Tuple[str, float, dict]]:
    data = aiohttp.formdata.FormData()

    if isinstance(file, str):
        data.add_field('image', file)
    else:
        data.add_field('image', file, content_type='image/jpeg', filename='file.jpeg')

    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0'
    headers = {
        'User-Agent': user_agent,
        'referrer': 'https://deepai.org/machine-learning-model/nsfw-detector',
        'client-library': 'deepai-js-client',
        'api-key': api_key(user_agent),
    }

    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.deepai.org/api/nsfw-detector', headers=headers, data=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json()

        async with session.get(f'https://api.deepai.org/get_standard_api_result_data/{result["id"]}') as response:
            if response.status != 200:
                raise BadRequestError()
            result_data = await response.json()
            scale = result_data['result_data']['scale_applied']

    return f'https://api.deepai.org/job-view-file/{result["id"]}/inputs/image.jpg', scale, result['output']


async def demographic_recognition(file: Union[str, io.BytesIO]) -> Optional[Tuple[str, float, dict]]:
    data = aiohttp.formdata.FormData()

    if isinstance(file, str):
        data.add_field('image', file)
    else:
        data.add_field('image', file, content_type='image/jpeg', filename='file.jpeg')

    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0'
    headers = {
        'User-Agent': user_agent,
        'referrer': 'https://deepai.org/machine-learning-model/demographic-recognition',
        'client-library': 'deepai-js-client',
        'api-key': api_key(user_agent),
    }

    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.deepai.org/api/demographic-recognition', headers=headers, data=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json()

        async with session.get(f'https://api.deepai.org/get_standard_api_result_data/{result["id"]}') as response:
            if response.status != 200:
                raise BadRequestError()
            result_data = await response.json()
            scale = result_data['result_data']['scale_applied']

    return f'https://api.deepai.org/job-view-file/{result["id"]}/inputs/image.jpg', scale, result['output']


async def densecap(file: Union[str, io.BytesIO]) -> Optional[Tuple[str, float, dict]]:
    data = aiohttp.formdata.FormData()

    if isinstance(file, str):
        data.add_field('image', file)
    else:
        data.add_field('image', file, content_type='image/jpeg', filename='file.jpeg')

    user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0'
    headers = {
        'User-Agent': user_agent,
        'referrer': 'https://deepai.org/machine-learning-model/densecap',
        'client-library': 'deepai-js-client',
        'api-key': api_key(user_agent),
    }

    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.deepai.org/api/densecap', headers=headers, data=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json()

        async with session.get(f'https://api.deepai.org/get_standard_api_result_data/{result["id"]}') as response:
            if response.status != 200:
                raise BadRequestError()
            result_data = await response.json()
            scale = result_data['result_data']['scale_applied']

    return f'https://api.deepai.org/job-view-file/{result["id"]}/inputs/image.jpg', scale, result['output']
