from aiogram.types import Message

from common.tg.filters import MetaInfo
from common.utils import one_liner, shorten


async def process_votes(_message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    args = meta.arguments

    if not text and not args:
        return True

    if text:
        if t := target.forward_from:
            name = t.full_name
        elif t := target.forward_from_chat:
            name = t.full_name
        elif t := target.forward_sender_name:
            name = t
        else:
            name = target.from_user.full_name

        text = f'{name}: {one_liner(text)}'

    else:
        text = 'Голосование'

    options = ['👍🏻', '👎🏻']
    if args:
        options = [a[:100] for a in args][:10]

    return await target.reply_poll(
        question=shorten(text, width=140, placeholder=' [...] '),
        options=options + ['🤔'],
        is_anonymous=False,
    )
