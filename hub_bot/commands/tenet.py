from msu_hub_bot.settings import settings

import io
from typing import Optional

from PIL import Image, ImageFile, ImageOps
from aiogram.types import Message
from aiogram.utils.markdown import hcode, quote_html

from app import cpu_executor
from common.tg.utils import action_by_type, download
from common.utils import megabytes, image_bytes_io, FakeBytesIO
from utils.ffmpeg import ffmpeg

ImageFile.LOAD_TRUNCATED_IMAGES = True


def reverse_audio(file: io.BytesIO) -> Optional[io.BytesIO]:
    parameters = [
        '-vf', 'reverse',
        '-af', 'areverse',
        '-f', 'ogg', '-acodec', 'libopus',
    ]
    result = ffmpeg(file, parameters=parameters, out_suffix='.ogg')
    return result


def reverse_video(file: io.BytesIO) -> Optional[io.BytesIO]:
    parameters = [
        '-vf', 'reverse',
        '-af', 'areverse',
        '-f', 'mp4',
    ]
    result = ffmpeg(file, parameters=parameters, out_suffix='.mp4')
    return result


def reverse_webm(file: io.BytesIO) -> Optional[io.BytesIO]:
    parameters = [
        '-vf', 'reverse',
        '-c:v', 'libvpx-vp9',
    ]
    result = ffmpeg(file, parameters=parameters, out_suffix='.webm')
    return result


def mirror_image(file: io.BytesIO, name: str = '', ext: str = 'png') -> io.BytesIO:
    image = ImageOps.mirror(Image.open(file))
    return image_bytes_io(image, name or 'image', ext)


async def process_reverse(message: Message):
    t = target = message.reply_to_message
    if not target:
        if photos := (await message.from_user.get_profile_photos(limit=1)).photos:
            f = await photos[0][-1].download(FakeBytesIO())
            return await message.reply_photo(mirror_image(f))
        return True

    if action := action_by_type(t.content_type):
        await t.chat.do(action)

    if text := t.text:
        return await t.reply(quote_html(text[::-1]))

    if file := t.sticker:
        if file.is_animated:
            return True

        if file.is_video:
            f = await download(file)

            result_file, timeouted = await cpu_executor.run(reverse_webm, f)
            if timeouted:
                return await message.reply(hcode('🤷🏻‍♂️ Timeout'))
            if not result_file:
                return await message.reply(hcode('🤷🏻‍♂️ Не удалось выполнить запрос'))

            await message.bot.add_sticker_to_set(settings.tenet_sticker_owner_id, 'tenet_webm_by_msu_hub_bot', '🔄', webm_sticker=result_file)
            ss = await message.bot.get_sticker_set('tenet_webm_by_msu_hub_bot')
            await t.reply_sticker(ss.stickers[-1].file_id)
            await ss.stickers[-1].delete_from_set()
            return True

        f = await file.download(destination_file=FakeBytesIO())
        return await t.reply_sticker(mirror_image(f, ext='webp'))

    if poll := t.poll:
        return await t.bot.send_poll(t.chat.id,
                                     question=poll.question[::-1],
                                     options=[o.text[::-1] for o in poll.options],
                                     type=poll.type,
                                     allows_multiple_answers=poll.allows_multiple_answers,
                                     correct_option_id=0,
                                     explanation=poll.explanation and poll.explanation[::-1],
                                     reply_to_message_id=t.message_id)

    if location := (t.venue and t.venue.location or t.location):
        lat, lon = location.latitude, location.longitude
        return await message.reply_location(latitude=-lat, longitude=lon - 180 if lon > 0 else lon + 180)

    caption = t.caption and quote_html(t.caption[::-1]) or ''

    if file := t.photo:
        f = await file[-1].download(destination_file=FakeBytesIO())
        return await t.reply_photo(mirror_image(f), caption=caption)

    if t.document and t.document.mime_type in ('image/jpeg', 'image/jpg', 'image/png'):
        file = t.document
        if file.file_size > megabytes(20):
            return await message.reply(hcode('🤷🏻‍♂️ Мне недоступны файлы больше 20 Мб'))

        f = await file.download(destination_file=FakeBytesIO())

        rsplit = file.file_name.rsplit('.', 1)
        name, ext = rsplit[0][::-1], t.document.mime_subtype
        return await t.reply_document(mirror_image(f, name, ext), caption=caption)

    if file := t.voice or t.audio or t.animation or t.video_note or t.video:
        if file.file_size > megabytes(20):
            return await message.reply(hcode('🤷🏻‍♂️ Мне недоступны файлы больше 20 Мб'))

        f = await file.download(destination_file=FakeBytesIO())

        reverse = reverse_audio if t.voice or t.audio else reverse_video
        result_file, timeouted = await cpu_executor.run(reverse, f)
        if timeouted:
            return await message.reply(hcode('🤷🏻‍♂️ Timeout'))
        if not result_file:
            return await message.reply(hcode('🤷🏻‍♂️ Не удалось выполнить запрос, возможно файл слишком большой'))

        if t.voice or t.audio:
            return await t.reply_voice(result_file, caption=caption, duration=file.duration)

        if t.animation:
            return await t.reply_animation(result_file, caption=caption)

        if t.video_note:
            return await t.reply_video_note(result_file, duration=file.duration)

        return await t.reply_video(result_file, caption=caption, duration=file.duration)

    return True
