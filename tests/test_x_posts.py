"""Native X output keeps content, attribution and Telegram delivery boundaries."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramServerError
from aiogram.methods import SendRichMessage
from aiogram.types import (
    BufferedInputFile,
    InputMediaVideo,
    InputRichBlockBlockQuotation,
    InputRichBlockCollage,
    InputRichBlockFooter,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockVideo,
    RichTextBold,
    RichTextCustomEmoji,
    RichTextUrl,
)
from telegram_helpers import RecordingSession, make_message
from telemetry_helpers import Capture, config

from msu_hub_bot.providers.fxembed import FxMedia, FxMediaFormat, FxPost, FxText, parse_post_url
from msu_hub_bot.telegram.links import rich
from msu_hub_bot.telegram.links import x as x_posts
from msu_hub_bot.telegram.wrapper import BotWrapper
from msu_hub_bot.telemetry import MediaReason, Telemetry


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
    return "\n\n".join("".join(span.text for span in rich._rich_spans(block.text)) for block in all_blocks(items) if hasattr(block, "text"))


def links_of(items):
    return [span.url for block in all_blocks(items) if hasattr(block, "text") for span in rich._rich_spans(block.text) if span.url]


def test_native_layout_keeps_compact_linked_header_quote_media_and_no_footer():
    items = [media(1), media(2, "video"), media(3)]
    quoted = post(id="321", text="На Луну", author={"name": "Лунный кот", "screen_name": "mooncat"}, media={"all": [items[2]]})
    value = post(media={"all": items[:2]}, quote=quoted)
    messages = x_posts.render_x_post(value, uploads(items))
    assert len(messages) == 1
    assert messages[0].skip_entity_detection is True
    result = messages[0].blocks
    header = result[0].text
    assert isinstance(header[0], RichTextCustomEmoji)
    assert header[0].custom_emoji_id == "5422502846648039176"
    assert header[0].alternative_text == "💬"
    assert isinstance(header[2], RichTextBold) and header[2].text == "Космокот"
    assert isinstance(header[4], RichTextUrl) and header[4].text == "@cosmocat" and header[4].url == value.author.url
    assert isinstance(header[6], RichTextUrl) and header[6].text == "↗" and header[6].url == value.url
    assert header[7] == "\n\n"
    assert len(result) == 3 and isinstance(result[0], InputRichBlockParagraph)
    assert text_of(result[:1]) == "💬 Космокот · @cosmocat · ↗\n\n" + value.text
    assert isinstance(result[1], InputRichBlockCollage)
    assert [item.type for item in result[1].blocks] == ["photo", "video"]
    assert isinstance(result[2], InputRichBlockBlockQuotation)
    assert text_of(result[2].blocks[:1]) == "💬 Лунный кот · @mooncat · ↗\n\nНа Луну"
    assert any(isinstance(item, InputRichBlockPhoto) for item in result[2].blocks)
    assert not any(isinstance(item, InputRichBlockFooter) for item in all_blocks(result))
    assert links_of(result) == [value.author.url, value.url, quoted.author.url, quoted.url]
    assert "Статистика" not in text_of(result)


def test_reply_attribution_and_body_have_internal_blank_lines_in_one_paragraph():
    value = post(text="First line\nSecond line", replying_to={"screen_name": "cat", "status": "999"})
    rendered = x_posts.render_x_post(value, {})
    assert len(rendered) == 1 and len(rendered[0].blocks) == 1
    assert isinstance(rendered[0].blocks[0], InputRichBlockParagraph)
    assert text_of(rendered[0].blocks) == "💬 Космокот · @cosmocat · ↗\n\n↪ В ответ @cat\n\nFirst line\nSecond line"
    assert links_of(rendered[0].blocks)[-1] == "https://x.com/cat/status/999"


@pytest.mark.parametrize("has_media_link", [False, True], ids=["empty-body", "removed-media-link"])
def test_media_only_post_has_no_empty_text_block_or_trailing_blank_lines(has_media_link):
    item = media(1)
    link = "https://t.co/media"
    raw = {"text": link, "facets": [{"type": "media", "id": "1", "indices": [0, len(link)]}]} if has_media_link else None
    rendered = x_posts.render_x_post(post(text="", raw_text=raw, media={"all": [item]}), uploads([item]))
    assert len(rendered[0].blocks) == 2
    assert text_of(rendered[0].blocks[:1]) == "💬 Космокот · @cosmocat · ↗"
    assert isinstance(rendered[0].blocks[1], InputRichBlockPhoto)


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


def test_uploaded_selected_media_links_are_removed_without_hiding_other_attachments():
    first, second = media(1), media(2)
    raw = {
        "text": "😀 Photos https://t.co/first https://t.co/second",
        "facets": [
            {"type": "media", "indices": [10, 28], "id": "1"},
            {"type": "media", "indices": [29, 48], "id": "2"},
        ],
    }
    value = post(raw_text=raw, media={"all": [first, second]})
    rendered = x_posts.render_x_post(value, uploads([first, second]), link=parse_post_url("https://x.com/cat/status/123/photo/2"))
    output = text_of(blocks(rendered))
    assert "😀 Photos https://t.co/first" in output
    assert "https://t.co/second" not in output
    attached = [block for block in all_blocks(blocks(rendered)) if isinstance(block, InputRichBlockPhoto)]
    assert len(attached) == 1 and attached[0].photo.media.filename == "1"


@pytest.mark.parametrize(
    "facet,uploaded",
    [
        ({"type": "media", "id": "1"}, False),
        ({"type": "media", "id": "different"}, True),
        ({"type": "media"}, True),
        ({"type": "url", "id": "1"}, True),
    ],
    ids=["download-missing", "other-media-id", "missing-media-id", "ordinary-url"],
)
def test_unproven_media_links_are_never_removed(facet, uploaded):
    item = media(1)
    link = "https://t.co/keep"
    value = post(
        raw_text={"text": link, "facets": [{**facet, "indices": [0, len(link)]}]},
        media={"all": [item]},
    )
    rendered = x_posts.render_x_post(value, uploads([item]) if uploaded else {})
    assert link in text_of(blocks(rendered))


@pytest.mark.parametrize("uploaded", [False, True], ids=["missing-upload", "uploaded"])
@pytest.mark.parametrize(
    "body,indices,original",
    [
        ("Keep these words intact", [5, 16], None),
        ("Read https://t.co/real", [5, 22], "https://t.co/different"),
    ],
    ids=["offsets-cover-ordinary-words", "offsets-cover-another-link"],
)
def test_stale_media_facets_cannot_remove_or_replace_original_content(body, indices, original, uploaded):
    item = media(1)
    value = post(
        raw_text={
            "text": body,
            "facets": [
                {
                    "type": "media",
                    "id": "1",
                    "indices": indices,
                    "original": original,
                    "replacement": "https://x.com/wrong/status/999/photo/1",
                    "display": "Unrelated replacement",
                }
            ],
        },
        media={"all": [item]},
    )
    rendered = x_posts.render_x_post(value, uploads([item]) if uploaded else {})
    assert text_of(rendered[0].blocks[:1]).partition("\n\n")[2] == body
    assert "https://x.com/wrong/status/999/photo/1" not in links_of(blocks(rendered))


def test_matching_media_original_removes_the_trailing_link_and_its_empty_spacing():
    item = media(1)
    body = "😀 Keep this\n\nhttps://t.co/media  \n"
    start = len("😀 Keep this\n\n".encode("utf-16-le")) // 2
    value = post(
        raw_text={
            "text": body,
            "facets": [{"type": "media", "id": "1", "indices": [start, start + 18], "original": "https://t.co/media"}],
        },
        media={"all": [item]},
    )
    rendered = x_posts.render_x_post(value, uploads([item]))
    assert text_of(rendered[0].blocks[:1]).partition("\n\n")[2] == "😀 Keep this"
    assert any(isinstance(block, InputRichBlockPhoto) for block in all_blocks(blocks(rendered)))


def test_media_link_without_a_facet_is_preserved_even_when_media_is_uploaded():
    item = media(1)
    value = post(raw_text={"text": "https://t.co/keep"}, media={"all": [item]})
    assert "https://t.co/keep" in text_of(blocks(x_posts.render_x_post(value, uploads([item]))))


@pytest.mark.parametrize("prefix", ["😀 ", "🧑🏽‍🚀 "])
def test_media_facet_removal_preserves_utf16_neighbors_links_and_trailing_text(prefix):
    item = media(1)
    body = prefix + "https://t.co/media @cat https://t.co/page end 😺"

    def facet(kind, label, **extra):
        index = body.index(label)
        start = len(body[:index].encode("utf-16-le")) // 2
        end = start + len(label.encode("utf-16-le")) // 2
        return {"type": kind, "indices": [start, end], **extra}

    value = post(
        raw_text={
            "text": body,
            "facets": [
                facet("media", "https://t.co/media", id="1"),
                facet("mention", "@cat"),
                facet("url", "https://t.co/page", replacement="https://example.org/page", display="example.org/page"),
            ],
        },
        media={"all": [item]},
    )
    rendered = x_posts.render_x_post(value, uploads([item]))
    output = text_of(blocks(rendered))
    assert prefix in output
    assert "https://t.co/media" not in output
    assert "@cat example.org/page end 😺" in output
    assert "https://x.com/cat" in links_of(blocks(rendered))
    assert "https://example.org/page" in links_of(blocks(rendered))


def test_uploaded_media_link_in_a_quote_does_not_remove_unselected_parent_media():
    first, second = media(1), media(2)
    short = "https://t.co/media"
    value = post(
        raw_text={"text": short, "facets": [{"type": "media", "id": "1", "indices": [0, len(short)]}]},
        media={"all": [first, second]},
        quote=post(id="456", text="Quoted photo", media={"all": [first]}),
    )
    rendered = x_posts.render_x_post(value, uploads([first, second]), link=parse_post_url("https://x.com/cat/status/123/photo/2"))
    assert short in text_of(rendered[0].blocks[:2])


def test_overflow_preserves_custom_header_emoji_and_plain_fallback_glyph():
    value = post(text="😀" * 40000)
    rendered = x_posts.render_x_post(value, {})
    assert len(rendered) > 1
    spans = [span for block in all_blocks(blocks(rendered)) if hasattr(block, "text") for span in rich._rich_spans(block.text)]
    custom = [span for span in spans if span.custom_emoji_id]
    assert len(custom) == 1
    assert custom[0].custom_emoji_id == "5422502846648039176" and custom[0].text == "💬"
    first = rich._plain(rendered[0].blocks)
    assert first.startswith("💬 Космокот · @cosmocat")
    assert value.author.url in first and value.url in first


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
        for span in rich._rich_spans(block.text)
        if span.text and all(char == character for char in span.text)
    )
    assert body == original
    assert all(rich._fits(message.blocks) for message in result)
    assert links_of(blocks(result))[-1].endswith("/status/123")


@pytest.mark.parametrize("suffix,expected_messages", [("", 1), ("😀", 2)], ids=["exact-limit", "astral-overflow"])
def test_header_and_separator_count_toward_the_utf16_message_limit(suffix, expected_messages):
    intro = "💬 Космокот · @cosmocat · ↗\n\n"
    body = "я" * (32768 - len(intro.encode("utf-16-le")) // 2) + suffix
    rendered = x_posts.render_x_post(post(text=body), {})
    assert len(rendered) == expected_messages
    assert "".join(text_of(message.blocks) for message in rendered) == intro + body
    assert all(rich._fits(message.blocks) for message in rendered)


def test_many_media_split_at_budget_without_losing_order():
    items = [media(index) for index in range(103)]
    result = x_posts.render_x_post(post(media={"all": items}), uploads(items))
    assert len(result) == 3
    attached = [block.photo.media.filename for block in all_blocks(blocks(result)) if isinstance(block, InputRichBlockPhoto)]
    assert attached == [str(index) for index in range(103)]
    assert all(rich._cost(message.blocks)[2] <= 50 for message in result)


def test_many_article_blocks_keep_all_text_within_block_limit():
    texts = [f"line {index}" for index in range(1200)]
    result = x_posts.render_x_post(post(article={"title": "Article", "content": {"blocks": [{"text": line} for line in texts]}}), {})
    rendered = text_of(blocks(result))
    assert all(line in rendered for line in texts)
    assert all(rich._cost(message.blocks)[1] <= 500 for message in result)


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
                {"type": "bold", "indices": [0, rich._units(body)]},
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
    spans = list(rich._rich_spans(result[0].blocks[0].text))
    body_start = next(index for index, span in enumerate(spans) if span.text == "\n\n") + 1
    assert all("BOLD" in (span.style or "") for span in spans[body_start:])
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
    spans = list(rich._rich_spans(rendered[0].blocks[0].text))
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


@pytest.mark.parametrize("sensitive", [False, True])
def test_shared_video_upload_keeps_geometry_and_independent_post_spoilers(sensitive):
    item = media(1, "video")
    uploaded = InputMediaVideo(
        media=BufferedInputFile(b"synthetic", filename="video.mp4"),
        width=800,
        height=600,
        duration=7,
        supports_streaming=True,
    )
    value = post(
        possibly_sensitive=sensitive,
        media={"all": [item]},
        quote=post(id="456", possibly_sensitive=not sensitive, media={"all": [item]}),
    )
    rendered = x_posts.render_x_post(value, {item.url: uploaded})
    videos = [block.video for block in all_blocks(blocks(rendered)) if isinstance(block, InputRichBlockVideo)]
    assert len(videos) == 2 and videos[0] is not videos[1]
    assert [video.has_spoiler for video in videos] == [sensitive, not sensitive]
    assert all((video.width, video.height, video.duration, video.supports_streaming) == (800, 600, 7, True) for video in videos)
    assert uploaded.has_spoiler is None


@pytest.mark.parametrize(
    "dimensions,variant_dimensions,expected",
    [
        ((800, 600), None, (800, 600)),
        ((800, 600), (320, 180), (320, 180)),
        ((800, 600), (320, None), (800, 600)),
        ((800, 600), (None, 180), (800, 600)),
        ((0, 600), None, (None, None)),
        ((800, 0), None, (None, None)),
        ((0, 0), (320, 180), (320, 180)),
        ((40000, 20000), None, (10000, 5000)),
        ((800, 600), (10**400, 5 * 10**399), (10000, 5000)),
    ],
    ids=[
        "primary",
        "variant",
        "variant-width-only",
        "variant-height-only",
        "primary-width-missing",
        "primary-height-missing",
        "variant-only",
        "scaled",
        "malformed-huge-variant",
    ],
)
def test_video_geometry_uses_a_complete_pair_from_the_selected_source(dimensions, variant_dimensions, expected):
    item = media(1, "video", width=dimensions[0], height=dimensions[1], duration=3.2)
    variant = (
        FxMediaFormat(url="https://video.twimg.com/variant.mp4", width=variant_dimensions[0], height=variant_dimensions[1])
        if variant_dimensions is not None
        else None
    )
    upload = x_posts._video_upload(BufferedInputFile(b"synthetic", filename="video.mp4"), item, variant)
    assert (upload.width, upload.height) == expected
    assert upload.duration == 3 and upload.supports_streaming


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
    paragraph = next(
        block for block in blocks(result) if isinstance(block, InputRichBlockParagraph) and text_of([block]) == "😀 bold plain"
    )
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
    assert session.methods[0].text.startswith("💬 Космокот · @cosmocat")
    assert "full body " * 700 in "".join(method.text for method in session.methods)
    assert value.url in "".join(method.text for method in session.methods)
    assert all(method.parse_mode is None and method.link_preview_options.is_disabled for method in session.methods)
    assert all(method.message_thread_id == 73 for method in session.methods)


@pytest.mark.parametrize("reason", ["Bad Request: invalid custom emoji", "Bad Request: CUSTOM_EMOJI_NOT_ALLOWED"])
async def test_custom_emoji_rejection_retries_once_with_plain_glyph_and_intact_rich_media(monkeypatch, reason):
    async def download(session, item, path, budget):
        path.write_bytes(b"synthetic")

    monkeypatch.setattr(x_posts, "_download", download)
    session = RecordingSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    value = post(
        media={"all": [media(1)]},
        quote=post(id="456", possibly_sensitive=True, media={"all": [media(2, "video")]}),
    )
    method = SendRichMessage(chat_id=-10042, rich_message=x_posts.render_x_post(value, {})[0])
    sent = make_message(message_id=501)
    monkeypatch.setattr(bot, "send_rich_message", AsyncMock(side_effect=[TelegramBadRequest(method=method, message=reason), sent]))

    result = await x_posts.publish_x_post(value, bot, -10042, 12, message_thread_id=73)

    assert result is sent
    assert bot.send_rich_message.await_count == 2
    first, retry = [call.kwargs for call in bot.send_rich_message.await_args_list]
    original, retried = first["rich_message"], retry["rich_message"]
    original_spans = [span for block in all_blocks(original.blocks) if hasattr(block, "text") for span in rich._rich_spans(block.text)]
    retried_spans = [span for block in all_blocks(retried.blocks) if hasattr(block, "text") for span in rich._rich_spans(block.text)]
    assert sum(bool(span.custom_emoji_id) for span in original_spans) == 2
    assert not any(span.custom_emoji_id for span in retried_spans)
    assert text_of(retried.blocks) == text_of(original.blocks)
    assert links_of(retried.blocks) == links_of(original.blocks)
    assert retried.skip_entity_detection is True
    original_media = [block for block in all_blocks(original.blocks) if isinstance(block, InputRichBlockPhoto | InputRichBlockVideo)]
    retried_media = [block for block in all_blocks(retried.blocks) if isinstance(block, InputRichBlockPhoto | InputRichBlockVideo)]
    assert original_media == retried_media
    assert len(retried_media) == 2 and retried_media[1].video.has_spoiler
    assert (retried_media[1].video.width, retried_media[1].video.height) == (800, 600)
    for call in (first, retry):
        assert call["chat_id"] == -10042 and call["message_thread_id"] == 73
        assert call["reply_parameters"].message_id == 12
    assert not session.methods


@pytest.mark.parametrize("error_type", [TelegramBadRequest, TelegramNetworkError, TelegramServerError, TimeoutError])
async def test_emoji_retry_propagates_a_second_unrelated_or_ambiguous_failure(monkeypatch, error_type):
    session = RecordingSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    method = SendRichMessage(chat_id=1, rich_message=x_posts.render_x_post(post(), {})[0])
    second = TimeoutError() if error_type is TimeoutError else error_type(method=method, message="chat not found")
    monkeypatch.setattr(
        bot,
        "send_rich_message",
        AsyncMock(side_effect=[TelegramBadRequest(method=method, message="invalid custom emoji"), second]),
    )
    with pytest.raises(error_type):
        await x_posts.publish_x_post(post(), bot, 1, 12)
    assert bot.send_rich_message.await_count == 2
    assert not session.methods


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
    remaining = rich._plain(x_posts.render_x_post(original, {})[1].blocks)
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
    assert calls == ["first"] * 3 + ["second"] * 3
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


@pytest.mark.parametrize("use_variant", [False, True], ids=["primary", "selected-variant"])
async def test_published_video_retains_downloaded_geometry_across_redirects(monkeypatch, use_variant):
    item = media(
        1,
        "video",
        width=1280,
        height=720,
        filesize=100 * 1024 * 1024 if use_variant else None,
        formats=[
            {
                "url": "https://video.twimg.com/smaller.mp4",
                "container": "mp4",
                "codec": "h264",
                "width": 320,
                "height": 180,
                "size": 1024,
            }
        ],
    )
    http = http_session(
        Response(status=302, headers={"Location": "/redirected.mp4"}),
        Response(headers={"Content-Type": "video/mp4"}, chunks=[b"synthetic-video"]),
    )

    @asynccontextmanager
    async def client_session(**kwargs):
        yield http

    session = RecordingSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    monkeypatch.setattr(x_posts.aiohttp, "ClientSession", client_session)
    await x_posts.publish_x_post(post(media={"all": [item]}), bot, -10042, 12)
    assert len(session.methods) == 1 and isinstance(session.methods[0], SendRichMessage)
    video = next(block.video for block in all_blocks(session.methods[0].rich_message.blocks) if isinstance(block, InputRichBlockVideo))
    assert (video.width, video.height) == ((320, 180) if use_variant else (1280, 720))
    assert [url for url, _ in http.calls] == [
        "https://video.twimg.com/smaller.mp4" if use_variant else item.url,
        "https://video.twimg.com/redirected.mp4",
    ]


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


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://pbs.twimg.com/media/image?format=jpg&name=orig", "https://pbs.twimg.com/media/image?format=jpg&name=large"),
        ("https://pbs.twimg.com/media/image.jpg:orig", "https://pbs.twimg.com/media/image.jpg?name=large"),
        ("https://pbs.twimg.com/media/image.png?name=orig", "https://pbs.twimg.com/media/image.png?name=large"),
        ("https://pbs.twimg.com/media/image?format=webp&name=orig", "https://pbs.twimg.com/media/image?format=webp&name=large"),
    ],
)
def test_smaller_photo_url_preserves_the_recognized_asset_and_format(url, expected):
    assert x_posts._smaller_photo_url(media(1, url=url)) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://pbs.twimg.com/media/image.jpg?name=large",
        "https://pbs.twimg.com/media/image.png",
        "https://pbs.twimg.com/media/image?format=webp",
        "https://pbs.twimg.com/media/image.jpg:small",
        "https://pbs.twimg.com/media/image.jpg:large?name=orig",
        "https://pbs.twimg.com/media/image.jpg?name=orig&name=large",
        "https://pbs.twimg.com/media/image?format=jpg&format=png",
        "https://pbs.twimg.com/media/image.jpg?token=secret",
        "https://pbs.twimg.com/profile_images/image.jpg",
        "https://pbs.twimg.com/media/image?format=gif",
        "https://pbs.twimg.com/media/image",
        "https://video.twimg.com/media/image.jpg",
        "https://pbs.twimg.com.attacker.invalid/media/image.jpg",
        "http://pbs.twimg.com/media/image.jpg",
    ],
)
def test_smaller_photo_url_does_not_rewrite_smaller_unknown_or_unsafe_urls(url):
    # The downloader still validates URLs if a typed object was copied incorrectly.
    item = media(1).model_copy(update={"url": url})
    assert x_posts._smaller_photo_url(item) is None


async def test_photo_metadata_oversize_skips_original_and_attempts_only_large(tmp_path):
    item = media(1, url="https://pbs.twimg.com/media/image.jpg:orig", filesize=10 * 1024 * 1024)
    session = http_session(Response(chunks=[b"smaller-photo"]))
    budget = x_posts._DownloadBudget()
    path = tmp_path / "photo"

    assert await x_posts._download(session, item, path, budget) is None

    assert [url for url, _ in session.calls] == ["https://pbs.twimg.com/media/image.jpg?name=large"]
    assert path.read_bytes() == b"smaller-photo"
    assert budget.requests == budget.reduced_photos == 1


@pytest.mark.parametrize("rejection", ["header", "stream", "status"])
async def test_photo_oversize_recovers_once_without_reusing_partial_bytes(tmp_path, rejection):
    limit = 9 * 1024 * 1024
    original = {
        "header": Response(content_length=limit + 1),
        "stream": Response(chunks=[b"x" * 65536] * (limit // 65536) + [b"overflow"]),
        "status": Response(status=413),
    }[rejection]
    item = media(1, url="https://pbs.twimg.com/media/image?format=jpg&name=orig")
    session = http_session(original, Response(chunks=[b"small-photo"]))
    path = tmp_path / "photo"
    budget = x_posts._DownloadBudget()
    available = budget.remaining

    await x_posts._download(session, item, path, budget)

    assert [url for url, _ in session.calls] == [item.url, "https://pbs.twimg.com/media/image?format=jpg&name=large"]
    assert path.read_bytes() == b"small-photo"
    consumed = limit + len(b"overflow") if rejection == "stream" else 0
    assert budget.remaining == available - consumed - len(b"small-photo")
    assert budget.requests == 2 and budget.reduced_photos == 1


async def test_second_photo_size_rejection_has_no_third_request(tmp_path):
    item = media(1, url="https://pbs.twimg.com/media/image.jpg?name=orig")
    session = http_session(Response(status=413), Response(status=413))
    budget = x_posts._DownloadBudget()

    with pytest.raises(x_posts._DownloadRejected) as error:
        await x_posts._download(session, item, tmp_path / "photo", budget)

    assert error.value.reason is MediaReason.OVERSIZE
    assert len(session.calls) == budget.requests == 2
    assert budget.reduced_photos == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://pbs.twimg.com/media/image.jpg?name=large",
        "https://pbs.twimg.com/profile_images/image.jpg",
        "https://pbs.twimg.com/media/image.jpg?signed=opaque",
    ],
)
async def test_smaller_or_unknown_photo_size_failure_is_not_retried(tmp_path, url):
    session = http_session(Response(status=413))
    budget = x_posts._DownloadBudget()
    with pytest.raises(x_posts._DownloadRejected):
        await x_posts._download(session, media(1, url=url), tmp_path / "photo", budget)
    assert len(session.calls) == 1 and budget.reduced_photos == 0


@pytest.mark.parametrize(
    "response,reason",
    [
        (Response(headers={"Content-Type": "text/html"}), MediaReason.UNSUPPORTED_FORMAT),
        (Response(status=302, headers={"Location": "http://127.0.0.1/private"}), MediaReason.REDIRECT_REJECTED),
        (Response(status=404), MediaReason.HTTP_ERROR),
    ],
)
async def test_non_size_photo_failures_never_trigger_rendition_recovery(tmp_path, response, reason):
    item = media(1, url="https://pbs.twimg.com/media/image.jpg?name=orig")
    session = http_session(response)
    budget = x_posts._DownloadBudget()
    with pytest.raises(x_posts._DownloadRejected) as error:
        await x_posts._download(session, item, tmp_path / "photo", budget)
    assert error.value.reason is reason
    assert len(session.calls) == 1 and budget.reduced_photos == 0


async def test_exhausted_total_budget_prevents_photo_retry_and_subsequent_requests(tmp_path):
    session = http_session(Response(chunks=[b"abcd", b"overflow"]))
    budget = x_posts._DownloadBudget(4)
    item = media(1, url="https://pbs.twimg.com/media/image.jpg?name=orig")
    with pytest.raises(x_posts._DownloadRejected) as error:
        await x_posts._download(session, item, tmp_path / "photo", budget)
    assert error.value.reason is MediaReason.BUDGET_EXHAUSTED
    with pytest.raises(x_posts._DownloadRejected):
        await x_posts._download(session, item, tmp_path / "other", budget)
    assert budget.remaining < 0
    assert len(session.calls) == 1 and budget.reduced_photos == 0


async def test_preparation_exports_real_ready_reduced_and_omitted_outcomes_without_content(monkeypatch, tmp_path):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)

    class NetworkFailure(Response):
        async def __aenter__(self):
            raise aiohttp.ClientConnectionError("ERROR_CANARY https://example.invalid/?token=SECRET_CANARY")

    items = [media(index, url=f"https://pbs.twimg.com/media/URL_CANARY_{index}.jpg?name=orig") for index in range(5)]
    http = http_session(
        Response(chunks=[b"BODY_CANARY"]),
        Response(content_length=10 * 1024 * 1024),
        Response(chunks=[b"reduced"]),
        Response(headers={"Content-Type": "text/HEADER_CANARY"}),
        Response(status=302, headers={"Location": "http://127.0.0.1/REDIRECT_CANARY"}),
        NetworkFailure(),
    )

    @asynccontextmanager
    async def client_session(**kwargs):
        assert kwargs["trust_env"] is False
        yield http

    monkeypatch.setattr(x_posts.aiohttp, "ClientSession", client_session)
    sink = Capture()
    telemetry = Telemetry(config(), transport=sink)
    await telemetry.start()
    try:
        with telemetry.context(user_id=42, chat_id=-10042, message_id=7):
            prepared = await x_posts._prepare_uploads(
                post(text="POST_CANARY", author={"name": "AUTHOR_CANARY", "screen_name": "usercanary"}, media={"all": items}),
                None,
                tmp_path,
                telemetry,
            )
    finally:
        await telemetry.close()

    assert set(prepared) == {item.url for item in items[:2]}
    assert sorted(path.name for path in tmp_path.iterdir()) == ["0.jpg", "1.jpg"]
    assert (tmp_path / "0.jpg").read_bytes() == b"BODY_CANARY"
    assert (tmp_path / "1.jpg").read_bytes() == b"reduced"
    assert len(http.calls) == 6
    assert all(url.startswith("https://pbs.twimg.com/") for url, _ in http.calls)
    assert len(sink.spans()) == len(sink.logs()) == 1
    attributes = {item.key: item.value for item in sink.spans()[0].attributes}
    assert attributes["operation"].string_value == "x.media.prepare"
    assert attributes["outcome"].string_value == "unavailable"
    assert attributes["media.assets.total"].int_value == 5
    assert attributes["media.assets.ready"].int_value == 2
    assert attributes["media.assets.reduced"].int_value == 1
    assert attributes["media.assets.omitted"].int_value == 3
    assert attributes["media.download.attempts"].int_value == 6
    for reason in ("unsupported_format", "redirect_rejected", "network"):
        assert attributes[f"media.omitted.{reason}"].int_value == 1
    assert attributes["telegram.user_id"].int_value == 42
    assert attributes["telegram.chat_id"].int_value == -10042
    assert sink.logs()[0].body.string_value == "media.preparation.omitted"
    exported = sink.serialized()
    assert "bot.media.assets" in exported and "ready_reduced" in exported
    for canary in ("CANARY", "usercanary", "127.0.0.1", "pbs.twimg.com", "example.invalid"):
        assert canary not in exported


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_preparation_interruption_accounts_for_unattempted_media_and_cleans_temporary_files(monkeypatch, tmp_path, stop):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    started = asyncio.Event()
    paths = []

    class StalledResponse(Response):
        async def iter_chunked(self, _):
            yield b"unfinished image"
            started.set()
            await asyncio.Event().wait()

    http = http_session(StalledResponse())

    @asynccontextmanager
    async def client_session(**kwargs):
        yield http

    temporary_directory = x_posts.TemporaryDirectory

    def capture_directory(**kwargs):
        result = temporary_directory(dir=tmp_path, **kwargs)
        paths.append(Path(result.name))
        return result

    monkeypatch.setattr(x_posts.aiohttp, "ClientSession", client_session)
    monkeypatch.setattr(x_posts, "TemporaryDirectory", capture_directory)
    monkeypatch.setattr(x_posts, "_PREPARE_TIMEOUT", 0.02 if stop == "timeout" else 90)
    sink = Capture()
    telemetry = Telemetry(config(), transport=sink)
    await telemetry.start()
    session = RecordingSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    task = asyncio.create_task(x_posts.publish_x_post(post(media={"all": [media(1), media(2)]}), bot, 1, telemetry=telemetry))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert paths[0].is_dir() and list(paths[0].iterdir())
        if stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await asyncio.wait_for(task, timeout=1)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await telemetry.close()

    reason = "timeout" if stop == "timeout" else "cancelled"
    assert len(http.calls) == 1
    assert paths and all(not path.exists() for path in paths)
    assert not list(tmp_path.iterdir())
    assert len(session.methods) == (1 if stop == "timeout" else 0)
    if session.methods:
        rendered = session.methods[0].rich_message.blocks
        assert "Полный текст" in text_of(rendered)
        assert not any(isinstance(block, InputRichBlockPhoto) for block in all_blocks(rendered))
    attributes = {item.key: item.value for item in sink.spans()[0].attributes}
    assert attributes["outcome"].string_value == reason
    assert attributes["media.assets.total"].int_value == attributes["media.assets.omitted"].int_value == 2
    assert attributes[f"media.omitted.{reason}"].int_value == 2
    assert attributes["media.download.attempts"].int_value == 1
