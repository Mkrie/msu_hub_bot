import io
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Optional

import aiogram
import cv2
from aiocache import cached
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, InputFile, InputMediaPhoto
from aiogram.utils.callback_data import CallbackData

from app import cpu_executor
from common.tg.callbacks import CallbackCommandBase
from common.utils import bytes_io


def capture():
    return cv2.VideoCapture('http://cam.mnc.ru/axis-cgi/mjpg/video.cgi?camera=1')


def camera(name: str) -> Optional[bytes]:
    if name == 'msu':
        ret, frame = capture().read()

        if not ret:
            ret, frame = capture().read()

        if ret:
            t = (Path(tempfile.gettempdir()) / Path(tempfile.mktemp(suffix='.jpg')))
            cv2.imwrite(str(t), frame)
            return t.read_bytes()

    return


@cached(ttl=10)
async def _camera_msu() -> Optional[bytes]:
    content, timeouted = await cpu_executor.run(camera, 'msu')
    if timeouted:
        return
    if not content:
        return
    return content


async def camera_msu() -> Optional[io.BytesIO]:
    content = await _camera_msu()
    if not content:
        return
    return bytes_io(content)


class Camera(CallbackCommandBase):
    callback_data = CallbackData('camera', 'name', 'action')

    @classmethod
    def keyboard(cls, name: str) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardMarkup().row(
            InlineKeyboardButton(text='🔄 Обновить', callback_data=cls.callback_data.new(name, 'update')),
            InlineKeyboardButton(text='⏹ Сохранить вид', callback_data=cls.callback_data.new(name, 'stop')),
        )
        return keyboard

    @classmethod
    async def process(cls, message: Message):
        target = message.reply_to_message or message
        if photo := await camera_msu():
            return await target.reply_photo(photo, reply_markup=cls.keyboard('msu'))
        return await message.reply('😔 Камера недоступна')

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: dict):
        m = query.message

        name, action = callback_data['name'], callback_data['action']

        if action == 'stop':
            await query.answer('✅ Вид сохранён', cache_time=5)
            return await m.delete_reply_markup()

        await query.answer('✅ Вид обновляется', cache_time=1)

        async with cls.lock(m.chat.id):
            if photo := await camera_msu():
                with suppress(aiogram.exceptions.BadRequest):
                    return await m.edit_media(InputMediaPhoto(InputFile(photo)), reply_markup=cls.keyboard(name))

        return True
