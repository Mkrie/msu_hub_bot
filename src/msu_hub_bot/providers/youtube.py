"""Bounded YouTube Shorts previews only when their video is available."""

from dataclasses import dataclass
import math
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from msu_hub_bot.providers.link_download import allowed_url, download_video, extract_info, request_json
from msu_hub_bot.providers.link_diagnostics import LinkReason, LinkStage, record_link_diagnostic
from msu_hub_bot.providers.link_models import LinkPost

_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be")
_VIDEO_ID = re.compile(r"[a-zA-Z0-9_-]{11}\Z")
_TIMESTAMP = re.compile(r"(?:\d+(?:\.\d+)?|(?:\d+h)?(?:\d+m)?(?:\d+s)?)\Z")
_MAX_SECONDS = 75
_SHORT_SECONDS = 180


@dataclass(frozen=True, slots=True)
class YouTubeLink:
    id: str
    url: str
    shorts: bool


def parse_youtube_url(url: str) -> YouTubeLink | None:
    """Normalize single-video links, removing tracking but keeping playback times."""
    if not allowed_url(url, _HOSTS):
        return None
    parts = urlsplit(url)
    try:
        query = parse_qs(parts.query, max_num_fields=64)
        fragment = parse_qs(parts.fragment, max_num_fields=64)
    except ValueError:
        return None
    path = parts.path.rstrip("/")
    shorts = path.startswith("/shorts/")
    if parts.hostname in ("youtu.be", "www.youtu.be"):
        video_id = path.removeprefix("/")
    elif path == "/watch":
        ids = query.get("v", [])
        video_id = ids[0] if len(ids) == 1 else ""
    elif path.startswith(("/shorts/", "/embed/", "/live/", "/v/")):
        video_id = path.split("/", 2)[2]
    else:
        return None
    if not _VIDEO_ID.fullmatch(video_id):
        return None
    canonical = f"https://www.youtube.com/shorts/{video_id}" if shorts else "https://www.youtube.com/watch"
    kept: dict[str, str] = {} if shorts else {"v": video_id}
    for key in ("t", "start", "end"):
        values = fragment.get(key) or query.get(key)
        if values and len(values) == 1 and values[0] and _TIMESTAMP.fullmatch(values[0]):
            kept[key] = values[0]
    return YouTubeLink(id=video_id, url=canonical + ("?" + urlencode(kept) if kept else ""), shorts=shorts)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _short_video(info: dict[str, Any]) -> bool:
    duration = info.get("duration")
    return (
        isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and math.isfinite(duration)
        and 0 < duration <= _SHORT_SECONDS
        and info.get("is_live") is not True
        and info.get("live_status") not in ("is_live", "is_upcoming", "post_live")
        and isinstance(info.get("formats"), list)
        and bool(info["formats"])
    )


def _oembed(url: str, deadline: float) -> dict[str, Any]:
    return (
        request_json(
            "https://www.youtube.com/oembed?" + urlencode({"url": url, "format": "json"}),
            deadline=min(deadline, time.monotonic() + 8),
            allowed_hosts=("www.youtube.com",),
            max_bytes=64 * 1024,
        )
        or {}
    )


def fetch_youtube(url: str) -> LinkPost | None:
    """Publish only a downloaded video; metadata alone never becomes a preview."""
    link = parse_youtube_url(url)
    if link is None:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.UNSUPPORTED)
        return None
    if not link.shorts:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.POLICY)
        return None
    deadline = time.monotonic() + _MAX_SECONDS
    info = extract_info(link.url, provider="youtube", deadline=min(deadline, time.monotonic() + 25))
    if not info:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.UNAVAILABLE)
        return None
    if info.get("id") not in (None, link.id) or info.get("_type") in ("playlist", "multi_video", "compat_list"):
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.INVALID_RESPONSE)
        return None
    if not _short_video(info):
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.POLICY)
        return None
    video = download_video(
        link.url,
        deadline=min(deadline - 8, time.monotonic() + 40),
        max_duration=_SHORT_SECONDS,
        require_audio=True,
    )
    if video is None or video.kind != "video" or not video.data:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.EMPTY)
        return None

    title = _text(info.get("title"))
    author = _text(info.get("channel")) or _text(info.get("uploader"))
    author_url = _text(info.get("channel_url")) or _text(info.get("uploader_url"))
    if not title or title == link.id or not author:
        fallback = _oembed(link.url, deadline)
        if not title or title == link.id:
            title = _text(fallback.get("title"))
        author = author or _text(fallback.get("author_name"))
        author_url = author_url or _text(fallback.get("author_url"))
    if not title or title == link.id:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.UNAVAILABLE)
        return None
    description = _text(info.get("description"))
    return LinkPost(
        site="youtube",
        url=link.url,
        author=author or "YouTube",
        author_url=author_url if allowed_url(author_url, _HOSTS) else None,
        title=title,
        text=description if description != title else "",
        assets=(video,),
    )
