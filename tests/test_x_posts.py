"""Native X output keeps content, attribution and Telegram delivery boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramServerError
from aiogram.methods import SendRichMessage
from aiogram.types import (
    BufferedInputFile,
    InputRichBlockBlockQuotation,
    InputRichBlockCollage,
    InputRichBlockFooter,
    InputRichBlockPhoto,
    InputRichBlockVideo,
    RichTextBold,
)

from msu_hub_bot.providers.fxembed import FxMedia, FxPost, FxText, parse_post_url
from msu_hub_bot.telegram import x_posts
from msu_hub_bot.telegram.wrapper import BotWrapper
from telegram_helpers import RecordingSession, make_message


def post(**changes):
    return FxPost.model_validate(
        {
            "id": "123",
            "type": "status",
            "text": "Полный текст <b>не HTML</b> 😀",
            "author": {"name": "Космокот", "screen_name": "cosmocat"},
            **changes,
        }
    )


def media(index, kind="photo", **changes):
    return FxMedia.model_validate(
        {
            "id": str(index),
            "type": kind,
            "url": f"https://{'pbs' if kind == 'photo' else 'video'}.twimg.com/{index}.{'jpg' if kind == 'photo' else 'mp4'}",
            "width": 800,
            "height": 600,
            **changes,
        }
    )


def uploads(items):
    return {item.url: BufferedInputFile(b"synthetic", filename=str(index)) for index, item in enumerate(items)}


def blocks(messages):
    for message in messages:
        yield from message.blocks


def all_blocks(items):
    for block in items:
        yield block
        if isinstance(block, InputRichBlockBlockQuotation | InputRichBlockCollage):
            yield from all_blocks(block.blocks)


def text_of(items):
    return "\n\n".join(
        "".join(span.text for span in x_posts._rich_spans(block.text)) for block in all_blocks(items) if hasattr(block, "text")
    )


def links_of(items):
    return [span.url for block in all_blocks(items) if hasattr(block, "text") for span in x_posts._rich_spans(block.text) if span.url]


def test_native_layout_keeps_order_quote_media_and_clean_footer():
    items = [media(1), media(2, "video"), media(3)]
    quoted = post(id="321", text="На Луну", media={"all": [items[2]]})
    value = post(media={"all": items[:2]}, quote=quoted)
    messages = x_posts.render_x_post(value, uploads(items))
    assert len(messages) == 1
    assert messages[0].skip_entity_detection is True
    result = messages[0].blocks
    assert text_of(result[:2]) == "Космокот · @cosmocat\n\n" + value.text
    assert isinstance(result[2], InputRichBlockCollage)
    assert [item.type for item in result[2].blocks] == ["photo", "video"]
    assert isinstance(result[3], InputRichBlockBlockQuotation)
    assert any(isinstance(item, InputRichBlockPhoto) for item in result[3].blocks)
    assert isinstance(result[-1], InputRichBlockFooter)
    assert links_of(result)[-1] == value.url
    assert "Статистика" not in text_of(result)


def test_utf16_facets_preserve_emoji_and_link_mentions_to_x():
    raw = FxText.model_validate(
        {
            "text": "😀 @cat https://t.co/a tail",
            "facets": [
                {"type": "mention", "indices": [3, 7]},
                {"type": "url", "indices": [8, 22], "replacement": "https://example.org/page", "display": "example.org/page"},
            ],
        }
    )
    value = post(text="short display", raw_text=raw)
    result = x_posts.render_x_post(value, {})
    assert "😀 @cat example.org/page tail" in text_of(blocks(result))
    assert "https://x.com/cat" in links_of(blocks(result))
    assert "https://example.org/page" in links_of(blocks(result))


def test_no_facet_post_uses_full_raw_body_and_safe_explicit_links():
    value = post(text="summary", raw_text={"text": "full @alice <script> https://example.org/a.\n@b /start"})
    result = x_posts.render_x_post(value, {})
    assert value.raw_text.text in text_of(blocks(result))
    assert "https://x.com/alice" in links_of(blocks(result))
    assert "https://example.org/a" in links_of(blocks(result))
    assert all(message.skip_entity_detection for message in result)


def test_unknown_or_unsafe_facets_never_inject_markup_or_hide_original_text():
    raw = {
        "text": "first second",
        "facets": [
            {"type": "future", "indices": [0, 5], "replacement": "hidden"},
            {"type": "url", "indices": [6, 12], "replacement": "javascript:alert(1)", "display": "second"},
        ],
    }
    result = x_posts.render_x_post(post(raw_text=raw), {})
    assert "first second" in text_of(blocks(result))
    assert not any(link.startswith("javascript:") for link in links_of(blocks(result)))


@pytest.mark.parametrize("length", [32700, 32768, 70000])
@pytest.mark.parametrize("character", ["я", "😀"], ids=["cyrillic", "astral"])
def test_long_posts_preserve_every_character_and_all_message_limits(length, character):
    original = character * length
    result = x_posts.render_x_post(post(text=original, raw_text={"text": original, "display_text_range": [0, 10]}), {})
    body = "".join(
        span.text
        for message in result
        for block in message.blocks
        if hasattr(block, "text")
        for span in x_posts._rich_spans(block.text)
        if span.text and all(char == character for char in span.text)
    )
    assert body == original
    assert all(x_posts._fits(message.blocks) for message in result)
    assert links_of(blocks(result))[-1].endswith("/status/123")


def test_many_media_split_at_budget_without_losing_order():
    items = [media(index) for index in range(103)]
    result = x_posts.render_x_post(post(media={"all": items}), uploads(items))
    assert len(result) == 3
    attached = [block.photo.media.filename for block in all_blocks(blocks(result)) if isinstance(block, InputRichBlockPhoto)]
    assert attached == [str(index) for index in range(103)]
    assert all(x_posts._cost(message.blocks)[2] <= 50 for message in result)


def test_many_article_blocks_keep_all_text_within_block_limit():
    texts = [f"line {index}" for index in range(1200)]
    result = x_posts.render_x_post(post(article={"title": "Article", "content": {"blocks": [{"text": line} for line in texts]}}), {})
    rendered = text_of(blocks(result))
    assert all(line in rendered for line in texts)
    assert all(x_posts._cost(message.blocks)[1] <= 500 for message in result)


def test_deep_quotes_are_flattened_without_losing_attribution():
    value = post(text="innermost")
    for index in range(25):
        value = post(id=str(index + 1000), text=f"quotation {index}", quote=value)
    result = x_posts.render_x_post(value, {})
    assert "innermost" in text_of(blocks(result))
    assert len([link for link in links_of(blocks(result)) if "/status/" in link]) == 26

    def depth(items):
        return max(
            (1 + depth(block.blocks) if isinstance(block, InputRichBlockBlockQuotation | InputRichBlockCollage) else 1 for block in items),
            default=0,
        )

    assert max(depth(message.blocks) for message in result) <= 8


def test_selected_media_does_not_hide_the_quote_or_body():
    items = [media(1), media(2, "video"), media(3), media(4)]
    value = post(media={"all": items[:3]}, quote=post(id="999", media={"all": items[3:]}))
    result = x_posts.render_x_post(value, uploads(items), link=parse_post_url("https://x.com/cat/status/123/photo/3"))
    attached = [block.photo.media.filename for block in all_blocks(blocks(result)) if isinstance(block, InputRichBlockPhoto)]
    assert attached == ["2", "3"]
    assert value.text in text_of(blocks(result))


def test_media_selectors_use_original_mixed_media_positions():
    items = [media(1), media(2, "video"), media(3)]
    result = x_posts.render_x_post(post(media={"all": items}), uploads(items), link=parse_post_url("https://x.com/cat/status/123/video/2"))
    attached = [block for block in all_blocks(blocks(result)) if isinstance(block, InputRichBlockPhoto | InputRichBlockVideo)]
    assert len(attached) == 1
    assert isinstance(attached[0], InputRichBlockVideo)
    assert attached[0].video.media.filename == "1"


def test_invalid_media_cannot_shift_the_selected_attachment():
    item = media(2)
    value = post(media={"all": [{"type": "photo", "url": "https://evil.example/image"}, item]})
    result = x_posts.render_x_post(value, uploads([item]), link=parse_post_url("https://x.com/cat/status/123/photo/1"))
    assert not any(isinstance(block, InputRichBlockPhoto) for block in all_blocks(blocks(result)))


def test_raw_entities_are_decoded_after_utf16_facet_slicing_and_styles_overlap_links():
    body = "😀 &amp; @cat #Кот $CAT"
    value = post(
        raw_text={
            "text": body,
            "facets": [
                {"type": "bold", "indices": [0, x_posts._units(body)]},
                {"type": "italic", "indices": [9, 13]},
                {"type": "mention", "indices": [9, 13]},
                {"type": "hashtag", "indices": [14, 18], "original": "Кот"},
                {"type": "symbol", "indices": [19, 23], "original": "CAT"},
            ],
        }
    )
    result = x_posts.render_x_post(value, {})
    assert "😀 & @cat #Кот $CAT" in text_of(blocks(result))
    assert "https://x.com/cat" in links_of(blocks(result))
    assert "https://x.com/hashtag/%D0%9A%D0%BE%D1%82" in links_of(blocks(result))
    assert "https://x.com/search?q=%24CAT" in links_of(blocks(result))
    spans = list(x_posts._rich_spans(result[0].blocks[1].text))
    assert all("BOLD" in (span.style or "") for span in spans)
    assert next(span for span in spans if span.text == "@cat").style == "BOLD,ITALIC"


def test_note_tweet_scalar_offsets_reach_renderer_as_utf16_without_losing_emoji_or_links():
    body = "😀Hello @cat tail"
    start = body.index("@cat")
    value = post(
        is_note_tweet=True,
        raw_text={
            "text": body,
            "facets": [
                {"type": "bold", "indices": [0, len(body)]},
                {"type": "mention", "indices": [start, start + len("@cat")]},
            ],
        },
    )
    rendered = x_posts.render_x_post(value, {})
    assert body in text_of(blocks(rendered))
    assert "https://x.com/cat" in links_of(blocks(rendered))
    spans = list(x_posts._rich_spans(rendered[0].blocks[1].text))
    assert next(span for span in spans if span.text == "@cat").style == "BOLD"


def test_unavailable_media_keep_native_text_and_attributed_source_links():
    result = x_posts.render_x_post(
        post(media={"all": [media(1), media(2, "video")]}, quote={"type": "tombstone", "url": "https://x.com/cat/status/999"}), {}
    )
    output = text_of(blocks(result))
    assert "Фото — в оригинале" in output
    assert "Видео — в оригинале" in output
    assert "Цитируемая публикация недоступна" in output
    assert "https://x.com/i/status/999" in links_of(blocks(result))


def test_sensitive_media_are_spoilers():
    items = [media(1), media(2, "gif")]
    result = x_posts.render_x_post(post(possibly_sensitive=True, media={"all": items}), uploads(items))
    assert all(
        (block.photo if isinstance(block, InputRichBlockPhoto) else block.video).has_spoiler
        for block in all_blocks(blocks(result))
        if isinstance(block, InputRichBlockPhoto | InputRichBlockVideo)
    )


def test_poll_notes_and_reply_attribution_are_visible():
    result = x_posts.render_x_post(
        post(
            poll={
                "choices": [{"label": "Да", "count": 3, "percentage": 75}, {"label": "Нет", "count": 1, "percentage": 25}],
                "total_votes": 4,
                "ends_at": "2020-01-01T00:00:00Z",
            },
            community_note={"text": "Важный контекст https://example.org/source"},
            replying_to={"screen_name": "cat", "status": "999"},
        ),
        {},
    )
    output = text_of(blocks(result))
    assert "75% · Да · 3 голосов" in output
    assert "Всего голосов: 4 · завершён" in output
    assert "Контекст от сообщества" in output
    assert "Важный контекст" in output
    assert "https://x.com/cat/status/999" in links_of(blocks(result))


def test_article_body_embedded_media_markdown_and_partial_styles_survive():
    image = media(1)
    article = {
        "title": "Заголовок",
        "content": {
            "blocks": [
                {"text": "😀 bold plain", "inlineStyleRanges": [{"offset": 3, "length": 4, "style": "Bold"}]},
                {"text": " ", "entityRanges": [{"offset": 0, "length": 1, "key": 0}, {"offset": 0, "length": 1, "key": 1}]},
            ],
            "entityMap": [
                {"key": "0", "kind": "MEDIA", "media_ids": ["1"]},
                {"key": "1", "kind": "MARKDOWN", "markdown": "**Keep all content**, including <a href='tg://user?id=42'>markup</a>"},
            ],
        },
        "media_entities": [image],
    }
    result = x_posts.render_x_post(post(article=article), uploads([image]))
    output = text_of(blocks(result))
    assert "😀 bold plain" in output
    assert "**Keep all content**" in output
    assert "markup</a>" in output
    paragraph = list(blocks(result))[3]
    assert any(isinstance(piece, RichTextBold) for piece in paragraph.text)
    assert len([block for block in all_blocks(blocks(result)) if isinstance(block, InputRichBlockPhoto)]) == 1
    assert "Доступен фрагмент" not in output


def test_excerpt_is_honestly_labelled_and_complete_available_text_is_kept():
    result = x_posts.render_x_post(post(article={"title": "Article", "preview_text": "Only excerpt"}), {})
    assert "Only excerpt" in text_of(blocks(result))
    assert "Доступен фрагмент статьи" in text_of(blocks(result))


@pytest.mark.asyncio
async def test_rich_delivery_keeps_reply_topic_and_cleans_uploads(monkeypatch):
    paths = []

    async def download(session, item, path, budget):
        path.write_bytes(b"synthetic")
        paths.append(path)

    monkeypatch.setattr(x_posts, "_download", download)
    session = RecordingSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    await x_posts.publish_x_post(post(media={"all": [media(1)]}), bot, -10042, 12, message_thread_id=73)
    assert len(session.methods) == 1
    method = session.methods[0]
    assert isinstance(method, SendRichMessage)
    assert method.chat_id == -10042
    assert method.message_thread_id == 73
    assert method.reply_parameters.message_id == 12
    assert paths and all(not path.exists() for path in paths)


@pytest.mark.asyncio
async def test_definite_media_rejection_falls_back_to_complete_text(monkeypatch):
    session = RecordingSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    error = TelegramBadRequest(
        method=SendRichMessage(chat_id=1, rich_message=x_posts.render_x_post(post(), {})[0]),
        message="Bad Request: not enough rights to send photos",
    )
    monkeypatch.setattr(bot, "send_rich_message", AsyncMock(side_effect=error))
    value = post(text="full body " * 700)
    await x_posts.publish_x_post(value, bot, -10042, 12, message_thread_id=73)
    assert "full body " * 700 in "".join(method.text for method in session.methods)
    assert value.url in "".join(method.text for method in session.methods)
    assert all(method.parse_mode is None and method.link_preview_options.is_disabled for method in session.methods)
    assert all(method.message_thread_id == 73 for method in session.methods)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [TelegramNetworkError, TelegramServerError, TimeoutError])
async def test_uncertain_send_never_falls_back_or_duplicates(monkeypatch, error_type):
    session = RecordingSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    method = SendRichMessage(chat_id=1, rich_message=x_posts.render_x_post(post(), {})[0])
    error = TimeoutError() if error_type is TimeoutError else error_type(method=method, message="uncertain")
    monkeypatch.setattr(bot, "send_rich_message", AsyncMock(side_effect=error))
    with pytest.raises(error_type):
        await x_posts.publish_x_post(post(), bot, -10042, 12)
    assert not session.methods
    bot.send_rich_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_not_found_is_not_retried_as_a_format_failure(monkeypatch):
    bot = BotWrapper("123456789:" + "a" * 35, session=RecordingSession())
    method = SendRichMessage(chat_id=1, rich_message=x_posts.render_x_post(post(), {})[0])
    monkeypatch.setattr(bot, "send_rich_message", AsyncMock(side_effect=TelegramBadRequest(method=method, message="chat not found")))
    with pytest.raises(TelegramBadRequest):
        await x_posts.publish_x_post(post(), bot, 1)
    assert not bot.session.methods


@pytest.mark.asyncio
async def test_partial_send_fallback_does_not_repeat_already_delivered_chunks(monkeypatch):
    session = RecordingSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    original = post(text="first-half" * 4000)
    method = SendRichMessage(chat_id=1, rich_message=x_posts.render_x_post(original, {})[0])
    monkeypatch.setattr(
        bot,
        "send_rich_message",
        AsyncMock(
            side_effect=[
                make_message(message_id=90),
                TelegramBadRequest(method=method, message="rich message rejected"),
                make_message(message_id=92),
            ]
        ),
    )
    await x_posts.publish_x_post(original, bot, 1, 12)
    assert bot.send_rich_message.await_args_list[1].kwargs["reply_parameters"].message_id == 90
    remaining = x_posts._plain(x_posts.render_x_post(original, {})[1].blocks)
    assert "".join(item.text for item in session.methods) == remaining


@pytest.mark.asyncio
async def test_concurrent_posts_share_the_existing_chat_delivery_lock(monkeypatch):
    bot = BotWrapper("123456789:" + "a" * 35, session=RecordingSession())
    calls = []

    async def send(**kwargs):
        calls.append(asyncio.current_task().get_name())
        await asyncio.sleep(0)
        return make_message(message_id=len(calls))

    monkeypatch.setattr(bot, "send_rich_message", send)
    value = post(text="a" * 70000)
    await asyncio.gather(
        asyncio.create_task(x_posts.publish_x_post(value, bot, 1), name="first"),
        asyncio.create_task(x_posts.publish_x_post(value, bot, 1), name="second"),
    )
    assert calls == ["first"] * 4 + ["second"] * 4
    assert not bot._chat_sends


class Response:
    def __init__(self, *, status=200, headers=None, chunks=(b"image",), content_length=None):
        self.status = status
        self.headers = {"Content-Type": "image/jpeg", **(headers or {})}
        self.content_length = content_length
        self.chunks = chunks
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def iter_chunked(self, _):
        for chunk in self.chunks:
            yield chunk


def http_session(*responses):
    pending = iter(responses)
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return next(pending)

    return SimpleNamespace(get=get, calls=calls)


@pytest.mark.asyncio
async def test_media_redirects_are_revalidated_before_following(tmp_path):
    session = http_session(Response(status=302, headers={"Location": "http://127.0.0.1/private"}))
    with pytest.raises(ValueError, match="redirect"):
        await x_posts._download(session, media(1), tmp_path / "media", x_posts._DownloadBudget())
    assert len(session.calls) == 1
    assert session.calls[0][1] == {"allow_redirects": False}


@pytest.mark.asyncio
async def test_supported_redirect_and_bounded_media_stream(tmp_path):
    session = http_session(Response(status=302, headers={"Location": "/image.jpg"}), Response(chunks=[b"ab", b"cd"]))
    target = tmp_path / "media"
    budget = x_posts._DownloadBudget(10)
    await x_posts._download(session, media(1), target, budget)
    assert target.read_bytes() == b"abcd"
    assert budget.remaining == 6
    assert session.calls[1][0] == "https://pbs.twimg.com/image.jpg"


@pytest.mark.asyncio
async def test_failed_streams_still_consume_the_aggregate_budget(tmp_path):
    session = http_session(Response(chunks=[b"ab", b"cdef"]))
    budget = x_posts._DownloadBudget(4)
    with pytest.raises(ValueError, match="upload limits"):
        await x_posts._download(session, media(1), tmp_path / "media", budget)
    assert budget.remaining <= 0
    with pytest.raises(ValueError, match="upload limits"):
        await x_posts._download(session, media(2), tmp_path / "other", budget)
    assert len(session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response", [Response(content_length=100), Response(headers={"Content-Type": "text/html"}), Response(chunks=[]), Response(status=404)]
)
async def test_unusable_media_is_rejected_before_telegram_upload(tmp_path, response):
    with pytest.raises(ValueError):
        await x_posts._download(http_session(response), media(1), tmp_path / "media", x_posts._DownloadBudget(10))


@pytest.mark.asyncio
async def test_oversize_video_chooses_one_compatible_variant_without_retrying(tmp_path):
    mib = 1024 * 1024
    value = media(
        1,
        "video",
        filesize=100 * mib,
        formats=[
            {"url": "https://video.twimg.com/too-big.mp4", "container": "mp4", "codec": "h264", "size": 60 * mib, "bitrate": 10_000_000},
            {"url": "https://video.twimg.com/hevc.mp4", "container": "mp4", "codec": "hevc", "size": 10 * mib, "bitrate": 9_000_000},
            {"url": "https://video.twimg.com/stream.m3u8", "container": "m3u8", "codec": "h264", "size": 10 * mib, "bitrate": 8_000_000},
            {"url": "https://video.twimg.com/sd.mp4", "container": "mp4", "codec": "h264", "size": 10 * mib, "bitrate": 1_000_000},
            {"url": "https://video.twimg.com/hd.mp4", "container": "mp4", "codec": "h264", "size": 20 * mib, "bitrate": 3_000_000},
        ],
    )
    session = http_session(Response(headers={"Content-Type": "video/mp4"}, chunks=[b"video"]))
    budget = x_posts._DownloadBudget(25 * mib)
    await x_posts._download(session, value, tmp_path / "video", budget)
    assert [call[0] for call in session.calls] == ["https://video.twimg.com/hd.mp4"]
    assert budget.remaining == 25 * mib - 5


def test_video_size_estimation_uses_duration_and_preserves_aggregate_budget():
    value = media(
        1,
        "video",
        duration=120,
        formats=[
            {"url": "https://video.twimg.com/1.mp4", "container": "mp4", "codec": "h264", "bitrate": 20_000_000},
            {"url": "https://video.twimg.com/smaller.mp4", "container": "mp4", "codec": "h264", "bitrate": 1_000_000},
        ],
    )
    assert x_posts._download_url(value, 49 * 1024 * 1024) == "https://video.twimg.com/smaller.mp4"
    with pytest.raises(ValueError, match="upload limits"):
        x_posts._download_url(value, 1 * 1024 * 1024)


def test_article_lists_keep_bullets_and_restart_numbering_after_paragraphs():
    value = post(
        article={
            "content": {
                "blocks": [
                    {"type": "ordered-list-item", "text": "One"},
                    {"type": "ordered-list-item", "text": "Two"},
                    {"type": "unstyled", "text": "Break"},
                    {"type": "ordered-list-item", "text": "Restart"},
                    {"type": "unordered-list-item", "text": "Bullet"},
                ]
            }
        }
    )
    rendered = text_of(blocks(x_posts.render_x_post(value, {})))
    assert "1. One\n\n2. Two\n\nBreak\n\n1. Restart\n\n• Bullet" in rendered
