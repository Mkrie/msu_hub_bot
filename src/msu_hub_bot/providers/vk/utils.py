"""Escape raw VK text once and keep links/media within public HTTP boundaries."""

import ipaddress
import re
from collections import deque
from collections.abc import Iterator
from html import escape, unescape
from urllib.parse import urlsplit

from msu_hub_bot.utils import shorten

_WIKI_OR_URL = re.compile(r"\[([^ |\n]+)\|([^\]\n]+)\]|https?://[^\s<>\[\]\"']+", re.U)
_HASHTAG = re.compile(r"(#\S+)@\S+", re.U)
_HTML_ATOM = re.compile(r"</?a\b[^>]*>|&(?:#[0-9]+|#x[0-9a-fA-F]+|[a-zA-Z]+);|.", re.S)
# Preserve Telegram's bare-URL detection; don't absorb escaped surrounding quotes.
_SHORT_URL = r"""https?://(?:(?!&(?:lt|gt|quot|#x27|#39);)[^\s<>\[\]"']){1,400}(?=$|[\s<>\[\]"']|&(?:lt|gt|quot|#x27|#39);)"""
_HTML_TOKEN = re.compile(_SHORT_URL + "|" + _HTML_ATOM.pattern, re.S)
_LINK_TAG = re.compile(r"</?a\b[^>]*>")
_MEDIA_DOMAINS = ("userapi.com", "vkuserphoto.ru", "vk.com", "vk.ru", "okcdn.ru", "mycdn.me", "vk-cdn.net")


def safe_url(value: str, *, media: bool = False) -> str:
    if len(value) > 2048 or re.search(r"[\s<>\"'\x00-\x1f\x7f]", value):
        return ""
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower().rstrip(".")
        if url.scheme not in {"https", "http"} or not host or url.username or url.password or url.port not in {None, 80, 443}:
            return ""
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        if media and not any(host == domain or host.endswith("." + domain) for domain in _MEDIA_DOMAINS):
            return ""
        return value
    except ValueError:
        return ""


def href(url: str, text: str | None = None, url_cut_width: int = 32) -> str:
    label = escape(text or shorten(url, width=url_cut_width))
    return f'<a href="{escape(valid, quote=True)}">{label}</a>' if (valid := safe_url(url)) else label


def prepare_vk_text(text: str) -> str:
    text = _HASHTAG.sub(r"\1", text)
    result, previous = [], 0
    for match in _WIKI_OR_URL.finditer(text):
        result.append(escape(text[previous : match.start()]))
        if target := match[1]:
            if re.fullmatch(r"[A-Za-z0-9_.-]+", target):
                target = "https://vk.com/" + target
            elif target.startswith(("vk.com/", "vk.ru/")):
                target = "https://" + target
            result.append(href(target, match[2]))
        else:
            url = match[0]
            result.append(href(url, shorten(url, width=80)) if len(url) > 80 else escape(url))
        previous = match.end()
    result.append(escape(text[previous:]))
    return "".join(result)


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
        return value if unescape(_LINK_TAG.sub("", value)).strip() else ""

    while True:
        token = pending.popleft() if pending else next(source, None)
        if token is None:
            break
        opening = token.startswith("<a ")
        closing = token == "</a>"
        visible = "" if opening or closing else unescape(token)
        next_link = token if opening else "" if closing else active_link
        token_size = utf16_length(visible)
        token_bytes = len(token.encode())
        if token.startswith(("http://", "https://")) and (token_size > limit or token_bytes > max_bytes):
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
