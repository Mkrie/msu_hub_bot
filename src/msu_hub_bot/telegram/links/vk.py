"""Deliver VK renderer output within Telegram text, link and media limits."""

import re
from collections import deque
from collections.abc import Iterator
from functools import partial
from html import unescape

from aiogram.types import Message
from aiogram.utils.markdown import hide_link

from msu_hub_bot.providers.vk.posts import VkPost
from msu_hub_bot.settings import settings
from msu_hub_bot.telegram.wrapper import BotWrapper

_HTML_ATOM = re.compile(r"<tg-emoji\b[^>]*>.*?</tg-emoji>|</?a\b[^>]*>|&(?:#[0-9]+|#x[0-9a-fA-F]+|[a-zA-Z]+);|.", re.S)
# Preserve Telegram's bare-URL detection; don't absorb escaped surrounding quotes.
_SHORT_URL = r"""https?://(?:(?!&(?:lt|gt|quot|#x27|#39);)[^\s<>\[\]"']){1,400}(?=$|[\s<>\[\]"']|&(?:lt|gt|quot|#x27|#39);)"""
_HTML_TOKEN = re.compile(_SHORT_URL + "|" + _HTML_ATOM.pattern, re.S)
_LINK_TAG = re.compile(r"</?a\b[^>]*>")
_EMOJI_TAG = re.compile(r"</?tg-emoji\b[^>]*>")
_VK_LOGO = '<tg-emoji emoji-id="5278229754099540071">💙</tg-emoji> '


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_html(text: str, *, limit: int = 4096, max_bytes: int = 32768, max_links: int = 100) -> Iterator[str]:
    """Split VK's escaped text/anchors without losing labels or breaking markup.

    Telegram budgets parsed text, raw HTML bytes and link entities separately.
    A caller adding a hidden preview must reserve its space in all three budgets.
    """
    if min(limit, max_bytes, max_links) < 1:
        raise ValueError("Telegram text budgets must be positive")
    source = (match[0] for match in _HTML_TOKEN.finditer(text))
    pending: deque[str] = deque()
    tokens: list[str] = []
    active_link = ""
    size = raw_bytes = links = 0
    paragraph: tuple[int, str, int] | None = None
    word: tuple[int, str, int] | None = None

    def rendered(parts: list[str], link: str) -> str:
        value = "".join(parts) + ("</a>" if link else "")
        return value if unescape(_EMOJI_TAG.sub("", _LINK_TAG.sub("", value))).strip() else ""

    while True:
        token = pending.popleft() if pending else next(source, None)
        if token is None:
            break
        opening = token.startswith("<a ")
        closing = token == "</a>"
        visible = "" if opening or closing else unescape(_EMOJI_TAG.sub("", token))
        next_link = token if opening else "" if closing else active_link
        token_size = utf16_length(visible)
        token_bytes = len(token.encode())
        if token.startswith(("http://", "https://")) and (active_link or token_size > limit or token_bytes > max_bytes):
            pending.extendleft(reversed([match[0] for match in _HTML_ATOM.finditer(token)]))
            continue
        if (
            size + token_size > limit
            or raw_bytes + token_bytes + (len("</a>") if next_link else 0) > max_bytes
            or links + opening > max_links
        ):
            if not size:
                raise ValueError("A VK link cannot fit in a Telegram message")
            boundary = next((point for point in (paragraph, word) if point and point[2] >= size / 2), None)
            end, link, _ = boundary or (len(tokens), active_link, size)
            if link and end < len(tokens) and tokens[end] == "</a>":
                end, link = end + 1, ""
            if part := rendered(tokens[:end], link):
                yield part
            pending.extendleft(reversed([*tokens[end:], token]))
            tokens = [link] if link else []
            active_link = link
            size, raw_bytes, links = 0, len(link.encode()), int(bool(link))
            paragraph = word = None
            continue
        tokens.append(token)
        active_link = next_link
        size += token_size
        raw_bytes += token_bytes
        links += opening
        if visible.isspace():
            word = (len(tokens), active_link, size)
            if visible == "\n":
                paragraph = word
    if part := rendered(tokens, active_link):
        yield part


async def publish_vk_post(
    post: VkPost,
    bot: BotWrapper,
    chat_id: int,
    reply_to: int | None = None,
    with_header: bool = True,
    *,
    message_thread_id: int | None = None,
    parsed_link: bool = False,
) -> Message | None:
    # The configured destination prefers a captioned album when it fits.
    if chat_id == settings.vk_default_chat_id:
        text, web_preview, photos_urls, gifs_urls = post.for_publish(False, False)
        if parsed_link:
            text = _VK_LOGO + text
        if utf16_length(_EMOJI_TAG.sub("", text)) <= 1024:
            return await bot.send_super_message_prefer_album(
                text, web_preview, photos_urls, gifs_urls, chat_id, reply_to, message_thread_id=message_thread_id
            )
    else:
        text, web_preview, photos_urls, gifs_urls = post.for_publish(with_header)
        if parsed_link:
            text = _VK_LOGO + text
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
