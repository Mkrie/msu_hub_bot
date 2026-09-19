"""YouTube cards, native sound and the shared public-download boundary."""

import io
import json
from pathlib import Path
import subprocess
import time
from unittest.mock import MagicMock

from PIL import Image
import pytest
import requests

from msu_hub_bot.providers import link_download as download
from msu_hub_bot.providers import youtube
from msu_hub_bot.providers.link_models import LinkAsset

VIDEO_ID = "jNQXAC9IVRw"
WATCH = f"https://www.youtube.com/watch?v={VIDEO_ID}"
SHORT = f"https://www.youtube.com/shorts/{VIDEO_ID}"
THUMB = f"https://i.ytimg.com/vi/{VIDEO_ID}/hqdefault.jpg"
PHOTO = LinkAsset("photo", b"jpeg", 480, 360)
VIDEO = LinkAsset("video", b"mp4", 144, 198, 3)


@pytest.mark.parametrize(
    "url,expected,shorts",
    [
        (WATCH + "&list=playlist&si=tracking&t=1m2s", WATCH + "&t=1m2s", False),
        (f"https://youtu.be/{VIDEO_ID}?si=tracking#t=42", WATCH + "&t=42", False),
        (f"https://www.youtu.be/{VIDEO_ID}", WATCH, False),
        (f"https://music.youtube.com/watch?v={VIDEO_ID}", WATCH, False),
        (f"https://m.youtube.com/watch?v={VIDEO_ID}&t=5#t=8", WATCH + "&t=8", False),
        (SHORT + "?si=tracking&start=4&end=10", SHORT + "?start=4&end=10", True),
        (f"https://youtube.com/embed/{VIDEO_ID}/?start=1", WATCH + "&start=1", False),
        (f"https://www.youtube.com/live/{VIDEO_ID}", WATCH, False),
    ],
)
def test_single_video_urls_preserve_times_and_drop_tracking(url, expected, shorts):
    parsed = youtube.parse_youtube_url(url)
    assert parsed == youtube.YouTubeLink(VIDEO_ID, expected, shorts)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/playlist?list=example",
        "https://www.youtube.com/@channel",
        "https://www.youtube.com/watch?v=short",
        WATCH + "&v=18NGQq7p3LY",
        "https://www.youtube.com.evil.test/watch?v=" + VIDEO_ID,
        "https://www.youtube.com@127.0.0.1/watch?v=" + VIDEO_ID,
        "https://www.youtube.com:443/watch?v=" + VIDEO_ID,
        "https://youtu.be/" + VIDEO_ID + "/extra",
        "http://www.youtube.com/watch?v=" + VIDEO_ID,
        WATCH + "\n",
    ],
)
def test_invalid_or_collection_routes_do_not_start_work(monkeypatch, url):
    extract = MagicMock()
    monkeypatch.setattr(youtube, "extract_info", extract)
    assert youtube.fetch_youtube(url) is None
    extract.assert_not_called()


def install(monkeypatch, info=None, fallback=None, video=VIDEO, photo=PHOTO):
    methods = {
        "extract_info": MagicMock(return_value=info),
        "request_json": MagicMock(return_value=fallback),
        "download_video": MagicMock(return_value=video),
        "download_image": MagicMock(return_value=photo),
    }
    for name, method in methods.items():
        monkeypatch.setattr(youtube, name, method)
    return methods


def metadata(**changes):
    return {
        "id": VIDEO_ID,
        "title": "A useful video",
        "channel": "A channel",
        "channel_url": "https://www.youtube.com/@channel",
        "thumbnail": THUMB,
        "description": "Complete text with <literal> markup.\n\n00:01 First chapter",
        "duration": 3,
        "formats": [{"format_id": "video"}],
        "live_status": "not_live",
        **changes,
    }


def test_watch_is_a_full_metadata_card_and_never_downloads_video(monkeypatch):
    methods = install(monkeypatch, metadata())
    result = youtube.fetch_youtube(WATCH + "&t=1m2s&list=abc")
    assert result.url == WATCH + "&t=1m2s"
    assert result.title == "A useful video"
    assert result.author == "A channel"
    assert result.author_url == "https://www.youtube.com/@channel"
    assert result.text == metadata()["description"]
    assert result.assets == (PHOTO,)
    methods["download_video"].assert_not_called()
    methods["request_json"].assert_not_called()


def test_short_keeps_source_time_sound_and_actual_geometry(monkeypatch):
    methods = install(monkeypatch, metadata())
    result = youtube.fetch_youtube(SHORT + "?t=2")
    assert result.assets == (VIDEO,)
    assert (result.assets[0].width, result.assets[0].height) == (144, 198)
    assert result.url == SHORT + "?t=2"
    call = methods["download_video"].call_args
    assert call.args == (SHORT + "?t=2",)
    assert call.kwargs["require_audio"] is True
    assert call.kwargs["max_duration"] == 180
    methods["download_image"].assert_not_called()


@pytest.mark.parametrize(
    "fields",
    [
        {"duration": None},
        {"duration": float("nan")},
        {"duration": True},
        {"duration": 181},
        {"duration": 0},
        {"is_live": True},
        {"live_status": "is_upcoming"},
        {"live_status": "post_live"},
        {"formats": []},
    ],
)
def test_unknown_or_unbounded_short_remains_a_card(monkeypatch, fields):
    methods = install(monkeypatch, metadata(**fields))
    assert youtube.fetch_youtube(SHORT).assets == (PHOTO,)
    methods["download_video"].assert_not_called()


def test_failed_short_download_keeps_useful_card(monkeypatch):
    install(monkeypatch, metadata(), video=None)
    assert youtube.fetch_youtube(SHORT).assets == (PHOTO,)


def test_official_oembed_card_works_without_playback_metadata(monkeypatch):
    methods = install(
        monkeypatch,
        fallback={
            "title": "Public title",
            "author_name": "Public author",
            "author_url": "https://www.youtube.com/@author",
            "thumbnail_url": THUMB,
            "html": '<iframe src="https://untrusted.test"></iframe>',
        },
    )
    result = youtube.fetch_youtube(SHORT)
    assert result.title == "Public title" and result.author == "Public author"
    assert result.text == "" and result.assets == (PHOTO,)
    methods["download_video"].assert_not_called()
    call = methods["request_json"].call_args
    assert call.args[0].startswith("https://www.youtube.com/oembed?")
    assert call.kwargs["allowed_hosts"] == ("www.youtube.com",)
    assert call.kwargs["max_bytes"] == 65536


def test_malformed_optional_metadata_does_not_hide_title(monkeypatch):
    methods = install(monkeypatch, metadata(channel_url="https://evil.test", thumbnail="https://127.0.0.1/private"), photo=None)
    result = youtube.fetch_youtube(WATCH)
    assert result.title == "A useful video"
    assert result.assets == () and result.author_url is None
    methods["download_image"].assert_not_called()


@pytest.mark.parametrize("info", [None, {"title": VIDEO_ID}, metadata(id="other"), metadata(_type="playlist")])
def test_failed_or_mismatched_metadata_does_not_invent_a_post(monkeypatch, info):
    install(monkeypatch, info)
    assert youtube.fetch_youtube(WATCH) is None


def test_provider_operations_share_one_deadline_with_reserved_card_fallback(monkeypatch):
    monkeypatch.setattr(youtube.time, "monotonic", lambda: 100)
    methods = install(monkeypatch, metadata(), video=None)
    youtube.fetch_youtube(SHORT)
    assert methods["extract_info"].call_args.kwargs["deadline"] == 125
    assert methods["download_video"].call_args.kwargs["deadline"] == 140
    assert methods["download_image"].call_args.kwargs["deadline"] == 175


@pytest.mark.parametrize(
    "url,hosts,expected",
    [
        ("https://cdn.test/image", ("cdn.test",), True),
        ("https://a.cdn.test/image", ("cdn.test",), False),
        ("https://a.cdn.test/image", (".cdn.test",), True),
        ("https://cdn.test/image", (".cdn.test",), True),
        ("https://badcdn.test/image", (".cdn.test",), False),
        ("https://cdn.test.evil.test/image", (".cdn.test",), False),
        ("https://user:pass@cdn.test/image", ("cdn.test",), False),
        ("https://cdn.test:443/image", ("cdn.test",), False),
        ("http://cdn.test/image", ("cdn.test",), False),
        ("https://cdn.test/image\r\n", ("cdn.test",), False),
    ],
)
def test_explicit_host_policy(url, hosts, expected):
    assert download.allowed_url(url, hosts) is expected


def test_worker_cleans_private_files_and_bounds_output(monkeypatch):
    directories = []

    def process(command, *, timeout, max_output_bytes):
        request = Path(command[-1])
        directories.append(request.parent)
        payload = json.loads(request.read_text())
        assert command[1:3] == ["-m", "msu_hub_bot.providers.link_download"]
        assert 0 < timeout <= 10 and max_output_bytes == download.JSON_BYTES
        assert payload["url"] == THUMB and payload["operation"] == "image"
        (request.parent / "result.bin").write_bytes(b"jpeg")
        return b'{"width":480,"height":360}'

    monkeypatch.setattr(download, "run_process", process)
    result = download.download_image(THUMB, deadline=time.monotonic() + 10, allowed_hosts=("i.ytimg.com",))
    assert result == PHOTO
    assert not directories[0].exists()


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired("child", 1),
        subprocess.CalledProcessError(1, "child"),
        ValueError("bad JSON"),
        OSError("failed child"),
    ],
)
def test_worker_failure_is_quiet_and_cleans_tempfiles(monkeypatch, failure):
    directories = []

    def process(command, **kwargs):
        directory = Path(command[-1]).parent
        directories.append(directory)
        (directory / "partial.mp4").write_bytes(b"partial")
        raise failure

    monkeypatch.setattr(download, "run_process", process)
    assert download.download_video(SHORT, deadline=time.monotonic() + 10) is None
    assert not directories[0].exists()


def test_expired_worker_never_starts_a_child(monkeypatch):
    process = MagicMock()
    monkeypatch.setattr(download, "run_process", process)
    assert download.extract_info(WATCH, deadline=time.monotonic() - 1) is None
    process.assert_not_called()


def response(body=b"hello", *, status=200, headers=None):
    result = MagicMock()
    result.__enter__.return_value = result
    result.status_code = status
    result.headers = headers or {}
    result.iter_content.return_value = [body[:2], body[2:]]
    return result


def session(monkeypatch, *responses):
    client = MagicMock()
    client.__enter__.return_value = client
    client.get.side_effect = responses
    monkeypatch.setattr(requests, "Session", lambda: client)
    monkeypatch.setattr(download, "_public_dns", lambda url: None)
    return client


def test_requests_validate_each_redirect_and_ignore_environment_proxy(monkeypatch):
    redirect = response(status=302, headers={"Location": "https://private.test/secret"})
    client = session(monkeypatch, redirect)
    with pytest.raises(ValueError, match="host policy"):
        download._request("https://cdn.test/start", ("cdn.test",), 1024)
    assert client.trust_env is False
    assert client.get.call_count == 1
    assert client.get.call_args.kwargs["allow_redirects"] is False
    redirect.__exit__.assert_called_once()


@pytest.mark.parametrize("headers", [{"Content-Length": "50"}, {}])
def test_known_and_streaming_response_overflow_are_rejected(monkeypatch, headers):
    source = response(b"12345678901", headers=headers)
    session(monkeypatch, source)
    with pytest.raises(ValueError, match="size limit"):
        download._request("https://cdn.test/image", ("cdn.test",), 10)
    source.__exit__.assert_called_once()


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"])
def test_dns_private_addresses_are_rejected(monkeypatch, address):
    monkeypatch.setattr(download.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, (address, 443))])
    with pytest.raises(ValueError, match="Non-public"):
        download._public_dns("https://cdn.test/image")


def test_image_is_decoded_normalized_and_loses_metadata(monkeypatch, tmp_path):
    source = Image.new("RGB", (12, 8), "red")
    buffer = io.BytesIO()
    source.save(buffer, format="PNG")
    monkeypatch.setattr(download, "_request", lambda *args: (THUMB, buffer.getvalue(), "image/png"))
    path = tmp_path / "output.bin"
    assert download._image({"url": THUMB, "allowed_hosts": ["i.ytimg.com"], "max_bytes": 10000}, path) == {"width": 12, "height": 8}
    with Image.open(path) as image:
        assert image.format == "JPEG" and image.size == (12, 8)
        assert not image.getexif()


def test_extractor_options_are_anonymous_and_have_no_runtime_downloads():
    options = download.ydl_options()
    assert options["proxy"] == "" and options["cachedir"] is False
    assert options["remote_components"] == [] and options["js_runtimes"] == {"deno": {}}
    assert options["noplaylist"] is True and options["retries"] == 0
    assert not any(key in options for key in ("cookiefile", "cookiesfrombrowser", "username", "password"))


def video_format(name, *, size=None, height=720, acodec="none", vcodec="avc1.64001f", protocol="https", **extra):
    return {
        "format_id": name,
        "ext": "m4a" if vcodec == "none" else "mp4",
        "url": f"https://media.example.test/{name}",
        "protocol": protocol,
        "width": 1280,
        "height": height,
        "vcodec": vcodec,
        "acodec": acodec,
        "filesize": size,
        **extra,
    }


def test_real_extractor_selection_chooses_smaller_fitting_video_before_download():
    from yt_dlp import YoutubeDL

    formats = [
        video_format("audio", size=2_000_000, vcodec="none", acodec="mp4a.40.2"),
        video_format("720", size=20_000_000),
        video_format("1080", height=1080, size=60_000_000),
    ]
    options = {**download.ydl_options(), "format": lambda context: download._select_formats(context, download.VIDEO_BYTES)}
    with YoutubeDL(options) as client:
        selected = client.process_ie_result(
            {"id": "synthetic", "title": "Synthetic video", "duration": 10, "formats": formats}, download=False
        )
    assert [item["format_id"] for item in selected["requested_formats"]] == ["720", "audio"]


def test_selector_counts_audio_together_with_video_and_uses_estimated_sizes():
    formats = [
        video_format("audio", size=10_000_000, vcodec="none", acodec="aac"),
        video_format("720", filesize_approx=20_000_000),
        video_format("1080", height=1080, filesize_approx=45_000_000),
    ]
    chosen = list(download._select_formats({"formats": formats}, 49_000_000))
    assert [item["format_id"] for item in chosen[0]["requested_formats"]] == ["720", "audio"]


def test_selector_prefers_known_fitting_rendition_to_unbounded_highest_quality():
    formats = [
        video_format("audio", size=2_000_000, vcodec="none", acodec="aac"),
        video_format("720", size=20_000_000),
        video_format("1080", height=1080),
    ]
    assert next(download._select_formats({"formats": formats}, 49_000_000))["format_id"] == "720+audio"


def test_selector_supports_one_bounded_unknown_size_attempt_without_alternate_downloads():
    formats = [
        video_format("audio", vcodec="none", acodec="mp4a.40.2"),
        video_format("720"),
        video_format("1080", height=1080),
    ]
    assert [item["format_id"] for item in download._select_formats({"formats": formats}, 49_000_000)] == ["1080+audio"]


def test_progressive_selector_skips_oversize_and_incompatible_audio_formats():
    formats = [
        video_format("compatible", size=20_000_000, acodec="aac", vcodec="h264"),
        video_format("opus", size=20_000_000, acodec="opus"),
        video_format("too-large", size=60_000_000, acodec="aac"),
    ]
    assert next(download._select_formats({"formats": formats}, 49_000_000))["format_id"] == "compatible"


def test_native_selector_rejects_playlists_and_known_oversize_sources():
    formats = [
        video_format("playlist", size=20_000_000, acodec="aac", protocol="m3u8_native"),
        video_format("too-large", size=60_000_000, acodec="aac"),
    ]
    assert list(download._select_formats({"formats": formats}, 49_000_000)) == []


def test_download_policy_and_codec_selector_keep_sound(monkeypatch, tmp_path):
    from yt_dlp import YoutubeDL
    from yt_dlp.postprocessor import FFmpegMergerPP, FFmpegVideoRemuxerPP

    client = MagicMock()
    client.__enter__.return_value = client
    client.extract_info.return_value = {"duration": 3, "live_status": "not_live"}
    client.process_ie_result.side_effect = lambda *args, **kwargs: (tmp_path / "media.mp4").write_bytes(b"mp4")
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("msu_hub_bot.providers.ydl._SingleVideoYoutubeDL", factory)
    download._download_ydl(SHORT, tmp_path / "output", 1000, 180)
    options = factory.call_args.args[0]
    assert options["merge_output_format"] == "mp4"
    assert callable(options["format"])
    assert options["fixup"] == "never"
    with YoutubeDL(options) as real_client:
        for processor in (FFmpegMergerPP(real_client), FFmpegVideoRemuxerPP(real_client)):
            for keys in (["_i1", "_i"], ["_i2", "_i"]):
                assert processor._configuration_args("ffmpeg", keys) == [
                    "-protocol_whitelist",
                    "file,pipe",
                    "-format_whitelist",
                    "mov",
                ]
    assert options["match_filter"]({"duration": None}, incomplete=False)
    assert options["match_filter"]({"duration": 181}, incomplete=False)
    assert options["match_filter"]({"duration": 3, "is_live": True}, incomplete=False)
    assert options["match_filter"]({"duration": 3}, incomplete=False) is None
    hook = options["progress_hooks"][0]
    hook({"filename": "video", "downloaded_bytes": 600})
    with pytest.raises(ValueError, match="aggregate"):
        hook({"filename": "audio", "downloaded_bytes": 600})


@pytest.mark.parametrize("audio,required,ok", [("aac", True, True), (None, True, False), (None, False, True), ("opus", False, False)])
def test_finished_video_must_retain_supported_audio_when_required(monkeypatch, tmp_path, audio, required, ok):
    def get_video(url, output, *args):
        output.write_bytes(b"mp4")

    def probe(command, *, stdout, **kwargs):
        assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
        assert command[command.index("-format_whitelist") + 1] == "mov"
        streams = [{"codec_type": "video", "codec_name": "h264", "width": 144, "height": 198}]
        if audio:
            streams.append({"codec_type": "audio", "codec_name": audio})
        stdout.write(json.dumps({"streams": streams, "format": {"duration": "3.0"}}).encode())

    monkeypatch.setattr(download, "_download_ydl", get_video)
    monkeypatch.setattr(download.subprocess, "run", probe)
    payload = {"url": SHORT, "allowed_hosts": [], "max_bytes": 1000, "max_duration": 180, "require_audio": required}
    if ok:
        assert download._video(payload, tmp_path / "output") == {"width": 144, "height": 198, "duration": 3.0}
    else:
        with pytest.raises(ValueError, match="video/audio"):
            download._video(payload, tmp_path / "output")
