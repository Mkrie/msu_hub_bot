"""Public YouTube cards and bounded Shorts with their original soundtrack."""

from dataclasses import dataclass
import math
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from msu_hub_bot.providers.link_download import allowed_url, download_image, download_video, extract_info, request_json
from msu_hub_bot.providers.link_diagnostics import LinkReason, LinkStage, record_link_diagnostic
from msu_hub_bot.providers.link_models import LinkAsset, LinkPost

_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be")
_THUMBNAIL_HOSTS = ("i.ytimg.com", "i1.ytimg.com", "i2.ytimg.com", "i3.ytimg.com", "i4.ytimg.com")
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


def _thumbnail(info: dict[str, Any]) -> str | None:
    direct = info.get("thumbnail") or info.get("thumbnail_url")
    if isinstance(direct, str) and allowed_url(direct, _THUMBNAIL_HOSTS):
        return direct
    alternatives = info.get("thumbnails")
    if isinstance(alternatives, list):
        for alternative in reversed(alternatives):
            value = alternative.get("url") if isinstance(alternative, dict) else None
            if isinstance(value, str) and allowed_url(value, _THUMBNAIL_HOSTS):
                return value
    return None


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


def fetch_youtube(url: str) -> LinkPost | None:
    """Keep useful public metadata even when the host cannot obtain video formats."""
    link = parse_youtube_url(url)
    if link is None:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.UNSUPPORTED)
        return None
    deadline = time.monotonic() + _MAX_SECONDS
    info = extract_info(link.url, provider="youtube", deadline=min(deadline, time.monotonic() + 25)) or {}
    if info.get("id") not in (None, link.id) or info.get("_type") in ("playlist", "multi_video", "compat_list"):
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.INVALID_RESPONSE)
        info = {}
    title = _text(info.get("title"))
    author = _text(info.get("channel")) or _text(info.get("uploader"))
    thumbnail = _thumbnail(info)
    author_url = _text(info.get("channel_url")) or _text(info.get("uploader_url"))
    if not title or title == link.id or not author or thumbnail is None:
        fallback = (
            request_json(
                "https://www.youtube.com/oembed?" + urlencode({"url": link.url, "format": "json"}),
                deadline=min(deadline, time.monotonic() + 8),
                allowed_hosts=("www.youtube.com",),
                max_bytes=64 * 1024,
            )
            or {}
        )
        if not title or title == link.id:
            title = _text(fallback.get("title"))
        author = author or _text(fallback.get("author_name"))
        author_url = author_url or _text(fallback.get("author_url"))
        thumbnail = thumbnail or _thumbnail(fallback)
    if not title or title == link.id:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.UNAVAILABLE)
        return None
    assets: tuple[LinkAsset, ...] = ()
    if link.shorts and _short_video(info):
        video = download_video(
            link.url,
            deadline=min(deadline - 8, time.monotonic() + 40),
            max_duration=_SHORT_SECONDS,
            require_audio=True,
        )
        if video is not None:
            assets = (video,)
    elif link.shorts:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.POLICY)
    if not assets and thumbnail is not None:
        picture = download_image(thumbnail, deadline=deadline, allowed_hosts=_THUMBNAIL_HOSTS)
        if picture is not None:
            assets = (picture,)
    if not assets:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.EMPTY)
    description = _text(info.get("description"))
    return LinkPost(
        site="youtube",
        url=link.url,
        author=author or "YouTube",
        author_url=author_url if allowed_url(author_url, _HOSTS) else None,
        title=title,
        text=description if description != title else "",
        assets=assets,
    )
