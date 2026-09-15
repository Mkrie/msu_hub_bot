import asyncio
import random
from itertools import cycle
from operator import itemgetter
from textwrap import shorten, dedent
from urllib import parse

from PIL import Image, ImageDraw, ImageFont
from aiogram.types import ChatActions, Message, ContentType, InputFile, MediaGroup, MessageEntityType
from aiogram.utils.markdown import hitalic, quote_html, hbold, hpre, hlink, hcode
from transliterate import translit
from yarl import URL

from app import orfogrammka, cpu_executor
from common.constants import TELEGRAM_MESSAGE_MAX_LEN
from common.externals.deep_ai import super_resolution, waifu2x, photo_description, nudity_detection, demographic_recognition, densecap, colorizer
from common.externals.exceptions import ExternalServiceError
from common.externals.fakeyou import fake_you, Voices
from common.externals.huggingface import mask_anime2, mask_drag, mask_arcane, mask_yolo, mask_doll, mask_vintage, mask_privacy, mask_arcane_video, mask_paint, \
    mask_inter, hg_copilot, hg_dalle, hg_latent_diffusion, mask_inpaint, hg_stable_diffusion
from common.externals.lingvanex import translate
from common.externals.moe import which_anime
from common.externals.openai import completion
from common.externals.orfogrammka import OrfogrammkaError
from common.externals.other import mask_zombie, mask_toon, remove_bg, badwiki, porfirevich, cheatsheet, imgur_upload, duckduckgo, gpt3, agile_gan, bored, gptn
from common.externals.topdf import convert_to_pdf
from common.externals.urbandictionary import urban_dictionary
from common.tg.chat_actioner import ChatActioner
from common.tg.filters import MetaInfo
from common.tg.middlewares.haiku import rate_keyboard
from common.tg.utils import download, extract_image, action_by_type, send_super_reply
from common.utils import one_liner, image_bytes_io, prettify_bytes, cut_long_text, download_image, cut_left_half, download_content, FakeBytesIO, grid_images, \
    is_en, is_ru
from resources import ubuntu_mono_font
from utils.ffmpeg import to_ogg_opus, ffmpeg2, ffmpeg


async def process_external(message: Message, function, output_type: str = ContentType.PHOTO, handler=lambda x: x, async_handler=None):
    target, dest = await extract_image(message, with_profile_photo=True)
    file = await download(dest)
    if file is None:
        return True

    try:
        async with ChatActioner(message.chat, action_by_type(output_type)):
            result = await function(file)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    result = handler(result)
    if async_handler:
        result = await async_handler(result)

    if output_type == ContentType.TEXT:
        return await target.reply(result)

    if output_type == ContentType.DOCUMENT:
        return await target.reply_document(result)

    if output_type == ContentType.VIDEO:
        return await target.reply_video(result)

    if output_type == ContentType.ANIMATION:
        return await target.reply_animation(result)

    if output_type == 'list[photo]':
        media = MediaGroup()
        for b_io in result:
            media.attach_photo(b_io)
        return await target.reply_media_group(media)

    return await target.reply_photo(result)


async def process_toonify(message: Message):
    return await process_external(message, mask_toon)


async def process_anime(message: Message):
    return await process_external(message, mask_anime2)


async def process_paint(message: Message):
    async def handler(file):
        file, timeouted = await cpu_executor.run(ffmpeg, file, ['-f', 'mp4'], '.mp4')
        if timeouted:
            return await message.reply('🤷🏻‍♂️ Timeout')
        if file is None:
            return await message.reply('🤷🏻‍♂️ Что-то пошло не так')
        return file

    return await process_external(message, mask_paint, output_type=ContentType.VIDEO, async_handler=handler)


async def process_inter(message: Message, meta: MetaInfo):
    target_1, image_1, target_2, image_2 = await meta.extract_two_images()

    file_1, file_2 = await asyncio.gather(download(image_1), download(image_2))
    if not (file_1 and file_2):
        return await message.reply(hitalic(f'🤷🏻‍♂️ Нужно два изображения'))

    async with ChatActioner(message.chat, action_by_type(ContentType.VIDEO)):
        try:
            result = await mask_inter(file_1, file_2)
        except ExternalServiceError as e:
            return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

        return await target_2.reply_video(result)


async def process_drag(message: Message):
    return await process_external(message, mask_drag, handler=cut_left_half)


async def process_doll(message: Message):
    return await process_external(message, mask_doll, handler=cut_left_half)


async def process_vintage(message: Message):
    return await process_external(message, mask_vintage, handler=cut_left_half)


async def process_inpaint(message: Message):
    return await process_external(message, mask_inpaint)


async def process_arcane(message: Message, meta: MetaInfo):
    target, dest = await meta.extract_video()
    if not dest:
        return await process_external(message, mask_arcane)

    try:
        async with ChatActioner(message.chat, ChatActions.UPLOAD_VIDEO_NOTE if target.content_type == ContentType.VIDEO_NOTE else ChatActions.UPLOAD_VIDEO):
            file = await download(dest)
            if file is None:
                return True

            if target.content_type in (ContentType.ANIMATION, ContentType.STICKER):
                # Add empty audio stream since it's required 😱
                file, timeouted = await cpu_executor.run(ffmpeg2, file,
                                                         ['-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100'],
                                                         ['-c:v', 'copy', '-c:a', 'aac', '-shortest'], '.mp4')
                if timeouted:
                    return await message.reply('🤷🏻‍♂️ Timeout')
                if file is None:
                    return await message.reply('🤷🏻‍♂️ Что-то пошло не так')

            result = await mask_arcane_video(file, min(getattr(dest, 'duration', 3), 10))
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    if target.content_type == ContentType.VIDEO_NOTE:
        return await target.reply_video_note(result)

    return await target.reply_video(result)


async def process_privacy(message: Message):
    return await process_external(message, mask_privacy)


async def process_what_hg(message: Message):
    return await process_external(message, mask_yolo)


async def process_agile_gan(message: Message):
    return await process_external(message, agile_gan, output_type='list[photo]')


async def process_dalle(message: Message, meta: MetaInfo):
    target, text = meta.extract_text()

    if not text:
        return True

    text = text[:300]
    reply = await target.reply(f'🔄 Ожидание... Запрос: {hitalic(text)}')

    try:
        result = await hg_dalle(text)
    except ExternalServiceError as e:
        return await reply.edit_text(hitalic(f'🤷🏻‍♂️ {e.text}'))

    await reply.delete()

    caption = f'🤖 Результат DALL·E mini на запрос: {hitalic(text)}'
    return await target.reply_photo(grid_images(result), caption=caption)


async def process_stable_diffusion(message: Message, meta: MetaInfo):
    target, text = meta.extract_text()

    if not text:
        return True

    text = text[:300]
    reply = await target.reply(f'🔄 Ожидание... Запрос: {hitalic(text)}')

    try:
        result = None
        i = 0
        while result is None and i < 5:
            result = await hg_stable_diffusion(text)
            i += 1
    except ExternalServiceError as e:
        return await reply.edit_text(hitalic(f'🤷🏻‍♂️ {e.text}'))

    if result is None:
        return await reply.edit_text('🤷🏻‍♂️ Что-то пошло не так, попробуйте позже...')

    await reply.delete()

    caption = f'🤖 Результат Stable Diffusion на запрос: {hitalic(text)}'
    return await target.reply_photo(grid_images(result), caption=caption)


async def process_latent_diffusion(message: Message, meta: MetaInfo):
    target, text = meta.extract_text()

    if not text:
        return True

    text = text[:300]
    reply = await target.reply(f'🔄 Ожидание... Запрос: {hitalic(text)}')

    try:
        result = await hg_latent_diffusion(text)
    except ExternalServiceError as e:
        return await reply.edit_text(hitalic(f'🤷🏻‍♂️ {e.text}'))

    await reply.delete()

    caption = f'🤖 Результат Latent Diffusion на запрос: {hitalic(text)}'
    return await target.reply_photo(result, caption=caption)


async def process_zombie(message: Message):
    return await process_external(message, mask_zombie, handler=cut_left_half)


async def process_tyan(message: Message):
    target = message.reply_to_message or message

    async with ChatActioner(message.chat, action_by_type(ContentType.PHOTO)):
        number = ''.join(random.choices('0123456789', k=5))
        url = f'https://thisanimedoesnotexist.ai/results/psi-1.0/seed{number}.png'
        return await target.reply_photo(url, caption=hitalic(f'Рандомная тян №{number}'), reply_markup=rate_keyboard())


async def process_which_anime(message: Message):
    target, dest = await extract_image(message, with_profile_photo=True)
    file = await download(dest)
    if file is None:
        return True

    async with ChatActioner(message.chat, action_by_type(ContentType.TEXT)):
        try:
            result = await which_anime(file)
        except ExternalServiceError as e:
            return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

        caption = ''
        files = []
        for anime in result['result'][:3]:
            link = hlink('Anilist', f'https://anilist.co/anime/{anime["anilist"]}')
            caption += f'— {hcode(anime["filename"])}, {link}, похожесть: {float(anime["similarity"]):.2}\n\n'
            files.append(anime['video'])

        media = MediaGroup()
        for file in files:
            media.attach_video(InputFile.from_url(file, filename=URL(file).name), caption=caption)
            caption = ''

        return await target.reply_media_group(media)


async def process_colorizer(message: Message):
    return await process_external(message, colorizer, output_type=ContentType.PHOTO)


async def process_bg(message: Message):
    return await process_external(message, remove_bg, output_type=ContentType.DOCUMENT)


async def process_srgan(message: Message):
    return await process_external(message, super_resolution, output_type=ContentType.DOCUMENT)


async def process_waifu(message: Message):
    return await process_external(message, waifu2x, output_type=ContentType.DOCUMENT)


async def process_what(message: Message):
    return await process_external(message, photo_description, output_type=ContentType.TEXT, handler=lambda t: hitalic(t.capitalize()))


# https://coolors.co/f72585-7209b7-3a0ca3-4361ee-4cc9f0
colors = ((247, 37, 133), (114, 9, 183), (58, 12, 163), (67, 97, 238), (76, 201, 240))
# https://coolors.co/ef476f-ffd166-06d6a0-118ab2-073b4c
colors_2 = ((239, 71, 111), (255, 209, 102), (6, 214, 160), (17, 138, 178), (7, 59, 76))
font = ImageFont.truetype(str(ubuntu_mono_font), 22)


async def process_nudes(message: Message):
    target, dest = await extract_image(message, with_profile_photo=True)
    file = await download(dest)
    if file is None:
        return True

    try:
        async with ChatActioner(message.chat, action_by_type(ContentType.PHOTO)):
            photo_url, scale, result = await nudity_detection(file)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    if not result.get('detections'):
        return await target.reply_photo(photo_url, caption=f'😒 Нудесы не найдены, nsfw-фактор: {hbold(str(result["nsfw_score"])[:5])}')

    image: Image.Image = await download_image(photo_url)
    draw = ImageDraw.Draw(image)
    text = f'Nsfw-фактор: {hbold(str(result["nsfw_score"])[:5])} 😏\n\n'
    colors_it = cycle(colors)

    for d in sorted(result['detections'], key=itemgetter('confidence'), reverse=True):
        (x, y, d0, d1), name, confidence = d['bounding_box'], d['name'], d['confidence']
        box = tuple(int(p * scale) for p in (x, y, x + d0, y + d1))
        name, _, sub = name.partition(' - ')
        color = next(colors_it)

        draw.rounded_rectangle(box, radius=5, outline=color, width=3)
        draw.text(((box[0] + box[2]) // 2, box[1] + 5), name, fill='white', stroke_width=5, stroke_fill=color, align='center', anchor='ms', font=font)

        if sub:
            name += f' ({sub.lower()})'
        text += f'— {hitalic(name)}, точность: {hbold(confidence)}\n'

    return await target.reply_photo(image_bytes_io(image), caption=text)


async def process_demographic(message: Message):
    target, dest = await extract_image(message, with_profile_photo=True)
    file = await download(dest)
    if file is None:
        return True

    try:
        async with ChatActioner(message.chat, action_by_type(ContentType.PHOTO)):
            photo_url, scale, result = await demographic_recognition(file)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    if not result.get('faces'):
        return await target.reply_photo(photo_url, caption=f'😒 Лица не найдены')

    image: Image.Image = await download_image(photo_url)
    draw = ImageDraw.Draw(image)
    colors_it = cycle(reversed(colors))

    for d in result['faces']:
        (x, y, d0, d1), gender, culture, (age_1, age_2) = d['bounding_box'], d['gender'], d['cultural_appearance'], d['age_range']
        box = tuple(int(p * scale) for p in (x, y, x + d0, y + d1))
        text = f'{culture} {gender}, {age_1}-{age_2}'
        color = next(colors_it)

        draw.rounded_rectangle(box, radius=5, outline=color, width=3)
        draw.text(((box[0] + box[2]) // 2, box[1] + 5), text, fill='white', stroke_width=5, stroke_fill=color, align='center', anchor='ms', font=font)

    return await target.reply_photo(image_bytes_io(image))


async def process_densecap(message: Message):
    target, dest = await extract_image(message, with_profile_photo=True)
    file = await download(dest)
    if file is None:
        return True

    try:
        async with ChatActioner(message.chat, action_by_type(ContentType.PHOTO)):
            photo_url, scale, result = await densecap(file)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    if not result.get('captions'):
        return await target.reply_photo(photo_url, caption=f'😒 Объекты не найдены')

    image: Image.Image = await download_image(photo_url)
    draw = ImageDraw.Draw(image)
    colors_it = cycle(reversed(colors_2))

    for d in sorted(result['captions'], key=itemgetter('confidence'), reverse=True)[:6]:
        (x, y, d0, d1), text, confidence = d['bounding_box'], d['caption'], d['confidence']
        box = tuple(int(p * scale) for p in (x, y, x + d0, y + d1))
        color = next(colors_it)

        draw.rounded_rectangle(box, radius=5, outline=color, width=3)
        draw.text(((box[0] + box[2]) // 2, box[1] + 5), text, fill='white', stroke_width=5, stroke_fill=color, align='center', anchor='ms', font=font)

    return await target.reply_photo(image_bytes_io(image))


async def process_duckduckgo(message: Message, meta: MetaInfo):
    target, query = meta.extract_text()

    query = one_liner(cut_long_text(query, hard_max_len=100)[0]).strip().replace('\u200b', '')

    if not query:
        return True

    def lines(t: str) -> str:
        if t:
            return '\n' + t + '\n'
        return ''

    async with ChatActioner(message.chat, action_by_type(ContentType.TEXT)):
        r = await duckduckgo(query)

        if r['Redirect']:
            return await target.reply(hlink(r['Redirect'], r['Redirect']), disable_web_page_preview=True)

        text = f'''{hbold(r['Heading'])}\n{lines(r['AbstractText'])}\n{parse.unquote(r['AbstractURL'])}'''.strip()

        if not text or text == '<b></b>':
            return await message.reply('🤷🏻‍♂️ Ничего не найдено\n\nИскать на ' + hlink('DuckDuckGo', f'https://duckduckgo.com/?q={query}'),
                                       disable_web_page_preview=False)

        preview = r['AbstractURL']
        if not preview and r['Image']:
            preview = 'https://api.duckduckgo.com/' + r['Image']

        return await send_super_reply(target, text=text, web_preview=preview)


async def process_imgur(message: Message):
    def handler(r: dict) -> str:
        if e := r.get('error'):
            return f'🤷🏻‍♂️ {e}'
        return r['link'] + ' | ' + str(r['width']) + 'x' + str(r['height']) + ' | ' + prettify_bytes(r['size'])

    return await process_external(message, imgur_upload, output_type=ContentType.TEXT, handler=handler)


async def process_ud(message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if text is None:
        return True

    try:
        async with ChatActioner(message.chat, action_by_type(ContentType.TEXT)):
            result = await urban_dictionary(text)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    texts = []
    prev_header, total_len = None, 0
    for r in result[:3]:
        text = ''
        text += f"{hbold(r['header'])}\n\n" if r['header'].casefold() != prev_header else ''
        text += f"{quote_html(r['meaning'])}\n\n"
        text += f"Example:\n{hitalic(r['example'])}\n\n"
        text += f"👍🏻 {hbold(r['up'])} 👎🏻 {hbold(r['down'])}\n"
        text += f"{hbold('———')}\n"

        prev_header = r['header'].casefold()
        total_len += len(text)
        if total_len > TELEGRAM_MESSAGE_MAX_LEN:
            break
        texts.append(text)

    result = f"\n".join(texts)
    return await target.reply(result)


async def process_copilot(message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if not text:
        return True

    codes = [e.get_text(text) for e in target.entities if e.type == MessageEntityType.PRE]
    if codes:
        source = '<| file ext=.py |>\n' + codes[0]
    else:
        source = dedent(
            f'''
            <| file ext=.py |>
    
            def <infill>
                """{text}"""
                <infill>
            '''
        ).lstrip()

    try:
        async with ChatActioner(message.chat, action_by_type(ContentType.TEXT)):
            result = await hg_copilot(source)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    return await target.reply(hpre(result))


async def process_fake_voice(message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if not text:
        return True

    try:
        async with ChatActioner(message.chat, action_by_type(ContentType.AUDIO)):
            text = await translate(text, 'ru', 'en')
            wav_url = await fake_you(text, voice=Voices[meta.keyword.lower()].value)
            file = FakeBytesIO(await download_content(wav_url))
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    voice, timeouted = await cpu_executor.run(to_ogg_opus, file)
    if timeouted:
        return await message.reply(hcode('🤷🏻‍♂️ Timeout'))
    if not voice:
        return await message.reply(hcode('🤷🏻‍♂️ Не удалось выполнить запрос'))

    return await target.reply_voice(voice)


async def process_topdf(message: Message, meta: MetaInfo):
    target, dest = await meta.extract_doc()
    if dest is None:
        return True

    try:
        async with ChatActioner(message.chat, action_by_type(ContentType.DOCUMENT)):
            file = await download(dest)
            url, thumb, convert_name = await convert_to_pdf(file, dest.file_name, dest.mime_type)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    return await target.reply_document(InputFile.from_url(url, convert_name), thumb=InputFile.from_url(thumb))


async def process_badwiki(message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if not text:
        return True

    text = shorten(text, width=40, placeholder='')
    text = translit(text, 'ru', reversed=True)

    try:
        async with ChatActioner(message.chat, ChatActions.TYPING):
            result = await badwiki(text)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    result = hitalic(quote_html(one_liner(result).strip()))
    return await target.reply(result)


async def process_rugpt3(message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if not text:
        return True

    try:
        async with ChatActioner(message.chat, ChatActions.TYPING):
            for _ in range(3):
                result = await gpt3(text)
                if len(result) > len(text):
                    break
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    skip_length = max(0, len(text) + len(result) - TELEGRAM_MESSAGE_MAX_LEN)
    result = hbold(text[skip_length:]) + quote_html(cut_long_text(result[len(text):], hard_max_len=600)[0])
    return await target.reply(result)


async def process_openai_gpt3(message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if not text:
        return True

    try:
        async with ChatActioner(message.chat, ChatActions.TYPING):
            result = await completion(text)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    skip_length = max(0, len(text) + len(result) - TELEGRAM_MESSAGE_MAX_LEN)
    result = hbold(text[skip_length:]) + quote_html(result)
    return await target.reply(result)


async def process_gpt3(message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if not text:
        return True

    if is_ru(text):
        return await process_rugpt3(message, meta)
    return await process_openai_gpt3(message, meta)


async def process_gptn(message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if not text:
        return True

    try:
        async with ChatActioner(message.chat, ChatActions.TYPING):
            result = await gptn(text)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    skip_length = max(0, len(text) + len(result) - TELEGRAM_MESSAGE_MAX_LEN)
    result = hbold(quote_html(text[skip_length:])) + quote_html(cut_long_text(result[len(text):], hard_max_len=600)[0])
    return await target.reply(result)


async def process_porfirevich(message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if not text:
        return True

    try:
        async with ChatActioner(message.chat, ChatActions.TYPING):
            result = await porfirevich(text)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    skip_length = max(0, len(text) + len(result) - TELEGRAM_MESSAGE_MAX_LEN)
    result = hbold(quote_html(text[skip_length:])) + quote_html(result)
    return await target.reply(result)


async def process_bored(message: Message, meta: MetaInfo):
    target = meta.reply()

    try:
        async with ChatActioner(message.chat, ChatActions.TYPING):
            r = await bored()
            tr = await translate(r["activity"], 'en_GB', 'ru')
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    def humanize_price(price: float) -> str:
        if price < 0.2:
            return '🆓'
        price = int(price * 10 / 2 / 0.8)
        return '💰' * price + '◻️' * (5 - price)

    def humanize_participants(participants: int) -> str:
        return '👤' * participants

    def humanize_accessibility(accessibility: float) -> str:
        accessibility = accessibility * 10
        if accessibility < 3.3:
            return 'легко'
        if accessibility < 6.6:
            return 'средне'
        return 'сложно'

    text = ''
    text += f'🇬🇧 {hbold(r["activity"])}\n\n'
    text += f'🇷🇺 {hbold(tr)}\n\n'
    text += f'{hbold("Участников")}: {humanize_participants(r["participants"])}\n'
    text += f'{hbold("Стоимость")}: {humanize_price(r["price"])}\n'
    text += f'{hbold("Доступность")}: {humanize_accessibility(r["accessibility"])}\n\n'
    if r['link']:
        text += f'{hbold("Ссылка")}: {r["link"]}\n\n'

    return await target.reply(text)


async def process_cheatsheet(message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if not text:
        return True

    lang, _, query = text.partition(' ')
    try:
        async with ChatActioner(message.chat, ChatActions.TYPING):
            result = await cheatsheet(lang, query)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f'🤷🏻‍♂️ {e.text}'))

    result = hpre(result[:TELEGRAM_MESSAGE_MAX_LEN])
    return await target.reply(result)


async def process_orfogrammka(message: Message, meta: MetaInfo, profile: str):
    target, text = meta.extract_text()
    if not text:
        return True

    note = ''
    if len(text) > 1000:
        note = ' (' + hitalic('первые 1000 символов') + ')'

    text = shorten(text, width=1000, placeholder='')
    try:
        async with ChatActioner(message.chat, ChatActions.TYPING):
            result = await orfogrammka.submit(text, profile)
            text = orfogrammka.humanize(result)
    except OrfogrammkaError:
        await message.reply(hitalic(f'🤷🏻‍♂️ Не удалось выполнить запрос'))
        raise

    return await send_super_reply(target, text + note)


async def process_orfogrammka_common(message: Message, meta: MetaInfo):
    return await process_orfogrammka(message, meta, 'COMMON')


async def process_orfogrammka_cicero(message: Message, meta: MetaInfo):
    return await process_orfogrammka(message, meta, 'CICERO')


async def process_orfogrammka_quality(message: Message, meta: MetaInfo):
    return await process_orfogrammka(message, meta, 'QUALITY')
