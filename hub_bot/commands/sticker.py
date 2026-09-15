import io

import aiogram
import emoji
from PIL import Image, ImageOps
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import StatesGroup, State
from aiogram.types import Message, ChatType
from aiogram.utils.markdown import hlink

from common.tg.filters import MetaInfo
from common.tg.utils import extract_image, download, download_by_file_id
from common.utils import image_bytes_io, FakeBytesIO

sticker_set_name_template = 'with_love_for_{id}_by_msu_hub_bot'
sticker_set_name_template_a = 'with_love_for_{id}a_by_msu_hub_bot'


class StickerStates(StatesGroup):
    sticker_set_name = State()


def only_emojis(text: str) -> str:
    d = {e['emoji']: True for e in emoji.emoji_list(text)}
    return ''.join(list(d)[:5])


class Stickers:
    @classmethod
    async def tgs(cls, message: Message):
        if not (target := message.reply_to_message):
            return None
        if not target.sticker:
            return None
        if not target.sticker.is_animated:
            return None
        return target.sticker.file_id, await target.sticker.download(destination_file=FakeBytesIO())

    @staticmethod
    def png_cut(f: io.BytesIO, squared: bool = False) -> io.BytesIO:
        image: Image.Image = Image.open(f)

        if squared:
            box = (512, 512)
        else:
            box = (512, image.height * 512 // image.width)
            if image.width < image.height:
                box = (image.width * 512 // image.height, 512)

        image = ImageOps.fit(image, box, Image.LANCZOS)
        return image_bytes_io(image, 'sticker', 'png')

    @classmethod
    async def png(cls, message: Message):
        target, dest = await extract_image(message, with_profile_photo=True)
        file = await download(dest)
        if file:
            return dest.file_id, cls.png_cut(file)

    @classmethod
    async def sticker_set_name(cls, message: Message, state: FSMContext):
        title = message.text or message.caption or ''
        if not title:
            return await message.reply(f'🎈 Выберите подходящее название для стикерпака или тыкните /cancel')

        async with state.proxy() as data:
            sticker_set_name = data['sticker_set_name']
            emojis = data['emojis']
            png = data['png']
            tgs = data['tgs']

        await state.finish()

        png = png and cls.png_cut(await download_by_file_id(png)) or None
        tgs = tgs and await download_by_file_id(tgs) or None

        try:
            await message.bot.create_new_sticker_set(user_id=message.from_user.id, name=sticker_set_name, emojis=emojis,
                                                     png_sticker=png, tgs_sticker=tgs, title=title)
            link = hlink('стикерпак', f'https://t.me/addstickers/{sticker_set_name}')
            await message.reply(f'✨ Ура, для вас был создан {link}. Управлять им можно через @Stickers.\n\n'
                                f'Имейте в виду, что на телефонах новые стикеры появляются с задержкой.')
            ss = await message.bot.get_sticker_set(sticker_set_name)
            return await message.reply_sticker(ss.stickers[-1].file_id)
        except aiogram.exceptions.InvalidPeerID:
            return await message.reply(f'🤷🏻‍♂️ Чтоб я смог создать стикерпак для вас, вам нужно начать личный чат со мной')
        except aiogram.exceptions.BadRequest:
            await message.reply(f'🤷🏻‍♂️ Произошла какая-то ошибка, подробнее в /error_stickers')
            raise

    @classmethod
    async def make_sticker(cls, message: Message, meta: MetaInfo, state: FSMContext, sticker_set_name: str, png=None, tgs=None):
        emojis = only_emojis(meta.extract_text()[1]) or '✨'

        try:
            try:
                await message.bot.get_sticker_set(sticker_set_name)
                await message.bot.add_sticker_to_set(user_id=message.from_user.id, name=sticker_set_name,
                                                     emojis=emojis, png_sticker=png and png[1], tgs_sticker=tgs and tgs[1])

            except aiogram.exceptions.InvalidStickersSet:
                await StickerStates.sticker_set_name.set()
                async with state.proxy() as data:
                    data['sticker_set_name'] = sticker_set_name
                    data['emojis'] = emojis
                    data['png'] = png and png[0] or ''
                    data['tgs'] = tgs and tgs[0] or ''
                return await message.reply(
                    f'🎈 Для вас еще не создан стикерпак. Придумайте ему название в следующем сообщении ⬇️, или тыкните /cancel. '
                    f'Учтите, что название стикерпака видят все и изменить его нельзя.')

        except aiogram.exceptions.BadRequest as e:
            if str(e) == 'Stickers_too_much':
                return await message.reply(f'🤷🏻‍♂️ Стикерпак заполнен. Удалить ненужный стикер можно командой /sd ответом на него.')
            else:
                await message.reply(f'🤷🏻‍♂️ Произошла какая-то ошибка, подробнее в /error_stickers')
                raise

        ss = await message.bot.get_sticker_set(sticker_set_name)
        return await message.reply_sticker(ss.stickers[-1].file_id)

    @classmethod
    async def make_sticker_png(cls, message: Message, meta: MetaInfo, state: FSMContext, sticker_set_name: str):
        sticker = await cls.png(message)
        if sticker:
            return await cls.make_sticker(message, meta, state, sticker_set_name, png=sticker)

    @classmethod
    async def make_sticker_tgs(cls, message: Message, meta: MetaInfo, state: FSMContext, sticker_set_name: str):
        sticker = await cls.tgs(message)
        if sticker:
            return await cls.make_sticker(message, meta, state, sticker_set_name, tgs=sticker)


async def process_sticker(message: Message, meta: MetaInfo, state: FSMContext):
    sticker_set_name = sticker_set_name_template.format(id=message.from_user.id)
    return await Stickers.make_sticker_png(message, meta, state, sticker_set_name)


async def process_sticker_chat(message: Message, meta: MetaInfo, state: FSMContext):
    if message.chat.type == ChatType.PRIVATE:
        return await message.reply(f'🤷🏻‍♂️ Эта команда только для чатов')

    if not message.chat.all_members_are_administrators:
        admins = await message.chat.get_administrators()
        if message.from_user.id not in (a.user.id for a in admins):
            return await message.reply(f'🤷🏻‍♂️ Стикерпак чата могут редактировать только его админы')

    sticker_set_name = sticker_set_name_template.format(id=abs(message.chat.id))
    return await Stickers.make_sticker_png(message, meta, state, sticker_set_name)


async def process_animated_sticker(message: Message, meta: MetaInfo, state: FSMContext):
    sticker_set_name = sticker_set_name_template_a.format(id=message.from_user.id)
    return await Stickers.make_sticker_tgs(message, meta, state, sticker_set_name)


async def process_animated_sticker_chat(message: Message, meta: MetaInfo, state: FSMContext):
    if message.chat.type == ChatType.PRIVATE:
        return await message.reply(f'🤷🏻‍♂️ Эта команда только для чатов')

    if not message.chat.all_members_are_administrators:
        admins = await message.chat.get_administrators()
        if message.from_user.id not in (a.user.id for a in admins):
            return await message.reply(f'🤷🏻‍♂️ Стикерпак чата могут редактировать только его админы')

    sticker_set_name = sticker_set_name_template_a.format(id=abs(message.chat.id))
    return await Stickers.make_sticker_tgs(message, meta, state, sticker_set_name)


async def process_sticker_delete(message: Message):
    if not (target := message.reply_to_message):
        return True
    if not (sticker := target.sticker):
        return True

    name = sticker.set_name
    link = hlink('пака', f'https://t.me/addstickers/{name}')

    if not name.endswith('_by_msu_hub_bot'):
        return await message.reply('🤷🏻‍♂️ Этот стикерпак создан не мной, попробуйте через @Stickers')

    if name in (sticker_set_name_template.format(id=message.from_user.id),
                sticker_set_name_template_a.format(id=message.from_user.id)):
        await sticker.delete_from_set()
        return await message.reply(f'✅ Стикер удален из {link}, в течение часа он пропадет из набора у всех пользователей')

    if name in (sticker_set_name_template.format(id=abs(message.chat.id)),
                sticker_set_name_template_a.format(id=abs(message.chat.id))):
        admins = await message.chat.get_administrators()
        if message.from_user.id not in (a.user.id for a in admins):
            return await message.reply(f'🤷🏻‍♂️ Стикерпак чата могут редактировать только его админы')
        await sticker.delete_from_set()
        return await message.reply(f'✅ Стикер удален из {link}, в течение часа он пропадет из набора у всех пользователей')

    return await message.reply('🤷🏻‍♂️ Судя по всему, стикерпак создан другим пользователем или в другом чате, '
                               'где создавали — там и удаляйте')
