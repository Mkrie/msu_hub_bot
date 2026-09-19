"""Native Instagram acquisition preserves mixed media without inferring photos from broken videos."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from yt_dlp import YoutubeDL

from msu_hub_bot.providers import instagram
from msu_hub_bot.providers.link_models import LinkAsset

URL = "https://www.instagram.com/p/ABC_def123/"


def item(index=1, kind=1, **changes):
    return {
        "_ig_media_type": kind,
        "thumbnails": [{"url": f"https://scontent.cdninstagram.com/{index}.jpg", "width": 120, "height": 80}],
        "formats": [{"url": f"https://scontent.cdninstagram.com/{index}.mp4", "width": 120, "height": 80}] if kind == 2 else [],
        **changes,
    }


def post(**changes):
    return {
        "id": "ABC_def123",
        "description": "Полный текст <никакого HTML> @friends #котики",
        "channel": "friends.cat",
        "uploader": "Космокот",
        **item(),
        **changes,
    }


def providers(monkeypatch, info):
    monkeypatch.setattr(instagram.time, "monotonic", lambda: 100)
    extract = Mock(return_value=info)
    image = Mock(side_effect=lambda url, **kwargs: LinkAsset("photo", url.encode(), 640, 480))
    video = Mock(side_effect=lambda url, **kwargs: LinkAsset("video", url.encode(), 720, 1280, 12.5))
    monkeypatch.setattr(instagram, "extract_info", extract)
    monkeypatch.setattr(instagram, "download_image", image)
    monkeypatch.setattr(instagram, "download_video", video)
    return extract, image, video


@pytest.mark.parametrize(
    "url,canonical",
    [
        (URL + "?igsh=tracking", URL),
        ("http://instagram.com/p/ABC_def123", URL),
        ("https://www.instagram.com/friends.cat/p/ABC_def123/", URL),
        ("https://www.instagram.com/reels/ABC_def123/", "https://www.instagram.com/reel/ABC_def123/"),
    ],
)
def test_supported_links_canonicalize_without_fetching_profiles(url, canonical):
    assert instagram._post_url(url) == (canonical, "ABC_def123")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/friends.cat/",
        "https://www.instagram.com/stories/friends/123/",
        "https://www.instagram.com/explore/tags/cats/",
        "https://www.instagram.com/reels/audio/12345/",
        "https://www.instagram.com.attacker.invalid/p/ABC_def123/",
        "https://user:password@www.instagram.com/p/ABC_def123/",
        "https://www.instagram.com:443/p/ABC_def123/",
        "https://www.instagram.com/p/ABC_def123/extra",
        "https://www.instagram.com/p/ABC_def123/\n",
    ],
)
def test_unsupported_or_unsafe_link_does_not_start_a_worker(monkeypatch, url):
    extract, image, video = providers(monkeypatch, post())
    assert instagram.fetch_instagram(url) is None
    extract.assert_not_called()
    image.assert_not_called()
    video.assert_not_called()


@pytest.mark.parametrize("count", [None, 3])
def test_mixed_carousel_keeps_full_caption_order_decoded_geometry_and_one_deadline(monkeypatch, count):
    caption = "Длинная подпись 😀 <без обрезки>\n" * 400
    value = post(_ig_media_type=8, _ig_count=count, description=caption, entries=[item(1), item(2, 2, _ig_has_audio=True), item(3)])
    extract, image, video = providers(monkeypatch, value)

    result = instagram.fetch_instagram(URL)

    assert result is not None
    assert (result.site, result.url, result.author, result.username) == ("instagram", URL, "Космокот", "friends.cat")
    assert result.author_url == "https://www.instagram.com/friends.cat/"
    assert result.text == caption
    assert [asset.kind for asset in result.assets] == ["photo", "video", "photo"]
    assert [asset.data.decode().rsplit("/", 1)[1] for asset in result.assets] == ["1.jpg", "2.mp4", "3.jpg"]
    assert [(asset.width, asset.height) for asset in result.assets] == [(640, 480), (720, 1280), (640, 480)]
    extract.assert_called_once_with(URL, deadline=175, provider="instagram")
    assert video.call_args.kwargs["require_audio"] is True
    for call in [*image.call_args_list, *video.call_args_list]:
        assert call.kwargs["deadline"] == 175
        assert call.kwargs["allowed_hosts"] == (".cdninstagram.com", ".fbcdn.net")
        assert call.kwargs["referer"] == "https://www.instagram.com/"


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "another_post"},
        {"_ig_media_type": None},
        {"_ig_media_type": True},
        {"_ig_media_type": 7},
        {"_ig_media_type": 8, "entries": []},
        {"_ig_media_type": 8, "entries": [item(), "broken child"]},
        {"_ig_media_type": 8, "entries": [item(), {"thumbnails": item()["thumbnails"]}]},
        {"_ig_media_type": 8, "_ig_count": 3, "entries": [item(), item(2)]},
        {"_ig_media_type": 8, "_ig_count": True, "entries": [item()]},
        {"_ig_media_type": 8, "entries": [item()] * 21},
        {"_ig_count": 0},
        {"_ig_count": 2},
        {"entries": [item(2)]},
    ],
)
def test_ambiguous_or_incomplete_metadata_quietly_skips_without_media_requests(monkeypatch, changes):
    _, image, video = providers(monkeypatch, post(**changes))
    assert instagram.fetch_instagram(URL) is None
    image.assert_not_called()
    video.assert_not_called()


def test_captionless_post_and_absent_optional_author_are_supported(monkeypatch):
    providers(monkeypatch, post(description=None, channel=None, uploader=None))
    result = instagram.fetch_instagram(URL)
    assert result is not None and result.text == "" and result.author == "Instagram"
    assert result.author_url is None and result.username is None


@pytest.mark.parametrize(
    "formats",
    [
        [],
        [{"url": "https://scontent.cdninstagram.com/audio.mp4", "vcodec": "none"}],
        [{"url": "https://scontent.cdninstagram.com/silent.mp4", "acodec": "none"}],
        [{"url": "https://scontent.cdninstagram.com/stream.m3u8", "protocol": "m3u8_native"}],
        [{"url": "https://scontent.cdninstagram.com/dash.mp4", "manifest_url": "https://scontent.cdninstagram.com/list.mpd"}],
        [{"url": "https://scontent.cdninstagram.com/fragment.mp4", "fragments": [{"path": "fragment"}]}],
        [{"url": "https://scontent.cdninstagram.com/hevc.mp4", "vcodec": "hevc"}],
        [{"url": "https://scontent.cdninstagram.com.attacker.invalid/1.mp4"}],
    ],
)
def test_unusable_video_is_never_replaced_by_its_photo_thumbnail(monkeypatch, formats):
    _, image, video = providers(monkeypatch, post(**item(kind=2, _ig_has_audio=True, formats=formats)))
    assert instagram.fetch_instagram(URL) is None
    image.assert_not_called()
    video.assert_not_called()


def test_source_known_silent_video_does_not_require_audio(monkeypatch):
    value = post(**item(kind=2, _ig_has_audio=False, formats=[{"url": "https://scontent.cdninstagram.com/silent.mp4", "acodec": "none"}]))
    _, image, video = providers(monkeypatch, value)
    assert instagram.fetch_instagram(URL) is not None
    assert video.call_args.kwargs["require_audio"] is False
    image.assert_not_called()


def test_photo_selection_uses_last_upstream_candidate_when_sizes_are_absent(monkeypatch):
    candidates = [
        {"url": "https://scontent.cdninstagram.com/small.jpg"},
        {"url": "https://scontent.cdninstagram.com/large.jpg"},
        {"url": "http://127.0.0.1/private.jpg", "width": 10000, "height": 10000},
    ]
    _, image, _ = providers(monkeypatch, post(thumbnails=candidates))
    assert instagram.fetch_instagram(URL) is not None
    assert image.call_args.args[0] == candidates[1]["url"]


def test_one_missing_download_discards_the_incomplete_post(monkeypatch):
    _, image, _ = providers(monkeypatch, post(_ig_media_type=8, entries=[item(1), item(2), item(3)]))
    image.side_effect = [LinkAsset("photo", b"first", 10, 10), None]
    assert instagram.fetch_instagram(URL) is None
    assert image.call_count == 2


def test_downloads_share_total_byte_budget_and_reject_a_broken_helper_result(monkeypatch):
    _, image, _ = providers(monkeypatch, post(_ig_media_type=8, entries=[item(1), item(2)]))
    monkeypatch.setattr(instagram, "_MAX_BYTES", 5)
    image.side_effect = [LinkAsset("photo", b"four", 10, 10), LinkAsset("photo", b"more", 10, 10)]
    assert instagram.fetch_instagram(URL) is None
    assert [call.kwargs["max_bytes"] for call in image.call_args_list] == [5, 1]


def test_expired_metadata_deadline_does_not_start_downloads(monkeypatch):
    _, image, video = providers(monkeypatch, post())
    monkeypatch.setattr(instagram.time, "monotonic", Mock(side_effect=[100, 176]))
    assert instagram.fetch_instagram(URL) is None
    image.assert_not_called()
    video.assert_not_called()


def test_metadata_hook_preserves_real_upstream_mixed_media_and_source_counts():
    children = [
        {
            "pk": "12341",
            "media_type": 1,
            "image_versions2": {"candidates": [{"url": "https://scontent.cdninstagram.com/1.jpg", "width": 100, "height": 80}]},
        },
        {
            "pk": "12342",
            "media_type": 2,
            "has_audio": True,
            "video_versions": [{"url": "https://scontent.cdninstagram.com/2.mp4", "width": 80, "height": 100}],
        },
        {
            "pk": "12343",
            "media_type": 1,
            "image_versions2": {"candidates": [{"url": "https://scontent.cdninstagram.com/3.jpg", "width": 90, "height": 90}]},
        },
    ]
    product = {"pk": "1234", "media_type": 8, "carousel_media_count": 3, "carousel_media": children, "caption": {"text": "Полная подпись"}}
    with YoutubeDL({"quiet": True}) as client:
        value = instagram._NativeInstagramIE(client)._extract_product(product)
    assert value["_ig_media_type"] == 8 and value["_ig_count"] == 3
    assert value["description"] == "Полная подпись"
    assert [entry["_ig_media_type"] for entry in value["entries"]] == [1, 2, 1]
    assert value["entries"][1]["_ig_has_audio"] is True
    assert value["entries"][0]["formats"] == []
    assert value["entries"][0]["thumbnails"][0]["url"].endswith("1.jpg")
    assert value["entries"][1]["formats"][0]["url"].endswith("2.mp4")


def test_child_output_keeps_malformed_children_for_validation_and_discards_unrelated_metadata(monkeypatch):
    value = post(_ig_media_type=8, entries=[{**item(), "comments": [{"text": "PRIVATE_CANARY"}]}, "invalid"], comments=["PRIVATE_CANARY"])
    monkeypatch.setattr(instagram._NativeInstagramIE, "extract", lambda self, url: deepcopy(value))
    result = instagram._extract_instagram_info(URL)
    assert result is not None and result["entries"][1] == "invalid"
    assert "PRIVATE_CANARY" not in str(result)
    assert instagram._parse_info(result, "ABC_def123") is None


@pytest.mark.parametrize("broken", [None, "invalid", False])
def test_metadata_hook_rejects_a_source_child_that_upstream_would_silently_filter(broken):
    product = {"pk": "1234", "media_type": 8, "carousel_media": [{"pk": "12341", "media_type": 1}, broken]}
    with YoutubeDL({"quiet": True}) as client, pytest.raises(ValueError, match="Incomplete Instagram media metadata"):
        instagram._NativeInstagramIE(client)._extract_product(product)
