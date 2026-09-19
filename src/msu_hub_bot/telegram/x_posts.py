"""Native X posts: explicit rich blocks, bounded uploads and ordered delivery."""

import asyncio
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urljoin

import aiohttp
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    FSInputFile,
    InputFile,
    InputMediaPhoto,
    InputMediaVideo,
    InputRichBlockBlockQuotation,
    InputRichBlockCollage,
    InputRichBlockFooter,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockSectionHeading,
    InputRichBlockUnion,
    InputRichBlockVideo,
    InputRichMessage,
    LinkPreviewOptions,
    Message,
    ReplyParameters,
    RichTextBold,
    RichTextCode,
    RichTextCustomEmoji,
    RichTextItalic,
    RichTextStrikethrough,
    RichTextUnderline,
    RichTextUnion,
    RichTextUrl,
)

from msu_hub_bot.providers.fxembed import FxMedia, FxMediaFormat, FxPost, FxStyleRange, FxText, PostLink, safe_media_url, safe_public_url
from msu_hub_bot.providers.http import USER_AGENT
from msu_hub_bot.telegram.wrapper import BotWrapper

_MAX_TEXT = 32768
_MAX_BLOCKS = 500
_MAX_MEDIA = 50
_MAX_VIDEO_SIDE = 10000
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
_X_EMOJI_ID = "5422502846648039176"
_X_EMOJI_FALLBACK = "💬"
_DOWNLOADS = asyncio.Semaphore(2)
_LINK = re.compile(r"https?://[^\s<>]+|(?<![\w@])@[A-Za-z0-9_]{1,15}\b")
_MEDIA_LINK = re.compile(r"(?:https?://)?(?:t\.co|(?:pic\.)?(?:x\.com|twitter\.com))/[^\s<>]+", re.IGNORECASE)
_STYLES = {"BOLD", "ITALIC", "CODE", "STRIKETHROUGH", "UNDERLINE"}

type _Upload = InputFile | InputMediaVideo


@dataclass(frozen=True, slots=True)
class _Span:
    text: str
    url: str | None = None
    style: str | None = None
    custom_emoji_id: str | None = None

    def rich(self) -> RichTextUnion:
        text: RichTextUnion = (
            RichTextCustomEmoji(custom_emoji_id=self.custom_emoji_id, alternative_text=self.text) if self.custom_emoji_id else self.text
        )
        if self.url:
            text = RichTextUrl(text=text, url=self.url)
        for style in (self.style or "").split(","):
            if style == "BOLD":
                text = RichTextBold(text=text)
            elif style == "ITALIC":
                text = RichTextItalic(text=text)
            elif style == "CODE":
                text = RichTextCode(text=text)
            elif style == "STRIKETHROUGH":
                text = RichTextStrikethrough(text=text)
            elif style == "UNDERLINE":
                text = RichTextUnderline(text=text)
        return text


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _pieces(text: str, limit: int) -> Iterator[str]:
    """Split on code points; never cut a surrogate pair or discard whitespace."""
    start = size = 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            yield text[start:index]
            start, size = index, 0
        size += width
    if start < len(text):
        yield text[start:]


def _autolinks(text: str) -> list[_Span]:
    result: list[_Span] = []
    start = 0
    for match in _LINK.finditer(text):
        result.append(_Span(text[start : match.start()]))
        label = match[0]
        target = "https://x.com/" + label[1:] if label.startswith("@") else label.rstrip(".,;:!?)")
        prefix = label if label.startswith("@") else target
        result.append(_Span(prefix, target if safe_public_url(target) else None))
        result.append(_Span(label[len(prefix) :]))
        start = match.end()
    result.append(_Span(text[start:]))
    return [span for span in result if span.text]


def _unescape_x(text: str) -> str:
    for escaped, literal in (("&#34;", '"'), ("&#39;", "'"), ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):
        text = text.replace(escaped, literal)
    return text


def _spans(
    text: str,
    raw: FxText | None = None,
    styles: Sequence[FxStyleRange] = (),
    *,
    unescape: bool = False,
    shown_media_ids: frozenset[str] = frozenset(),
) -> list[_Span]:
    """Facet offsets refer to raw UTF-16 text, not Python character indices."""
    text = raw.text if raw is not None else text
    if (raw is None or not raw.facets) and not styles:
        return _autolinks(_unescape_x(text) if unescape else text)
    offsets = {0: 0}
    size = 0
    for index, char in enumerate(text):
        size += _units(char)
        offsets[size] = index + 1
    all_styles = [
        *styles,
        *(
            FxStyleRange(offset=facet.indices[0], length=facet.indices[1] - facet.indices[0], style=facet.type.upper())
            for facet in (raw.facets if raw else [])
            if facet.type.upper() in _STYLES
        ),
    ]
    ranges = [
        (offsets[style.offset], offsets[style.offset + style.length], style.style.upper())
        for style in all_styles
        if style.offset in offsets and style.offset + style.length in offsets and style.style.upper() in _STYLES
    ]

    def style_at(index: int) -> str | None:
        return ",".join(sorted({style for left, right, style in ranges if left <= index < right})) or None

    def gap(left: int, right: int) -> list[_Span]:
        boundaries = sorted({left, right, *(point for begin, end, _ in ranges for point in (begin, end) if left < point < right)})
        return [
            _Span(span.text, span.url, style_at(begin))
            for begin, end in zip(boundaries, boundaries[1:])
            for span in _autolinks(_unescape_x(text[begin:end]) if unescape else text[begin:end])
        ]

    result: list[_Span] = []
    start = 0
    for facet in sorted(raw.facets if raw else [], key=lambda value: value.indices[0]):
        if facet.type.upper() in _STYLES:
            continue
        left, right = (offsets.get(value) for value in facet.indices)
        if left is None or right is None or left < start or right <= left:
            continue
        result.extend(gap(start, left))
        original = _unescape_x(text[left:right]) if unescape else text[left:right]
        if facet.type == "media" and (
            not _MEDIA_LINK.fullmatch(original) or (facet.original is not None and _unescape_x(facet.original) != original)
        ):
            # Stale offsets must not replace or erase unrelated words or links.
            result.extend(gap(left, right))
            start = right
            continue
        if facet.type == "media" and facet.id in shown_media_ids:
            start = right
            if not text[right:].strip():
                while result and not result[-1].text.strip():
                    result.pop()
                if result:
                    result[-1] = replace(result[-1], text=result[-1].text.rstrip())
                start = len(text)
                break
            continue
        target = facet.replacement or ""
        if facet.type == "mention":
            target = "https://x.com/" + original.lstrip("@")
        elif facet.type == "hashtag":
            target = "https://x.com/hashtag/" + quote(original.lstrip("#"), safe="")
        elif facet.type in {"symbol", "cashtag"}:
            target = "https://x.com/search?q=" + quote(original, safe="")
        label = facet.display or (facet.replacement if facet.type == "url" else None) or original
        if facet.type not in {"url", "mention", "hashtag", "cashtag", "symbol", "media"}:
            target, label = "", original
        result.append(_Span(label, target if safe_public_url(target) else None, style_at(left)))
        start = right
    result.extend(gap(start, len(text)))
    return result


def _paragraphs(spans: Sequence[_Span], *, limit: int = _MAX_TEXT) -> list[InputRichBlockUnion]:
    parts: list[InputRichBlockUnion] = []
    current: list[RichTextUnion] = []
    size = 0
    for span in spans:
        remaining = span.text
        while remaining:
            available = limit - size
            if available < (2 if ord(remaining[0]) > 0xFFFF else 1):
                parts.append(InputRichBlockParagraph(text=current))
                current, size = [], 0
                continue
            piece = next(_pieces(remaining, available))
            current.append(replace(span, text=piece).rich())
            size += _units(piece)
            remaining = remaining[len(piece) :]
    if current:
        parts.append(InputRichBlockParagraph(text=current))
    return parts


def _author(post: FxPost) -> InputRichBlockParagraph:
    return InputRichBlockParagraph(
        text=[
            RichTextCustomEmoji(custom_emoji_id=_X_EMOJI_ID, alternative_text=_X_EMOJI_FALLBACK),
            " ",
            RichTextBold(text=post.author.name),
            " · ",
            RichTextUrl(text="@" + post.author.screen_name, url=post.author.url),
            " · ",
            RichTextUrl(text="↗", url=post.url),
            "\n",
        ]
    )


def _selected(post: FxPost, link: PostLink | None) -> list[FxMedia]:
    media = post.media.all if post.media else []
    if link is None or link.media_kind is None or link.media_index is None:
        return media
    return [item for index, item in enumerate(media, 1) if (item.source_index or index) == link.media_index]


def _video_upload(file: InputFile, media: FxMedia, variant: FxMediaFormat | None = None) -> InputMediaVideo:
    # Rich video blocks need explicit geometry; omitted dimensions reach Telegram as 0×0.
    width, height = media.width, media.height
    if variant and variant.width and variant.height:
        width, height = variant.width, variant.height
    if width > 0 and height > 0:
        scale = max(_MAX_VIDEO_SIDE, width, height)
        dimensions: tuple[int | None, int | None] = (max(1, width * _MAX_VIDEO_SIDE // scale), max(1, height * _MAX_VIDEO_SIDE // scale))
    else:
        dimensions = None, None
    return InputMediaVideo(
        media=file,
        width=dimensions[0],
        height=dimensions[1],
        duration=round(media.duration) if media.duration is not None else None,
        supports_streaming=True,
    )


def _gallery(media: Sequence[FxMedia], uploads: Mapping[str, _Upload], post: FxPost) -> list[InputRichBlockUnion]:
    result: list[InputRichBlockUnion] = []
    group: list[InputRichBlockUnion] = []

    def flush() -> None:
        if group:
            result.append(InputRichBlockCollage(blocks=list(group)) if len(group) > 1 else group[0])
            group.clear()

    for item in media:
        upload = uploads.get(item.url)
        if upload is None:
            flush()
            label = "Фото" if item.type == "photo" else "Видео"
            result.append(InputRichBlockParagraph(text=RichTextUrl(text=f"{label} — в оригинале", url=post.url)))
        elif item.type == "photo":
            file = upload.media if isinstance(upload, InputMediaVideo) else upload
            group.append(InputRichBlockPhoto(photo=InputMediaPhoto(media=file, has_spoiler=post.possibly_sensitive)))
        else:
            video = upload if isinstance(upload, InputMediaVideo) else _video_upload(upload, item)
            group.append(InputRichBlockVideo(video=video.model_copy(update={"has_spoiler": post.possibly_sensitive})))
        if len(group) == 10:
            flush()
    flush()
    return result


def _post_blocks(post: FxPost, uploads: Mapping[str, _Upload], link: PostLink | None = None, depth: int = 0) -> list[InputRichBlockUnion]:
    blocks: list[InputRichBlockUnion] = [_author(post)]
    if post.replying_to:
        blocks.append(
            InputRichBlockParagraph(text=RichTextUrl(text=f"↪ В ответ @{post.replying_to.screen_name}", url=post.replying_to.url))
        )
    media = _selected(post, link)
    shown = frozenset(item.id for item in media if item.id and item.url in uploads)
    blocks.extend(_paragraphs(_spans(post.text, post.raw_text, unescape=True, shown_media_ids=shown)))
    blocks.extend(_gallery(media, uploads, post))
    if post.media.unsupported_count:
        blocks.append(
            InputRichBlockParagraph(text=RichTextUrl(text="Другие вложения — в оригинале", url=post.media.external_url or post.url))
        )
    if post.article:
        blocks.extend(_article_blocks(post, uploads))
    if post.poll:
        blocks.extend(_poll_blocks(post))
    if post.community_note:
        blocks.append(InputRichBlockSectionHeading(text="Контекст от сообщества", size=4))
        blocks.extend(_paragraphs(_spans(post.community_note.text, post.community_note)))
    if isinstance(post.quote, FxPost):
        quote = _post_blocks(post.quote, uploads, depth=depth + 1)
        if depth < 6:
            blocks.append(InputRichBlockBlockQuotation(blocks=quote))
        else:
            blocks.append(InputRichBlockParagraph(text=RichTextBold(text="Цитата")))
            blocks.extend(quote)
    elif post.quote:
        label = "Цитируемая публикация недоступна."
        source = post.quote.url or (f"https://x.com/i/status/{post.quote.id}" if post.quote.id else None)
        blocks.append(InputRichBlockParagraph(text=RichTextUrl(text=label, url=source) if source else label))
    return blocks


def _poll_blocks(post: FxPost) -> list[InputRichBlockUnion]:
    poll = post.poll
    assert poll is not None
    blocks: list[InputRichBlockUnion] = [InputRichBlockSectionHeading(text="Опрос · результаты на момент загрузки", size=4)]
    for choice in poll.choices:
        blocks.extend(_paragraphs([_Span(f"{choice.percentage:g}% · {choice.label} · {choice.count} голосов")]))
    status = ""
    try:
        end = datetime.fromisoformat(poll.ends_at.replace("Z", "+00:00"))
        if end.tzinfo is not None:
            status = " · завершён" if end <= datetime.now(timezone.utc) else f" · до {end:%d.%m.%Y %H:%M} UTC"
    except ValueError:
        pass
    blocks.extend(_paragraphs([_Span(f"Всего голосов: {poll.total_votes}{status}. Голосовать можно в оригинале.")]))
    return blocks


def _article_blocks(post: FxPost, uploads: Mapping[str, _Upload]) -> list[InputRichBlockUnion]:
    article = post.article
    assert article is not None
    blocks: list[InputRichBlockUnion] = []
    if article.title:
        blocks.append(InputRichBlockSectionHeading(text=article.title, size=2))
    if article.cover_media:
        blocks.extend(_gallery([article.cover_media], uploads, post))
    media = {item.id: item for item in article.media_entities if item.id}
    entities = {entity.key: entity for entity in article.content.entity_map}
    used_media: set[str] = set()
    used_entities: set[str] = set()
    list_index = 0
    for block in article.content.blocks:
        list_index = list_index + 1 if block.type == "ordered-list-item" else 0
        if block.text.strip():
            spans = _spans(block.text, FxText(text=block.text, facets=block.facets), block.styles)
            if block.type == "code-block":
                spans = [_Span(span.text, span.url, "CODE") for span in spans]
            elif block.type == "unordered-list-item":
                spans.insert(0, _Span("• "))
            elif block.type == "ordered-list-item":
                spans.insert(0, _Span(f"{list_index}. "))
            paragraph = _paragraphs(spans)
            headings = {f"header-{name}": index for index, name in enumerate(("one", "two", "three", "four", "five", "six"), 1)}
            if block.type in headings:
                for heading in paragraph:
                    assert isinstance(heading, InputRichBlockParagraph)
                    blocks.append(InputRichBlockSectionHeading(text=heading.text, size=headings[block.type]))
            elif block.type == "blockquote":
                blocks.append(InputRichBlockBlockQuotation(blocks=paragraph))
            else:
                blocks.extend(paragraph)
        for reference in block.entities:
            key = str(reference.key)
            if key in used_entities:
                continue
            used_entities.add(key)
            entity = entities.get(key)
            if entity is None:
                blocks.append(InputRichBlockParagraph(text=RichTextUrl(text="Встроенное содержимое — в оригинале", url=post.url)))
            elif entity.kind == "MARKDOWN":
                # Untrusted embedded Markdown is literal content, never Telegram markup.
                blocks.extend(_paragraphs(_autolinks(entity.markdown)))
            elif entity.kind == "MEDIA":
                for media_id in entity.media_ids:
                    used_media.add(media_id)
                    if item := media.get(media_id):
                        blocks.extend(_gallery([item], uploads, post))
                    else:
                        blocks.append(InputRichBlockParagraph(text=RichTextUrl(text="Медиа статьи — в оригинале", url=post.url)))
            elif entity.kind == "TWEET" and entity.tweet_id:
                blocks.append(
                    InputRichBlockParagraph(text=RichTextUrl(text="Встроенная публикация", url=f"https://x.com/i/status/{entity.tweet_id}"))
                )
            else:
                blocks.append(InputRichBlockParagraph(text=RichTextUrl(text="Встроенное содержимое — в оригинале", url=post.url)))
    for item in article.media_entities:
        if item.id not in used_media and (article.cover_media is None or item.url != article.cover_media.url):
            blocks.extend(_gallery([item], uploads, post))
    if not article.has_full_content:
        if not article.content.blocks and article.preview_text:
            blocks.extend(_paragraphs(_autolinks(article.preview_text)))
        blocks.append(InputRichBlockParagraph(text=RichTextUrl(text="Доступен фрагмент статьи · полный текст в оригинале", url=post.url)))
    return blocks


def _rich_spans(text: RichTextUnion, *, url: str | None = None, style: str | None = None) -> Iterator[_Span]:
    if isinstance(text, str):
        yield _Span(text, url, style)
    elif isinstance(text, list):
        for child in text:
            yield from _rich_spans(child, url=url, style=style)
    elif isinstance(text, RichTextUrl):
        yield from _rich_spans(text.text, url=text.url, style=style)
    elif isinstance(text, RichTextCustomEmoji):
        yield _Span(text.alternative_text, url, style, text.custom_emoji_id)
    elif isinstance(text, RichTextBold | RichTextItalic | RichTextCode | RichTextStrikethrough | RichTextUnderline):
        combined = ",".join(sorted({*(style or "").split(","), text.type.upper()} - {""}))
        yield from _rich_spans(text.text, url=url, style=combined)
    else:
        raise TypeError("Unsupported X rich text")


def _cost(blocks: Sequence[InputRichBlockUnion]) -> tuple[int, int, int]:
    text = media = 0
    count = len(blocks)
    for block in blocks:
        if isinstance(block, InputRichBlockBlockQuotation | InputRichBlockCollage):
            nested = _cost(block.blocks)
            text, count, media = text + nested[0], count + nested[1], media + nested[2]
        elif isinstance(block, InputRichBlockPhoto | InputRichBlockVideo):
            media += 1
        elif isinstance(block, InputRichBlockParagraph | InputRichBlockFooter | InputRichBlockSectionHeading):
            text += sum(_units(span.text) for span in _rich_spans(block.text))
    return text, count, media


def _fits(blocks: Sequence[InputRichBlockUnion]) -> bool:
    return all(value <= maximum for value, maximum in zip(_cost(blocks), (_MAX_TEXT, _MAX_BLOCKS, _MAX_MEDIA), strict=True))


def _flat_blocks(blocks: Sequence[InputRichBlockUnion]) -> Iterator[InputRichBlockUnion]:
    for block in blocks:
        if isinstance(block, InputRichBlockBlockQuotation):
            yield InputRichBlockParagraph(text=RichTextBold(text="Цитата"))
            yield from _flat_blocks(block.blocks)
        elif isinstance(block, InputRichBlockCollage):
            yield from _flat_blocks(block.blocks)
        elif isinstance(block, InputRichBlockParagraph | InputRichBlockFooter | InputRichBlockSectionHeading):
            yield from _paragraphs(list(_rich_spans(block.text)))
        else:
            yield block


def render_x_post(post: FxPost, uploads: Mapping[str, _Upload], *, link: PostLink | None = None) -> list[InputRichMessage]:
    """Keep ordinary posts together; flatten and split only genuine overflows."""
    blocks = _post_blocks(post, uploads, link)
    if _fits(blocks):
        return [InputRichMessage(blocks=blocks, skip_entity_detection=True)]
    result: list[InputRichMessage] = []
    part: list[InputRichBlockUnion] = []
    for block in _flat_blocks(blocks):
        if part and not _fits([*part, block]):
            result.append(InputRichMessage(blocks=part, skip_entity_detection=True))
            part = []
        part.append(block)
    if part:
        result.append(InputRichMessage(blocks=part, skip_entity_detection=True))
    return result


def _all_media(post: FxPost, link: PostLink | None = None) -> Iterator[FxMedia]:
    yield from _selected(post, link)
    if post.article:
        if post.article.cover_media:
            yield post.article.cover_media
        yield from post.article.media_entities
    if isinstance(post.quote, FxPost):
        yield from _all_media(post.quote)


@dataclass(slots=True)
class _DownloadBudget:
    remaining: int = _MAX_UPLOAD_BYTES


def _variant_size(variant: FxMediaFormat, media: FxMedia) -> float | None:
    if variant.size is not None:
        return variant.size
    if variant.bitrate and variant.bitrate > 0 and media.duration and media.duration > 0:
        # FxEmbed's own Telegram selection estimates bitrate × duration; leave room for audio/container overhead.
        return variant.bitrate * media.duration / 8 * 1.1 + 65536
    return None


def _download_url(media: FxMedia, limit: int) -> str:
    primary = next((variant for variant in media.formats if variant.url == media.url), None)
    size = media.filesize if media.filesize is not None else _variant_size(primary, media) if primary else None
    if size is None or size <= limit:
        return media.url
    candidates = [
        variant
        for variant in media.formats
        if media.type != "photo"
        and variant.url != media.url
        and variant.container == "mp4"
        and variant.codec in {None, "h264"}
        and not any(codec in variant.url.lower() for codec in ("hevc", "vp9", "av1"))
        and (estimated := _variant_size(variant, media)) is not None
        and estimated <= limit
    ]
    if not candidates:
        raise ValueError("X media is outside upload limits")
    return max(candidates, key=lambda variant: (variant.bitrate or 0, (variant.width or 0) * (variant.height or 0))).url


async def _download(session: aiohttp.ClientSession, media: FxMedia, destination: Path, budget: _DownloadBudget) -> FxMediaFormat | None:
    limit = min(9 * 1024 * 1024 if media.type == "photo" else 49 * 1024 * 1024, budget.remaining)
    if limit <= 0 or not safe_media_url(media.url):
        raise ValueError("X media is outside upload limits")
    url = _download_url(media, limit)
    variant = next((item for item in media.formats if item.url == url), None)
    async with _DOWNLOADS:
        for _ in range(4):
            async with session.get(url, allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get("Location", ""))
                    if not safe_media_url(url):
                        raise ValueError("Unsupported media redirect")
                    continue
                if response.status != 200 or (response.content_length is not None and response.content_length > limit):
                    raise ValueError("X media is unavailable or too large")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                allowed = {"image/jpeg", "image/png", "image/webp"} if media.type == "photo" else {"video/mp4"}
                if content_type not in allowed:
                    raise ValueError("Unsupported X media format")
                size = 0
                with destination.open("wb") as stream:
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        budget.remaining -= len(chunk)
                        if size > limit:
                            raise ValueError("X media exceeds upload limits")
                        stream.write(chunk)
                if not size:
                    raise ValueError("Empty X media")
                return variant
    raise ValueError("Too many media redirects")


def _plain(blocks: Sequence[InputRichBlockUnion]) -> str:
    text: list[str] = []
    for block in _flat_blocks(blocks):
        if isinstance(block, InputRichBlockParagraph | InputRichBlockFooter | InputRichBlockSectionHeading):
            text.append(
                "".join(span.text + (f" ({span.url})" if span.url and span.url != span.text else "") for span in _rich_spans(block.text))
            )
        elif isinstance(block, InputRichBlockPhoto | InputRichBlockVideo):
            text.append("Медиа — в оригинале.")
    return "\n\n".join(text)


def _can_fallback(error: TelegramBadRequest) -> bool:
    reason = error.message.lower()
    return any(word in reason for word in ("rich", "media", "photo", "video", "file", "entity", "text is too long"))


def _without_custom_emoji(blocks: Sequence[InputRichBlockUnion]) -> list[InputRichBlockUnion]:
    result: list[InputRichBlockUnion] = []
    for block in blocks:
        if isinstance(block, InputRichBlockBlockQuotation | InputRichBlockCollage):
            block = block.model_copy(update={"blocks": _without_custom_emoji(block.blocks)})
        elif isinstance(block, InputRichBlockParagraph | InputRichBlockFooter | InputRichBlockSectionHeading):
            text = [replace(span, custom_emoji_id=None).rich() for span in _rich_spans(block.text)]
            block = block.model_copy(update={"text": text})
        result.append(block)
    return result


async def publish_x_post(
    post: FxPost,
    bot: BotWrapper,
    chat_id: int,
    reply_to: int | None = None,
    *,
    message_thread_id: int | None = None,
    link: PostLink | None = None,
) -> Message | None:
    """Download only public X CDN media; never repeat an ambiguous Telegram send."""
    uploads: dict[str, _Upload] = {}
    with TemporaryDirectory(prefix="msu-x-") as directory:
        async with aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=40, connect=10)
        ) as session:
            budget = _DownloadBudget()
            seen: set[str] = set()
            try:
                async with asyncio.timeout(90):
                    for index, media in enumerate(_all_media(post, link)):
                        if media.url in seen:
                            continue
                        seen.add(media.url)
                        suffix = ".jpg" if media.type == "photo" else ".mp4"
                        path = Path(directory) / f"{index}{suffix}"
                        try:
                            variant = await _download(session, media, path, budget)
                        except aiohttp.ClientError, TimeoutError, OSError, ValueError:
                            path.unlink(missing_ok=True)
                            continue
                        file = FSInputFile(path)
                        uploads[media.url] = file if media.type == "photo" else _video_upload(file, media, variant)
            except TimeoutError:
                pass
        result = None
        async with bot.serial_send(chat_id):
            for message in render_x_post(post, uploads, link=link):
                reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to is not None else None
                for custom_emoji in (True, False):
                    try:
                        result = await bot.send_rich_message(
                            chat_id=chat_id, rich_message=message, reply_parameters=reply, message_thread_id=message_thread_id
                        )
                    except TelegramBadRequest as error:
                        if custom_emoji and any(word in error.message.lower() for word in ("custom emoji", "custom_emoji")):
                            # A definite rejection permits one retry if the bot loses emoji eligibility.
                            message = message.model_copy(update={"blocks": _without_custom_emoji(message.blocks or [])})
                            continue
                        if not _can_fallback(error):
                            raise
                        for text in _pieces(_plain(message.blocks or []), 4096):
                            if not text.strip():
                                continue
                            result = await bot.send_message(
                                chat_id=chat_id,
                                text=text,
                                parse_mode=None,
                                link_preview_options=LinkPreviewOptions(is_disabled=True),
                                reply_parameters=reply,
                                message_thread_id=message_thread_id,
                            )
                            reply = ReplyParameters(message_id=result.message_id, allow_sending_without_reply=True)
                    break
                if result:
                    reply_to = result.message_id
        return result
