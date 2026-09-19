from functools import partial

from aiogram.types import Message
from aiogram.utils.markdown import hide_link

from msu_hub_bot.providers.vk.posts import VkPost
from msu_hub_bot.providers.vk.utils import split_html, utf16_length
from msu_hub_bot.settings import settings
from msu_hub_bot.telegram.wrapper import BotWrapper


async def publish_vk_post(
    post: VkPost,
    bot: BotWrapper,
    chat_id: int,
    reply_to: int | None = None,
    with_header: bool = True,
    *,
    message_thread_id: int | None = None,
) -> Message | None:
    # The configured destination prefers a captioned album when it fits.
    if chat_id == settings.vk_default_chat_id:
        text, web_preview, photos_urls, gifs_urls = post.for_publish(False, False)
        if utf16_length(text) <= 1024:
            return await bot.send_super_message_prefer_album(
                text, web_preview, photos_urls, gifs_urls, chat_id, reply_to, message_thread_id=message_thread_id
            )
    else:
        text, web_preview, photos_urls, gifs_urls = post.for_publish(with_header)
    preview = hide_link(web_preview) if web_preview else ""
    return await bot.send_super_message(
        text,
        web_preview,
        photos_urls,
        gifs_urls,
        chat_id,
        reply_to,
        message_thread_id=message_thread_id,
        text_splitter=partial(
            split_html,
            limit=4096 - bool(preview),
            max_bytes=32768 - len(preview.encode()),
            max_links=100 - bool(preview),
        ),
    )
