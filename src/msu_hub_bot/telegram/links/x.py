"""Native X posts: explicit rich blocks, bounded uploads and ordered delivery."""

import asyncio
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp
from aiogram.types import (
    FSInputFile,
    InputFile,
    InputMediaPhoto,
    InputMediaVideo,
    InputRichBlockBlockQuotation,
    InputRichBlockCollage,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockSectionHeading,
    InputRichBlockUnion,
    InputRichBlockVideo,
    InputRichMessage,
    Message,
    RichTextBold,
    RichTextUrl,
)

from msu_hub_bot.providers.fxembed import FxMedia, FxMediaFormat, FxPost, FxStyleRange, FxText, PostLink, safe_media_url, safe_public_url
from msu_hub_bot.providers.http import USER_AGENT
from msu_hub_bot.telegram.links.rich import _paragraphs, _Span, _units, publish_messages, split_messages
from msu_hub_bot.telegram.wrapper import BotWrapper
from msu_hub_bot.telemetry import Boundary, MediaKind, MediaReason, Provider, Telemetry

_MAX_VIDEO_SIDE = 10000
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
_PREPARE_TIMEOUT = 90
_X_EMOJI_ID = "5422502846648039176"
_X_EMOJI_FALLBACK = "💬"
_DOWNLOADS = asyncio.Semaphore(2)
_LINK = re.compile(r"https?://[^\s<>]+|(?<![\w@])@[A-Za-z0-9_]{1,15}\b")
_MEDIA_LINK = re.compile(r"(?:https?://)?(?:t\.co|(?:pic\.)?(?:x\.com|twitter\.com))/[^\s<>]+", re.IGNORECASE)
_STYLES = {"BOLD", "ITALIC", "CODE", "STRIKETHROUGH", "UNDERLINE"}

type _Upload = InputFile | InputMediaVideo


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


def _author(post: FxPost) -> list[_Span]:
    return [
        _Span(_X_EMOJI_FALLBACK, custom_emoji_id=_X_EMOJI_ID),
        _Span(" "),
        _Span(post.author.name, style="BOLD"),
        _Span(" · "),
        _Span("@" + post.author.screen_name, post.author.url),
        _Span(" · "),
        _Span("↗", post.url),
    ]


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
    intro = _author(post)
    if post.replying_to:
        intro.extend([_Span("\n\n"), _Span(f"↪ В ответ @{post.replying_to.screen_name}", post.replying_to.url)])
    media = _selected(post, link)
    shown = frozenset(item.id for item in media if item.id and item.url in uploads)
    body = _spans(post.text, post.raw_text, unescape=True, shown_media_ids=shown)
    if body:
        # Clients trim paragraph edges; keep the visible separator inside the text.
        intro.extend([_Span("\n\n"), *body])
    blocks = _paragraphs(intro)
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


def render_x_post(post: FxPost, uploads: Mapping[str, _Upload], *, link: PostLink | None = None) -> list[InputRichMessage]:
    """Keep ordinary posts together; flatten and split only genuine overflows."""
    blocks = _post_blocks(post, uploads, link)
    return split_messages(blocks)


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
    requests: int = 0
    reduced_photos: int = 0


class _DownloadRejected(ValueError):
    def __init__(self, reason: MediaReason) -> None:
        self.reason = reason
        message = "X media is outside upload limits" if reason in {MediaReason.OVERSIZE, MediaReason.BUDGET_EXHAUSTED} else reason.value
        super().__init__(message)


def _smaller_photo_url(media: FxMedia) -> str | None:
    """Change only recognized original X photo URLs to the CDN's large rendition."""
    if media.type != "photo" or not safe_media_url(media.url):
        return None
    parsed = urlsplit(media.url)
    if parsed.hostname != "pbs.twimg.com":
        return None
    match = re.fullmatch(
        r"/media/[A-Za-z0-9_-]+(?:\.(?P<format>jpg|jpeg|png|webp))?(?::(?P<size>orig|large|medium|small|thumb))?", parsed.path
    )
    if match is None:
        return None
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    params = dict(pairs)
    if len(params) != len(pairs) or params.keys() - {"format", "name"}:
        return None
    # An unspecified CDN size may already be smaller than large.
    if params.get("name", match["size"]) != "orig" or match["size"] not in {None, "orig"}:
        return None
    if params.get("format", match["format"]) not in {"jpg", "jpeg", "png", "webp"}:
        return None
    path = parsed.path.removesuffix(":" + match["size"]) if match["size"] else parsed.path
    params["name"] = "large"
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(params), ""))


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
        raise _DownloadRejected(MediaReason.OVERSIZE)
    return max(candidates, key=lambda variant: (variant.bitrate or 0, (variant.width or 0) * (variant.height or 0))).url


async def _download_once(session: aiohttp.ClientSession, media: FxMedia, url: str, destination: Path, budget: _DownloadBudget) -> None:
    limit = min(9 * 1024 * 1024 if media.type == "photo" else 49 * 1024 * 1024, budget.remaining)
    if limit <= 0:
        raise _DownloadRejected(MediaReason.BUDGET_EXHAUSTED)
    for _ in range(4):
        budget.requests += 1
        async with session.get(url, allow_redirects=False) as response:
            if response.status in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get("Location", ""))
                if not safe_media_url(url):
                    raise _DownloadRejected(MediaReason.REDIRECT_REJECTED)
                continue
            if response.status == 413:
                raise _DownloadRejected(MediaReason.OVERSIZE)
            if response.status != 200:
                raise _DownloadRejected(MediaReason.HTTP_ERROR)
            if response.content_length is not None and response.content_length > limit:
                raise _DownloadRejected(MediaReason.OVERSIZE)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            allowed = {"image/jpeg", "image/png", "image/webp"} if media.type == "photo" else {"video/mp4"}
            if content_type not in allowed:
                raise _DownloadRejected(MediaReason.UNSUPPORTED_FORMAT)
            size = 0
            with destination.open("wb") as stream:
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    budget.remaining -= len(chunk)
                    if size > limit:
                        reason = MediaReason.BUDGET_EXHAUSTED if budget.remaining <= 0 else MediaReason.OVERSIZE
                        raise _DownloadRejected(reason)
                    stream.write(chunk)
            if not size:
                raise _DownloadRejected(MediaReason.EMPTY)
            return
    raise _DownloadRejected(MediaReason.REDIRECT_REJECTED)


async def _download(session: aiohttp.ClientSession, media: FxMedia, destination: Path, budget: _DownloadBudget) -> FxMediaFormat | None:
    if budget.remaining <= 0:
        raise _DownloadRejected(MediaReason.BUDGET_EXHAUSTED)
    if not safe_media_url(media.url):
        raise _DownloadRejected(MediaReason.REDIRECT_REJECTED)
    async with _DOWNLOADS:
        try:
            limit = min(9 * 1024 * 1024 if media.type == "photo" else 49 * 1024 * 1024, budget.remaining)
            url = _download_url(media, limit)
            await _download_once(session, media, url, destination, budget)
        except _DownloadRejected as error:
            fallback = _smaller_photo_url(media) if error.reason is MediaReason.OVERSIZE else None
            if fallback is None or budget.remaining <= 0:
                raise
            destination.unlink(missing_ok=True)
            budget.reduced_photos += 1
            await _download_once(session, media, fallback, destination, budget)
            return None
    return next((item for item in media.formats if item.url == url), None)


async def _prepare_uploads(post: FxPost, link: PostLink | None, directory: Path, telemetry: Telemetry) -> dict[str, _Upload]:
    items: dict[str, FxMedia] = {}
    for media in _all_media(post, link):
        items.setdefault(media.url, media)
    if not items:
        return {}
    uploads: dict[str, _Upload] = {}
    with telemetry.operation(Boundary.MEDIA, "x.media.prepare", provider=Provider.FXEMBED) as preparation:
        async with aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=40, connect=10), trust_env=False
        ) as session:
            budget = _DownloadBudget()
            deadline = asyncio.timeout(_PREPARE_TIMEOUT)
            pending = iter(enumerate(items.values()))
            try:
                async with deadline:
                    for index, media in pending:
                        path = directory / f"{index}{'.jpg' if media.type == 'photo' else '.mp4'}"
                        started = time.monotonic()
                        before = budget.remaining, budget.requests, budget.reduced_photos
                        reason = MediaReason.READY
                        try:
                            variant = await _download(session, media, path, budget)
                            file = FSInputFile(path)
                            uploads[media.url] = file if media.type == "photo" else _video_upload(file, media, variant)
                            if budget.reduced_photos > before[2]:
                                reason = MediaReason.READY_REDUCED
                        except _DownloadRejected as error:
                            reason = error.reason
                        except TimeoutError:
                            reason = MediaReason.TIMEOUT
                        except aiohttp.ClientError:
                            reason = MediaReason.NETWORK
                        except OSError:
                            reason = MediaReason.IO_ERROR
                        except ValueError:
                            reason = MediaReason.UNSUPPORTED_FORMAT
                        except asyncio.CancelledError:
                            reason = MediaReason.TIMEOUT if deadline.expired() else MediaReason.CANCELLED
                            raise
                        finally:
                            preparation.media_asset(
                                MediaKind(media.type),
                                reason,
                                attempts=budget.requests - before[1],
                                downloaded_bytes=before[0] - budget.remaining,
                                duration=time.monotonic() - started,
                            )
                            if reason not in {MediaReason.READY, MediaReason.READY_REDUCED}:
                                path.unlink(missing_ok=True)
            except (TimeoutError, asyncio.CancelledError) as error:
                reason = MediaReason.TIMEOUT if isinstance(error, TimeoutError) else MediaReason.CANCELLED
                for _, media in pending:
                    preparation.media_asset(MediaKind(media.type), reason, attempts=0, downloaded_bytes=0, duration=0)
                if isinstance(error, asyncio.CancelledError):
                    raise
    return uploads


async def publish_x_post(
    post: FxPost,
    bot: BotWrapper,
    chat_id: int,
    reply_to: int | None = None,
    *,
    message_thread_id: int | None = None,
    link: PostLink | None = None,
    telemetry: Telemetry | None = None,
) -> Message | None:
    """Download only public X CDN media; never repeat an ambiguous Telegram send."""
    with TemporaryDirectory(prefix="msu-x-") as directory:
        uploads = await _prepare_uploads(post, link, Path(directory), telemetry or Telemetry())
        return await publish_messages(
            render_x_post(post, uploads, link=link),
            bot,
            chat_id,
            reply_to,
            message_thread_id=message_thread_id,
        )
