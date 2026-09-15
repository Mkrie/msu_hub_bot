from msu_hub_bot.redaction import redact

import json

from aiogram.types import Message
from aiogram.utils.markdown import hpre, quote_html

from app import redis
from common.constants import TELEGRAM_MESSAGE_MAX_LEN
from common.logger import LoggerBuilder
from common.tg.utils import send_super_reply
from common.utils import parse_int


async def process_json(message: Message):
    target_msg = message
    if message.reply_to_message:
        target_msg = message.reply_to_message

    target = target_msg.to_python()

    cut_length = TELEGRAM_MESSAGE_MAX_LEN // 2

    if text_part := target.get('text'):
        if len(text_part) > cut_length:
            target['text'] = text_part[:cut_length] + '...'

    if 'chat' in target:
        if 'pinned_message' in target['chat']:
            target['chat']['pinned_message'] = '{ ... }'

    text = hpre(redact(json.dumps(target, ensure_ascii=False, indent=True))[:TELEGRAM_MESSAGE_MAX_LEN])
    return await target_msg.reply(text, disable_notification=True)


async def process_logs(message: Message):
    if not LoggerBuilder.default_filename:
        return True

    args = message.get_args()
    lines = int(args) if args.isdigit() else 100

    with open(LoggerBuilder.default_filename, encoding='utf-8') as file:
        a = file.readlines()[-lines:]

    text = quote_html(redact(''.join(a)))
    return await send_super_reply(message, text, text_postprocess=hpre)


async def process_delete_after(message: Message):
    if not message.reply_to_message:
        return True

    args = message.get_args().split()
    after = parse_int(args[0], 0, 3, 10 * 24 * 60 * 60) if args else 0

    return await redis.mark_message_to_delete(message.reply_to_message, after=after)
