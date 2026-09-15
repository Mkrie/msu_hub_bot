from msu_hub_bot.settings import settings

import asyncio

import aiohttp
import bs4
from aiogram.types import Message, MediaGroup
from aiogram.utils.markdown import hitalic

from app import bot
from common.externals.anticaptcha import anticaptcha, anticaptcha_fail
from common.externals.exceptions import BadRequestError
from common.tg.filters import MetaInfo


async def process_rudalle(message: Message, meta: MetaInfo):
    settings.require("anticaptcha_token")
    retries_count = 2

    for retry in range(1, retries_count + 1):
        try:
            return await dalle(meta)
        except BadRequestError:
            await message.reply('🤷🏻‍♂️ Не удалось выполнить запрос')
            return
        except Exception:
            if retry == retries_count:
                await message.reply('🤷🏻‍♂️ Не удалось выполнить запрос')
                raise


async def process_dalle_emoji(message: Message, meta: MetaInfo):
    settings.require("anticaptcha_token")
    retries_count = 2

    for retry in range(1, retries_count + 1):
        try:
            return await dalle_emoji(meta)
        except BadRequestError:
            await message.reply('🤷🏻‍♂️ Не удалось выполнить запрос')
            return
        except Exception:
            if retry == retries_count:
                await message.reply('🤷🏻‍♂️ Не удалось выполнить запрос')
                raise




async def dalle(meta: MetaInfo):
    target, text = meta.extract_text()

    if not text:
        return True

    text = text[:300]
    reply = await target.reply(f'🔄 Ожидание... Запрос: {hitalic(text)}')

    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:94.0) Gecko/20100101 Firefox/94.0',
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        'referrer': 'https://rudalle.ru/demo',
    }

    async with aiohttp.ClientSession() as session:
        url = 'https://rudalle.ru/demo'
        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                await reply.delete()
                raise BadRequestError()
            html = await response.text()

        soup = bs4.BeautifulSoup(html, features='html.parser')
        form = soup.find('form', {'class': 'post-form'})
        if not form:
            await reply.delete()
            raise BadRequestError()
        csrfmiddlewaretoken = form.find('input', {'name': 'csrfmiddlewaretoken'}).get('value')
        captcha_0 = form.find('input', {'name': 'captcha_0'}).get('value')
        captcha_1, task_id = await anticaptcha(f'https://rudalle.ru/captcha/image/{captcha_0}/')

        data = aiohttp.formdata.FormData()
        data.add_field('csrfmiddlewaretoken', csrfmiddlewaretoken)
        data.add_field('text', text)
        data.add_field('captcha_0', captcha_0)
        data.add_field('captcha_1', captcha_1.upper())

        url = 'https://rudalle.ru/generate_image'
        async with session.post(url, headers=headers, data=data) as response:
            if response.status != 200:
                await reply.delete()
                raise BadRequestError()
            html = await response.text()

        soup = bs4.BeautifulSoup(html, features='html.parser')
        time_section = soup.find('section', {'class': 'wrapper2'}).find_all('p')
        if len(time_section) <= 2:
            await anticaptcha_fail(task_id)
            await reply.delete()
            raise ValueError('Captcha was solved incorrectly')
        time_left = time_section[2].text
        await reply.edit_text(f'🔄 {time_left}.. Запрос: {hitalic(text)}')
        url_part = soup.find('a', {'class': 'btn btn-blue'}).get('href')
        url = f'https://rudalle.ru{url_part}'

        while True:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    await reply.delete()
                    raise BadRequestError()
                html = await response.text()

            soup = bs4.BeautifulSoup(html, features='html.parser')
            image = soup.find('img', {'class': 'cardimage'})
            if image:
                image_url = image.get('src')
                break
            await asyncio.sleep(15.)

    await reply.delete()

    photos_urls = [
        image_url,
        image_url.replace('_00000', '_00001'),
        image_url.replace('_00000', '_00002'),
    ]

    media = MediaGroup()
    caption = f'🤖 Результат ruDALL-E на запрос: {hitalic(text)}'
    for url in photos_urls:
        media.attach_photo(url, caption=caption)
        caption = None

    return await bot.safe_send_media_group(chat_id=target.chat.id, media=media, reply_to_message_id=target.message_id)


async def dalle_emoji(meta: MetaInfo):
    target, text = meta.extract_text()

    if not text:
        return True

    text = text[:300]
    reply = await target.reply(f'🔄 Ожидание... Запрос: {hitalic(text)}')

    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:94.0) Gecko/20100101 Firefox/94.0',
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        'referrer': 'https://rudalle.ru/demo_emoji',
    }

    async with aiohttp.ClientSession() as session:
        url = 'https://rudalle.ru/demo_emoji'
        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                await reply.delete()
                raise BadRequestError()
            html = await response.text()

        soup = bs4.BeautifulSoup(html, features='html.parser')
        form = soup.find('form', {'class': 'post-form'})
        if not form:
            await reply.delete()
            raise BadRequestError()
        csrfmiddlewaretoken = form.find('input', {'name': 'csrfmiddlewaretoken'}).get('value')
        captcha_0 = form.find('input', {'name': 'captcha_0'}).get('value')
        captcha_1, task_id = await anticaptcha(f'https://rudalle.ru/captcha/image/{captcha_0}/')

        data = aiohttp.formdata.FormData()
        data.add_field('csrfmiddlewaretoken', csrfmiddlewaretoken)
        data.add_field('text', text)
        data.add_field('captcha_0', captcha_0)
        data.add_field('captcha_1', captcha_1.upper())

        url = 'https://rudalle.ru/generate_emoji'
        async with session.post(url, headers=headers, data=data) as response:
            if response.status != 200:
                await reply.delete()
                raise BadRequestError()
            html = await response.text()

        soup = bs4.BeautifulSoup(html, features='html.parser')
        time_section = soup.find('section', {'class': 'wrapper2'}).find_all('p')
        if len(time_section) <= 2:
            await anticaptcha_fail(task_id)
            await reply.delete()
            raise ValueError('Captcha was solved incorrectly')
        time_left = time_section[2].text
        await reply.edit_text(f'🔄 {time_left}.. Запрос: {hitalic(text)}')
        url_part = soup.find('a', {'class': 'btn btn-blue'}).get('href')
        url = f'https://rudalle.ru{url_part}'

        while True:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    await reply.delete()
                    raise BadRequestError()
                html = await response.text()

            soup = bs4.BeautifulSoup(html, features='html.parser')
            image = soup.find('img', {'class': 'cardimage'})
            if image:
                image_url = image.get('src')
                break
            await asyncio.sleep(15.)

    await reply.delete()

    media = MediaGroup()
    caption = f'🤖 Результат ruDALL-E на запрос: {hitalic(text)}'
    for url in [image_url]:
        media.attach_photo(url, caption=caption)
        caption = None

    return await bot.safe_send_media_group(chat_id=target.chat.id, media=media, reply_to_message_id=target.message_id)
