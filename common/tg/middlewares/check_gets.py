from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.types import Message, ChatType
from aiogram.utils.markdown import hbold


class CheckGets(BaseMiddleware):
    @staticmethod
    async def on_pre_process_message(message: Message, _results):
        if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            return

        message_id = str(message.message_id)
        if not message_id.endswith(('00000', '11111', '22222', '33333', '44444', '55555', '66666', '77777', '88888')):
            return

        return await message.reply(f'🥳 {hbold("Поздравляем")}! Вы отправили сообщение №{hbold(message_id)}:\n\n— {message.url}')
