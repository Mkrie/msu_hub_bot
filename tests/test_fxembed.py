"""The public X boundary preserves useful content without trusting optional metadata."""

import asyncio
import json

import aiohttp
import pytest
from pydantic import ValidationError

from msu_hub_bot.providers import fxembed
from msu_hub_bot.providers.exceptions import BadRequestError, NotFoundError
from msu_hub_bot.providers.fxembed import (
    FxArticle,
    FxArticleContent,
    FxEmbed,
    FxMediaContainer,
    FxPost,
    FxText,
    FxTombstone,
    is_x_url,
    parse_post_url,
    safe_media_url,
    safe_public_url,
)


def post_payload(**changes):
    return {
        "type": "status",
        "provider": "twitter",
        "id": "1234567890123456789",
        "url": "https://x.com/example/status/1234567890123456789",
        "text": "Hello 🐱 world",
        "raw_text": {"text": "Hello 🐱 world", "facets": [], "display_text_range": [0, 14]},
        "author": {"name": "Космокот", "screen_name": "example", "protected": False},
        "media": {"all": []},
        **changes,
    }


def photo(media_id="1", **changes):
    return {"id": media_id, "type": "photo", "url": f"https://pbs.twimg.com/media/{media_id}.jpg", "width": 1200, "height": 800, **changes}


def video(media_id="2", **changes):
    return {
        "id": media_id,
        "type": "video",
        "url": f"https://video.twimg.com/ext_tw_video/{media_id}/clip.mp4",
        "width": 1280,
        "height": 720,
        "duration": 2.5,
        **changes,
    }


@pytest.mark.parametrize(
    "host",
    ["x.com", "www.x.com", "mobile.x.com", "m.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com", "m.twitter.com", "X.COM"],
)
@pytest.mark.parametrize("path", ["example/status/123", "i/status/123", "i/web/status/123"])
def test_exact_original_domains_and_status_routes(host, path):
    link = parse_post_url(f"https://{host}/{path}?s=46&t=tracking#ignored")
    assert link and link.id == "123" and link.url == "https://x.com/i/status/123"


@pytest.mark.parametrize("kind,index", [("photo", 2), ("video", 1), ("photo", 99)])
def test_attachment_selection_survives_tracking_removal(kind, index):
    link = parse_post_url(f"http://twitter.com/example/status/123/{kind}/{index}/?s=19")
    assert link and (link.media_kind, link.media_index) == (kind, index)
    assert link.url == f"https://x.com/i/status/123/{kind}/{index}"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil-x.com/example/status/123",
        "https://x.com.attacker.example/example/status/123",
        "https://x.com@attacker.example/example/status/123",
        "https://attacker@x.com/example/status/123",
        "https://x.com:444/example/status/123",
        "https://x.com:443/example/status/123",
        "https://x.com./example/status/123",
        "https://x.com/example/status/0",
        "https://x.com/example/status/-123",
        "https://x.com/example/status/1e2",
        "https://x.com/example/status/000123",
        "https://x.com/example/status/123xyz",
        "https://x.com/example/status/123/comments",
        "https://x.com/example/status/123/photo/0",
        "https://x.com/example/status/123/photo/100",
        "https://x.com/example/status/123/video/-1",
        "https://x.com/example/status/123/photo/1/more",
        "https://x.com/example/status/123456789012345678901",
        "https://x.com/too_long_handle_name/status/123",
        "https://x.com/i/web/status/123/more",
        "https://x.com/example%2fstatus/123",
        "https://x.com/example/status/123\n",
        "https://x.com\\@attacker.example/example/status/123",
        "ftp://x.com/example/status/123",
        "https://fixupx.com/example/status/123",
        "https://fxtwitter.com/example/status/123",
        "https://x.com/example",
        "https://x.com/home",
    ],
)
def test_non_posts_and_host_confusion_are_rejected(url):
    assert parse_post_url(url) is None


@pytest.mark.parametrize("host", ["fixupx.com", "fxtwitter.com", "i.fixupx.com", "d.fxtwitter.com", "twittpr.com", "xfixup.com"])
def test_fixed_posts_have_separate_ownership(host):
    url = f"https://{host}/example/status/123/photo/1"
    assert is_x_url(url) and parse_post_url(url) is None
    assert not is_x_url(f"https://{host}.attacker.example/example/status/123")


@pytest.mark.parametrize(
    "url",
    ["https://pbs.twimg.com/media/a.jpg?name=orig", "https://video.twimg.com/ext_tw_video/a.mp4", "https://pbs.twimg.com:443/media/a.jpg"],
)
def test_only_official_https_cdn_media_is_downloadable(url):
    assert safe_media_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://pbs.twimg.com/media/a.jpg",
        "https://pbs.twimg.com.attacker.example/a.jpg",
        "https://user@pbs.twimg.com/a.jpg",
        "https://pbs.twimg.com:444/a.jpg",
        "https://127.0.0.1/a.jpg",
        "https://localhost/a.jpg",
        "file:///tmp/a.jpg",
        "https://images.example/a.jpg",
        "https://pbs.twimg.com/a\nb.jpg",
        "https://pbs.twimg.com\\@127.0.0.1/a.jpg",
    ],
)
def test_arbitrary_media_hosts_are_not_download_authorization(url):
    assert safe_media_url(url) is None


def test_public_display_links_do_not_authorize_downloads():
    assert safe_public_url("https://example.org/article?q=a&b=c") == "https://example.org/article?q=a&b=c"
    assert safe_media_url("https://example.org/article?q=a&b=c") is None
    for url in [
        "javascript:alert(1)",
        "tg://user?id=1",
        "https://127.0.0.1/",
        "http://169.254.169.254/",
        "https://localhost/",
        "https://a.local/",
        "https://[::1]/",
        "https://user:pass@example.org/",
        "https://example.org:bad/",
    ]:
        assert safe_public_url(url) is None


def test_utf16_facets_keep_full_body_and_drop_only_invalid_ranges():
    raw = FxText.model_validate(
        {
            "text": "A🐱B link",
            "display_text_range": [3, 9],
            "facets": [
                {"type": "bold", "indices": [1, 3]},
                {"type": "url", "indices": [5, 9], "replacement": "https://example.org/"},
                {"type": "italic", "indices": [1, 2]},
                {"type": "bold", "indices": [8, 20]},
                {"type": "bold", "indices": [5, 3]},
                {"type": "bold", "indices": [True, 3]},
                {"type": "bold", "indices": [-1, 3]},
                {"type": "bold", "indices": [0, 0]},
                None,
            ],
        }
    )
    assert raw.text == "A🐱B link" and raw.display_text_range == (3, 9)
    assert [(facet.type, facet.indices) for facet in raw.facets] == [("bold", (1, 3)), ("url", (5, 9))]


@pytest.mark.parametrize("raw", [None, {}, {"text": 123}, {"text": "Hello 🐱 world", "facets": None, "display_text_range": "changed"}])
def test_optional_text_metadata_cannot_hide_body(raw):
    post = FxPost.model_validate(post_payload(raw_text=raw))
    assert post.raw_text.text == post.text == "Hello 🐱 world"


def test_full_note_text_is_preserved_instead_of_short_presentation_text():
    body = "🐱 & <tag> " * 10000
    post = FxPost.model_validate(post_payload(text="Short display", raw_text={"text": body, "facets": []}))
    assert post.raw_text.text == body


def test_note_tweet_scalar_facets_normalize_once_without_changing_full_body_or_display_range():
    body = "🐱 " * 4000 + "https://t.co/link @cosmocat #Cat"
    link_start = body.index("https://")
    link_end = link_start + len("https://t.co/link")
    mention_start = body.index("@")
    mention_end = mention_start + len("@cosmocat")
    units = len(body.encode("utf-16-le")) // 2
    source = post_payload(
        is_note_tweet=True,
        raw_text={
            "text": body,
            "display_text_range": [0, units],
            "facets": [
                {"type": "bold", "indices": [0, len(body)]},
                {"type": "url", "indices": [link_start, link_end], "replacement": "https://example.org/full"},
                {"type": "mention", "indices": [mention_start, mention_end]},
                {"type": "italic", "indices": [0, len(body) + 1]},
            ],
        },
    )
    post = FxPost.model_validate(source)
    assert post.raw_text.text == body and post.raw_text.display_text_range == (0, units)
    assert [(facet.type, facet.indices) for facet in post.raw_text.facets] == [
        ("bold", (0, units)),
        ("url", (link_start + 4000, link_end + 4000)),
        ("mention", (mention_start + 4000, mention_end + 4000)),
    ]
    assert source["raw_text"]["facets"][1]["indices"] == [link_start, link_end]
    assert FxPost.model_validate(post.model_dump()).raw_text == post.raw_text


def test_note_normalization_does_not_change_legacy_or_community_note_utf16_facets():
    body = "🐱 @cat"
    facet = {"type": "mention", "indices": [3, 7]}
    legacy = FxPost.model_validate(post_payload(raw_text={"text": body, "facets": [facet]}))
    assert legacy.raw_text.facets[0].indices == (3, 7)
    note = FxPost.model_validate(post_payload(is_note_tweet=True, community_note={"text": body, "facets": [facet]}))
    assert note.community_note.facets[0].indices == (3, 7)


def test_nested_note_and_prevalidated_text_are_not_misinterpreted_as_legacy_offsets():
    raw = {"text": "😀Hello @cat tail", "facets": [{"type": "mention", "indices": [7, 11]}]}
    quoted = FxPost.model_validate(post_payload(quote=post_payload(id="123", is_note_tweet=True, raw_text=raw)))
    assert quoted.quote.raw_text.facets[0].indices == (8, 12)
    note = FxPost.model_validate(post_payload(is_note_tweet=True, raw_text=quoted.quote.raw_text))
    assert note.raw_text.facets[0].indices == (8, 12)


def test_ordered_media_ignores_duplicate_parallel_lists_and_rejects_bad_urls():
    post = FxPost.model_validate(
        post_payload(
            media={
                "all": [photo(), video(), photo("3"), video("4", type="gif"), photo("5", url="https://attacker.example/a.jpg")],
                "photos": [photo("duplicated")],
                "videos": [video("duplicated")],
            }
        )
    )
    assert [item.id for item in post.media.all] == ["1", "2", "3", "4"]
    assert [item.type for item in post.media.all] == ["photo", "video", "photo", "gif"]
    assert post.media.unsupported_count == 1
    assert FxMediaContainer.model_validate({"all": [], "photos": [photo()]}).all == []


def test_bad_optional_media_metadata_does_not_hide_valid_attachments():
    media = FxMediaContainer.model_validate(
        {
            "all": [
                video(
                    thumbnail_url="https://attacker.example/private",
                    width="oops",
                    height=-1,
                    duration={},
                    filesize=True,
                    altText=[],
                    formats=[None, {"url": "https://video.twimg.com/good.mp4", "size": 25}, {"url": "https://attacker.example/bad.mp4"}],
                )
            ],
            "external": {"type": "video", "url": "https://www.youtube.com/watch?v=example"},
        }
    )
    assert len(media.all) == 1 and media.all[0].width == media.all[0].height == 0
    assert media.all[0].thumbnail_url is None and media.all[0].filesize is None and media.all[0].duration is None
    assert len(media.all[0].formats) == 1 and media.unsupported_count == 1
    assert media.external_url == "https://www.youtube.com/watch?v=example"


def test_rejected_media_does_not_shift_original_attachment_selector_positions():
    media = FxMediaContainer.model_validate({"all": [photo("1", url="https://attacker.example/image"), video("2"), photo("3")]})
    assert [(item.id, item.source_index) for item in media.all] == [("2", 2), ("3", 3)]


@pytest.mark.parametrize(
    "key,value", [("poll", {"choices": "invalid"}), ("community_note", {"text": 1}), ("replying_to", {"status": -1}), ("article", 12)]
)
def test_invalid_optional_post_sections_preserve_original_text(key, value):
    post = FxPost.model_validate(post_payload(**{key: value}))
    assert post.raw_text.text == "Hello 🐱 world" and getattr(post, key) is None


def test_quotes_tombstones_notes_polls_and_reply_attribution_survive():
    child = post_payload(
        id="123",
        quote={
            "type": "tombstone",
            "provider": "twitter",
            "reason": "deleted",
            "id": "100",
            "url": "https://twitter.com/example/status/100",
        },
    )
    post = FxPost.model_validate(
        post_payload(
            quote=child,
            possibly_sensitive=True,
            community_note={
                "text": "Read this",
                "facets": [{"type": "url", "indices": [5, 9], "replacement": "https://example.org/context"}],
            },
            poll={
                "choices": [{"label": "Cat", "count": 10, "percentage": 100}],
                "total_votes": 10,
                "ends_at": "2026-09-19T12:00:00Z",
                "time_left_en": "Final results",
            },
            replying_to={"screen_name": "parent", "status": "99", "url": "https://attacker.example/"},
        )
    )
    assert isinstance(post.quote, FxPost) and isinstance(post.quote.quote, FxTombstone)
    assert post.quote.quote.reason == "deleted" and post.quote.quote.url == "https://x.com/i/status/100"
    assert post.community_note.facets[0].replacement == "https://example.org/context"
    assert post.poll.total_votes == 10 and post.poll.choices[0].percentage == 100
    assert post.possibly_sensitive and post.replying_to.url == "https://x.com/parent/status/99"


def test_private_or_malformed_quote_becomes_unavailable_without_leaking_body():
    for quote in [
        post_payload(author={"name": "Hidden", "screen_name": "hidden", "protected": True}),
        post_payload(author=None),
        {"type": "new_type", "url": "https://attacker.example/", "reason": "unknown"},
    ]:
        post = FxPost.model_validate(post_payload(quote=quote))
        assert isinstance(post.quote, FxTombstone)
        assert "Hidden" not in post.model_dump_json() and post.text == "Hello 🐱 world"
    with pytest.raises(ValidationError):
        FxPost.model_validate(post_payload(author={"name": "Private", "screen_name": "hidden", "protected": True}))


def test_article_normalization_preserves_draft_blocks_entities_media_and_inline_links():
    image = {
        "id": "m1",
        "media_info": {
            "__typename": "ApiImage",
            "original_img_url": "https://pbs.twimg.com/media/a.jpg",
            "original_img_width": 800,
            "original_img_height": 600,
        },
    }
    clip = {
        "media_id": "m2",
        "media_info": {
            "__typename": "ApiVideo",
            "media_url_https": "https://pbs.twimg.com/thumb.jpg",
            "original_info": {"width": 1280, "height": 720},
            "video_info": {
                "duration_millis": 1500,
                "variants": [
                    {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/list.m3u8"},
                    {"content_type": "video/mp4", "url": "https://video.twimg.com/low.mp4", "bitrate": 100},
                    {"content_type": "video/mp4", "url": "https://video.twimg.com/high.mp4", "bitrate": 200},
                ],
            },
        },
    }
    article = FxArticle.model_validate(
        {
            "title": "A title",
            "preview_text": "Preview only",
            "cover_media": image,
            "media_entities": [image, clip],
            "content": {
                "blocks": [
                    {"key": "a", "type": "header-one", "text": "Title", "inlineStyleRanges": [], "entityRanges": []},
                    {
                        "key": "b",
                        "type": "unstyled",
                        "text": "🐱 @example",
                        "inlineStyleRanges": [{"offset": 0, "length": 2, "style": "Bold"}],
                        "entityRanges": [],
                        "data": {"mentions": [{"fromIndex": 3, "toIndex": 11, "text": "example"}]},
                    },
                    {"key": "c", "type": "atomic", "text": " ", "entityRanges": [{"offset": 0, "length": 1, "key": 0}]},
                ],
                "entityMap": [
                    {"key": "0", "value": {"type": "MEDIA", "data": {"mediaItems": [{"mediaId": "m2"}]}}},
                    {"key": "1", "value": {"type": "MARKDOWN", "data": {"markdown": "**all** the text"}}},
                    {"key": "2", "value": {"type": "TWEET", "data": {"tweetId": "123"}}},
                    {"key": "3", "value": {"type": ["future"], "data": {}}},
                ],
            },
        }
    )
    assert article.has_full_content and article.title == "A title"
    assert article.content.blocks[1].styles[0].length == 2
    assert article.content.blocks[1].facets[0].replacement == "https://x.com/example"
    assert [item.kind for item in article.content.entity_map] == ["MEDIA", "MARKDOWN", "TWEET", "UNKNOWN"]
    assert article.content.entity_map[0].media_ids == ["m2"] and article.content.entity_map[1].markdown == "**all** the text"
    assert [media.type for media in article.media_entities] == ["photo", "video"]
    assert article.media_entities[1].url == "https://video.twimg.com/high.mp4" and article.media_entities[1].duration == 1.5
    assert article.cover_media.id == "m1"


@pytest.mark.parametrize("content", [None, {}, {"blocks": []}, {"blocks": [{"text": "Available body"}, {"text": None}]}])
def test_article_excerpt_is_never_mistaken_for_complete_article(content):
    article = FxArticle.model_validate({"preview_text": "Short preview", "content": content})
    assert not article.has_full_content and article.preview_text == "Short preview"
    if isinstance(content, dict) and content.get("blocks"):
        assert article.content.blocks[0].text == "Available body"


def test_normalized_models_support_reuse_without_dropping_content():
    container = FxMediaContainer.model_validate({"all": [photo()], "unsupported_count": 2})
    assert FxMediaContainer.model_validate(container.model_dump()).unsupported_count == 2
    article = FxArticle(content=FxArticleContent(blocks=[{"text": "Body"}]))
    assert article.has_full_content


class Response:
    def __init__(self, payload, *, status=200, declared_length=None, blocked=None):
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status = status
        self.content_length = declared_length
        self.content = self
        self.closed = False
        self.blocked = blocked

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def iter_chunked(self, size):
        if self.blocked:
            self.blocked.set()
            await asyncio.Event().wait()
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.options = None
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def install(monkeypatch, response):
    session = Session(response)

    def factory(**kwargs):
        session.options = kwargs
        return session

    monkeypatch.setattr(fxembed.aiohttp, "ClientSession", factory)
    return session


def link():
    return parse_post_url("https://x.com/example/status/1234567890123456789/photo/2?t=private-tracking")


async def test_fetch_uses_only_id_without_cookies_secrets_or_user_query(monkeypatch):
    response = Response({"code": 200, "status": post_payload()})
    session = install(monkeypatch, response)
    post = await FxEmbed().get_post(link())
    assert post.id == link().id and post.author.url == "https://x.com/example"
    assert session.calls == [("https://api.fxtwitter.com/2/status/1234567890123456789", {"allow_redirects": False})]
    assert session.options["trust_env"] is False and session.options["timeout"].total <= 20
    assert set(session.options["headers"]) == {"User-Agent", "Accept"}
    assert "MSUHubBot" in session.options["headers"]["User-Agent"]
    assert session.closed and response.closed


@pytest.mark.parametrize("status", [301, 302, 307, 429, 500])
async def test_redirect_and_http_errors_are_not_followed_or_retried(monkeypatch, status):
    response = Response({"code": 200, "status": post_payload()}, status=status)
    session = install(monkeypatch, response)
    with pytest.raises(BadRequestError):
        await FxEmbed().get_post(link())
    assert len(session.calls) == 1 and session.closed and response.closed


@pytest.mark.parametrize("http,code", [(404, 404), (401, 401), (403, 403), (200, 404), (200, 403)])
async def test_unavailable_posts_have_structured_generic_error(monkeypatch, http, code):
    session = install(monkeypatch, Response({"code": code, "message": "Sensitive upstream detail"}, status=http))
    with pytest.raises(NotFoundError) as error:
        await FxEmbed().get_post(link())
    assert "Sensitive" not in str(error.value) and session.closed


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        [],
        {"code": "200", "status": post_payload()},
        {"code": True, "status": post_payload()},
        {"code": 200},
        {"code": 200, "status": post_payload(id="999")},
        {"code": 200, "status": post_payload(provider="bluesky")},
        {"code": 200, "status": post_payload(type="tombstone")},
        {"code": 200, "status": post_payload(author=None)},
    ],
)
async def test_malformed_or_wrong_post_envelopes_cannot_escape(monkeypatch, payload):
    session = install(monkeypatch, Response(payload))
    with pytest.raises(BadRequestError):
        await FxEmbed().get_post(link())
    assert session.closed


@pytest.mark.parametrize("declared", [True, False])
async def test_declared_and_streamed_body_size_are_bounded(monkeypatch, declared):
    monkeypatch.setattr(fxembed, "MAX_RESPONSE_BYTES", 16)
    session = install(monkeypatch, Response(b"x" if declared else b"x" * 17, declared_length=17 if declared else None))
    with pytest.raises(BadRequestError):
        await FxEmbed().get_post(link())
    assert session.closed


@pytest.mark.parametrize("cancel", [False, True])
async def test_total_deadline_and_cancellation_close_own_session(monkeypatch, cancel):
    blocked = asyncio.Event()
    response = Response(b"", blocked=blocked)
    session = install(monkeypatch, response)
    monkeypatch.setattr(fxembed, "REQUEST_TIMEOUT", 5 if cancel else 0.02)
    task = asyncio.create_task(FxEmbed().get_post(link()))
    await asyncio.wait_for(blocked.wait(), timeout=1)
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else BadRequestError):
        await task
    assert session.closed and response.closed


async def test_transport_errors_never_include_provider_payload_in_public_exception(monkeypatch):
    session = install(monkeypatch, Response({}))

    def failure(*_, **__):
        raise aiohttp.ClientConnectionError("upstream-sensitive-detail")

    session.get = failure
    with pytest.raises(BadRequestError) as error:
        await FxEmbed().get_post(link())
    assert "sensitive" not in str(error.value) and session.closed
