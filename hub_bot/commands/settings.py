from aiogram.types import Message, ChatType
from aiogram.utils.markdown import hcode, hbold, hitalic

from common.tg.filters import MetaInfo
from common.tg.middlewares.settings import Settings
from common.tg.utils import chat_link


async def process_settings(message: Message, meta: MetaInfo, settings: Settings):
    args = meta.arguments
    schema = settings.schema()

    if not args:
        text = hbold('Настройки чата') + f' {await chat_link(message.chat)}\n\n'
        for name, p in schema['properties'].items():
            text += f'• {hcode(name)}: {p["type"]} = {getattr(settings, name)}\n'
        text += f'\n'
        text += f'Чтобы изменить настройку, пишите {hcode("/settings key value")}.\n'
        text += f'\n'
        text += hitalic('Note: это экспериментальный способ настройки, вероятно он будет улучшен, предложения настроек пишите') + ' ' + hcode("@rm_bk") + '.'
        return await message.reply(text)

    if len(args) != 2:
        text = f'ℹ️ Чтобы изменить настройку, пишите {hcode("/settings key value")}.'
        return await message.reply(text)

    key, value = args

    if key not in schema['properties']:
        text = f'🤷🏻‍♂️ У меня нет настройки {hcode(key)}, полный список настроек: /settings.'
        return await message.reply(text)

    if message.chat.type != ChatType.PRIVATE:
        member = await message.chat.get_member(message.from_user.id)
        if not member.is_chat_admin():
            text = f'🤷🏻‍♂️ Изменять настройки чата могут только админы.'
            return await message.reply(text)

    value_type = schema['properties'][key]['type']
    if value_type == 'boolean':
        if value.lower() in ('1', 'true', 'ok', 'enable'):
            setattr(settings, key, True)
        elif value.lower() in ('0', 'false', 'null', 'none', 'nan'):
            setattr(settings, key, False)
        else:
            text = f'🤷🏻‍♂️ Не могу распознать {hcode(value)} как тип {hcode(value_type)}.'
            return await message.reply(text)

        text = f'🆗 Настройка {hcode(key)} поставлена в {hcode(getattr(settings, key))}'
        return await message.reply(text)
