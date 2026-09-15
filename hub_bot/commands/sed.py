import re
from contextlib import suppress

from aiogram.types import ChatActions, Message
from aiogram.utils.exceptions import BadRequest
from aiogram.utils.markdown import hcode, quote_html

from app import cpu_executor
from common.constants import TELEGRAM_MESSAGE_MAX_LEN

re_flags = {
    'A': re.ASCII,
    'I': re.IGNORECASE,
    'L': re.LOCALE,
    'U': re.UNICODE,
    'M': re.MULTILINE,
    'S': re.DOTALL,
    'X': re.VERBOSE,
}


def get_flags(mode: str) -> int:
    result = 0
    for symbol in mode:
        result |= re_flags.get(symbol.upper(), 0)
    return result


sed_regexp = re.compile(r'^[sSыЫ]/(?P<pattern>[^\n]+)/(?P<sub>[^\n]*)(?:/(?P<mode>\w*))?$')


def sed_calc(text, commands, limit=5):
    seds = []
    for line in commands[:limit]:
        if not (sed := sed_regexp.fullmatch(line)):
            return
        seds.append(sed.groupdict())

    for sed in seds:
        pattern = sed['pattern']
        sub = sed['sub']
        mode = sed['mode'] or 'mi'

        with suppress(re.error):
            text = re.sub(pattern, sub, text, flags=get_flags(mode))

    return text


async def process_sed(message: Message):
    if not (reply_to := message.reply_to_message):
        return True

    text = reply_to.text or reply_to.caption
    if not text:
        return True

    await message.chat.do(ChatActions.TYPING)
    commands = message.text.split('\n')

    text, timeouted = await cpu_executor.run(sed_calc, text, commands)
    if timeouted:
        return await message.reply(hcode('Timeout 🤗'))
    if not text:
        return True

    with suppress(BadRequest):
        text = quote_html(text)
        return await reply_to.reply(text[:TELEGRAM_MESSAGE_MAX_LEN])
