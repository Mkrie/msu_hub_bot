"""Bound public link requests and native downloads inside killable workers.

Host entries are exact names; a leading dot explicitly permits subdomains.
Absolute monotonic deadlines let a post share one budget across all its media.
"""

import io
import errno
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from collections.abc import Iterator
from typing import Any, Literal, cast
from urllib.parse import urljoin, urlsplit

from msu_hub_bot.execution.process import ProcessOutputTooLarge, run_process
from msu_hub_bot.providers.link_diagnostics import LinkReason, LinkStage, record_link_diagnostic
from msu_hub_bot.providers.link_models import LinkAsset

PHOTO_BYTES = 9 * 1024 * 1024
VIDEO_BYTES = 49 * 1024 * 1024
JSON_BYTES = 2 * 1024 * 1024
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/132.0.0.0 Safari/537.36"
_SOURCE_HOSTS = (
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "instagram.com",
    "www.instagram.com",
    "tiktok.com",
    "www.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
)
_STAGES = {
    "page": LinkStage.REQUEST,
    "json": LinkStage.REQUEST,
    "extract": LinkStage.EXTRACT,
    "image": LinkStage.IMAGE,
    "video": LinkStage.VIDEO,
}


class _WorkerFailure(ValueError):
    def __init__(self, reason: LinkReason, *, http_status: int | None = None) -> None:
        self.reason = reason
        self.http_status = http_status
        super().__init__(reason.value)


def classify_link_error(error: BaseException) -> tuple[LinkReason, int | None]:
    """Inspect known exception types and causes; never parse their messages."""
    from urllib.error import HTTPError as UrlHTTPError, URLError

    from PIL import Image, UnidentifiedImageError
    import requests
    from yt_dlp.networking.exceptions import HTTPError as YdlHTTPError, TransportError
    from yt_dlp.utils import DownloadError, ExtractorError, UnsupportedError

    pending = [error]
    seen: set[int] = set()
    fallback = LinkReason.PROCESS_ERROR
    while pending and len(seen) < 8:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, _WorkerFailure):
            return current.reason, current.http_status
        if isinstance(current, (subprocess.TimeoutExpired, TimeoutError, requests.Timeout)):
            return LinkReason.TIMEOUT, None
        if isinstance(current, (requests.HTTPError, UrlHTTPError, YdlHTTPError)):
            status = (
                current.response.status_code
                if isinstance(current, requests.HTTPError) and current.response is not None
                else current.code
                if isinstance(current, UrlHTTPError)
                else current.status
                if isinstance(current, YdlHTTPError)
                else None
            )
            return LinkReason.HTTP_ERROR, status if type(status) is int and 100 <= status <= 599 else None
        if isinstance(current, (ProcessOutputTooLarge, Image.DecompressionBombError)):
            return LinkReason.TOO_LARGE, None
        if isinstance(current, OSError) and current.errno == errno.EFBIG:
            return LinkReason.TOO_LARGE, None
        if isinstance(current, subprocess.CalledProcessError) and current.returncode == -signal.SIGXFSZ:
            return LinkReason.TOO_LARGE, None
        if isinstance(current, UnsupportedError):
            return LinkReason.UNSUPPORTED, None
        if isinstance(current, (requests.RequestException, URLError, TransportError, socket.gaierror, ConnectionError)):
            fallback = LinkReason.NETWORK_ERROR
        elif isinstance(current, (ValueError, TypeError, KeyError, UnidentifiedImageError)):
            if fallback != LinkReason.NETWORK_ERROR:
                fallback = LinkReason.INVALID_RESPONSE
        elif isinstance(current, (DownloadError, ExtractorError)) and fallback == LinkReason.PROCESS_ERROR:
            fallback = LinkReason.UNAVAILABLE
        for cause in (current.__cause__, current.__context__, getattr(current, "cause", None)):
            if isinstance(cause, BaseException):
                pending.append(cause)
        exc_info = getattr(current, "exc_info", None)
        if isinstance(exc_info, tuple) and len(exc_info) == 3 and isinstance(exc_info[1], BaseException):
            pending.append(exc_info[1])
        if isinstance(current, URLError) and isinstance(current.reason, BaseException):
            pending.append(current.reason)
    return fallback, None


def allowed_url(url: str, allowed_hosts: tuple[str, ...]) -> bool:
    """Reject credentials, ports, control characters and host lookalikes."""
    if len(url) > 16_384 or any(ord(character) < 33 or ord(character) == 127 for character in url):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        return (
            parts.scheme == "https"
            and parts.username is None
            and parts.password is None
            and parts.port is None
            and any(host == entry.lstrip(".") or (entry.startswith(".") and host.endswith(entry)) for entry in allowed_hosts)
        )
    except ValueError:
        return False


def _run(operation: str, payload: dict[str, Any], *, deadline: float, max_bytes: int = JSON_BYTES) -> tuple[dict[str, Any], bytes] | None:
    started = time.monotonic()
    stage = _STAGES[operation]
    remaining = deadline - started
    if not math.isfinite(remaining) or remaining <= 0:
        record_link_diagnostic(stage, LinkReason.TIMEOUT, duration_ms=0)
        return None
    try:
        with TemporaryDirectory(prefix="hub-link-") as directory:
            request = Path(directory) / "request.json"
            request.write_text(json.dumps({"operation": operation, **payload, "max_bytes": max_bytes}), encoding="utf-8")
            result = run_process(
                [sys.executable, "-m", __name__, str(request)],
                timeout=remaining,
                max_output_bytes=JSON_BYTES,
            )
            envelope = json.loads(result)
            if not isinstance(envelope, dict):
                raise _WorkerFailure(LinkReason.INVALID_RESPONSE)
            if envelope.get("ok") is False:
                raw_reason = envelope.get("reason")
                if not isinstance(raw_reason, str):
                    raise _WorkerFailure(LinkReason.INVALID_RESPONSE)
                reason = LinkReason(raw_reason)
                status = envelope.get("http_status")
                if reason not in {
                    LinkReason.HTTP_ERROR,
                    LinkReason.TIMEOUT,
                    LinkReason.NETWORK_ERROR,
                    LinkReason.TOO_LARGE,
                    LinkReason.INVALID_RESPONSE,
                    LinkReason.UNSUPPORTED,
                    LinkReason.PROCESS_ERROR,
                    LinkReason.UNAVAILABLE,
                } or (status is not None and (type(status) is not int or not 100 <= status <= 599)):
                    raise _WorkerFailure(LinkReason.INVALID_RESPONSE)
                raise _WorkerFailure(reason, http_status=status)
            metadata = envelope.get("metadata")
            if envelope.get("ok") is not True or not isinstance(metadata, dict):
                raise _WorkerFailure(LinkReason.INVALID_RESPONSE)
            binary = Path(directory) / "result.bin"
            data = b""
            if binary.exists():
                if binary.stat().st_size > max_bytes:
                    raise _WorkerFailure(LinkReason.TOO_LARGE)
                with binary.open("rb") as stream:
                    data = stream.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise _WorkerFailure(LinkReason.TOO_LARGE)
            if operation == "page" and not isinstance(metadata.get("url"), str):
                raise _WorkerFailure(LinkReason.INVALID_RESPONSE)
            if operation in {"image", "video"} and _asset((metadata, data), "photo" if operation == "image" else "video") is None:
                raise _WorkerFailure(LinkReason.INVALID_RESPONSE)
        record_link_diagnostic(stage, LinkReason.OK, duration_ms=min(300_000, max(0, round((time.monotonic() - started) * 1000))))
        return metadata, data
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        reason, status = classify_link_error(error)
        record_link_diagnostic(
            stage, reason, duration_ms=min(300_000, max(0, round((time.monotonic() - started) * 1000))), http_status=status
        )
        return None


def request_page(
    url: str,
    *,
    deadline: float,
    allowed_hosts: tuple[str, ...],
    max_bytes: int = JSON_BYTES,
) -> tuple[str, bytes] | None:
    if not 0 < max_bytes <= JSON_BYTES or not allowed_url(url, allowed_hosts):
        record_link_diagnostic(LinkStage.REQUEST, LinkReason.UNSUPPORTED)
        return None
    result = _run("page", {"url": url, "allowed_hosts": allowed_hosts}, deadline=deadline, max_bytes=max_bytes)
    if result is None or not isinstance(result[0].get("url"), str):
        return None
    return result[0]["url"], result[1]


def request_json(
    url: str,
    *,
    deadline: float,
    allowed_hosts: tuple[str, ...],
    max_bytes: int = JSON_BYTES,
) -> dict[str, Any] | None:
    if not 0 < max_bytes <= JSON_BYTES or not allowed_url(url, allowed_hosts):
        record_link_diagnostic(LinkStage.REQUEST, LinkReason.UNSUPPORTED)
        return None
    result = _run("json", {"url": url, "allowed_hosts": allowed_hosts}, deadline=deadline, max_bytes=max_bytes)
    return result[0] if result is not None else None


def extract_info(
    url: str,
    *,
    deadline: float,
    provider: Literal["youtube", "instagram", "tiktok"] | None = None,
    flat: bool = False,
) -> dict[str, Any] | None:
    if not allowed_url(url, _SOURCE_HOSTS):
        record_link_diagnostic(LinkStage.EXTRACT, LinkReason.UNSUPPORTED)
        return None
    result = _run("extract", {"url": url, "provider": provider, "flat": flat}, deadline=deadline)
    return result[0] if result is not None else None


def _asset(result: tuple[dict[str, Any], bytes] | None, kind: Literal["photo", "video"]) -> LinkAsset | None:
    if result is None or not result[1]:
        return None
    metadata, data = result
    width, height = metadata.get("width"), metadata.get("height")
    duration = metadata.get("duration")
    if not isinstance(width, int) or not isinstance(height, int) or not 0 < width <= 10_000 or not 0 < height <= 10_000:
        return None
    if duration is not None and (not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0):
        return None
    return LinkAsset(kind=kind, data=data, width=width, height=height, duration=duration)


def download_image(
    url: str,
    *,
    deadline: float,
    allowed_hosts: tuple[str, ...],
    referer: str | None = None,
    max_bytes: int = PHOTO_BYTES,
) -> LinkAsset | None:
    if not 0 < max_bytes <= PHOTO_BYTES or not allowed_url(url, allowed_hosts):
        record_link_diagnostic(LinkStage.IMAGE, LinkReason.UNSUPPORTED)
        return None
    return _asset(
        _run("image", {"url": url, "allowed_hosts": allowed_hosts, "referer": referer}, deadline=deadline, max_bytes=max_bytes), "photo"
    )


def download_video(
    url: str,
    *,
    deadline: float,
    max_duration: float = 180,
    allowed_hosts: tuple[str, ...] = (),
    referer: str | None = None,
    max_bytes: int = VIDEO_BYTES,
    require_audio: bool = False,
) -> LinkAsset | None:
    if not 0 < max_bytes <= VIDEO_BYTES or not math.isfinite(max_duration) or max_duration <= 0:
        record_link_diagnostic(LinkStage.VIDEO, LinkReason.UNSUPPORTED)
        return None
    if not allowed_url(url, allowed_hosts or _SOURCE_HOSTS):
        record_link_diagnostic(LinkStage.VIDEO, LinkReason.UNSUPPORTED)
        return None
    return _asset(
        _run(
            "video",
            {"url": url, "allowed_hosts": allowed_hosts, "referer": referer, "max_duration": max_duration, "require_audio": require_audio},
            deadline=deadline,
            max_bytes=max_bytes,
        ),
        "video",
    )


def _public_dns(url: str) -> None:
    host = urlsplit(url).hostname
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
        raise _WorkerFailure(LinkReason.UNSUPPORTED)


def _request(url: str, hosts: tuple[str, ...], max_bytes: int, referer: str | None = None) -> tuple[str, bytes, str]:
    import requests

    headers = {"User-Agent": _USER_AGENT}
    if referer and allowed_url(referer, _SOURCE_HOSTS):
        headers["Referer"] = referer
    with requests.Session() as session:
        session.trust_env = False
        for _ in range(5):
            if not allowed_url(url, hosts):
                raise _WorkerFailure(LinkReason.UNSUPPORTED)
            _public_dns(url)
            with session.get(url, headers=headers, timeout=8, allow_redirects=False, stream=True) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                response.raise_for_status()
                if response.status_code != 200:
                    raise _WorkerFailure(LinkReason.HTTP_ERROR, http_status=response.status_code)
                length = response.headers.get("Content-Length")
                if length is not None and int(length) > max_bytes:
                    raise _WorkerFailure(LinkReason.TOO_LARGE)
                content = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise _WorkerFailure(LinkReason.TOO_LARGE)
                return url, bytes(content), response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    raise _WorkerFailure(LinkReason.HTTP_ERROR)


class _QuietLogger:
    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


def ydl_options() -> dict[str, Any]:
    """Shared anonymous extraction policy; never load CLI or account configuration."""
    return {
        "quiet": True,
        "noprogress": True,
        "logger": _QuietLogger(),
        "noplaylist": True,
        "cachedir": False,
        "proxy": "",
        "socket_timeout": 8,
        "retries": 0,
        "extractor_retries": 0,
        "fragment_retries": 0,
        "file_access_retries": 0,
        "js_runtimes": {"deno": {}},
        "remote_components": [],
        "allowed_extractors": ["youtube", "instagram", "tiktok"],
        "ignore_no_formats_error": True,
    }


def _extract(url: str, provider: str | None, flat: bool) -> dict[str, Any]:
    if provider == "instagram":
        from msu_hub_bot.providers.instagram import _extract_instagram_info

        result = _extract_instagram_info(url)
        if result is None:
            raise _WorkerFailure(LinkReason.UNAVAILABLE)
        return result
    from msu_hub_bot.providers.ydl import _SingleVideoYoutubeDL

    with _SingleVideoYoutubeDL({**ydl_options(), "extract_flat": flat}) as client:
        info = client.extract_info(url, download=False, process=not flat)
        if not isinstance(info, dict) or info.get("_type") in ("playlist", "multi_video", "compat_list"):
            raise _WorkerFailure(LinkReason.UNSUPPORTED)
        return cast(dict[str, Any], client.sanitize_info(info))


def _image(payload: dict[str, Any], output: Path) -> dict[str, Any]:
    from PIL import Image, ImageOps

    _, data, content_type = _request(payload["url"], tuple(payload["allowed_hosts"]), payload["max_bytes"], payload.get("referer"))
    if not content_type.startswith("image/"):
        raise _WorkerFailure(LinkReason.INVALID_RESPONSE)
    with Image.open(io.BytesIO(data)) as source:
        if source.width * source.height > 30_000_000:
            raise _WorkerFailure(LinkReason.TOO_LARGE)
        if getattr(source, "n_frames", 1) != 1:
            raise _WorkerFailure(LinkReason.UNSUPPORTED)
        source.load()
        picture = ImageOps.exif_transpose(source).convert("RGB")
        if max(picture.size) / min(picture.size) > 20:
            raise _WorkerFailure(LinkReason.UNSUPPORTED)
        picture.thumbnail((2560, 2560))
        picture.save(output, format="JPEG", quality=90, optimize=True)
        return {"width": picture.width, "height": picture.height}


def _video_policy(info: dict[str, Any], max_duration: float) -> bool:
    duration = info.get("duration")
    return (
        isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and math.isfinite(duration)
        and 0 < duration <= max_duration
        and info.get("is_live") is not True
        and info.get("live_status") not in ("is_live", "is_upcoming", "post_live")
        and info.get("_type") not in ("playlist", "multi_video", "compat_list")
    )


def _format_size(info: dict[str, Any]) -> int | None:
    for key in ("filesize", "filesize_approx"):
        value = info.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
            return int(value)
    return None


def _select_formats(context: dict[str, Any], max_bytes: int) -> Iterator[dict[str, Any]]:
    """Choose a fitting rendition before starting a single bounded download."""
    # yt-dlp supplies formats in ascending preference, including language/quality.
    formats = [item for item in reversed(context.get("formats", [])) if item.get("protocol") == "https"]

    def h264(item: dict[str, Any]) -> bool:
        return str(item.get("vcodec", "")).startswith(("h264", "avc1")) and item.get("ext") == "mp4"

    def aac(item: dict[str, Any]) -> bool:
        return str(item.get("acodec", "")).startswith(("aac", "mp4a"))

    videos = [item for item in formats if h264(item) and item.get("acodec") == "none" and 0 < (item.get("height") or 0) <= 1080]
    audios = [item for item in formats if aac(item) and item.get("vcodec") == "none" and item.get("ext") == "m4a"]
    combined = [item for item in formats if h264(item) and aac(item)]

    def fits(items: tuple[dict[str, Any], ...], unknown: bool) -> bool:
        sizes = [_format_size(item) for item in items]
        return (None in sizes) == unknown and sum(size or 0 for size in sizes) <= max_bytes

    for unknown in (False, True):
        for video in videos:
            for audio in audios:
                if fits((video, audio), unknown):
                    yield {
                        "format_id": f"{video['format_id']}+{audio['format_id']}",
                        "ext": "mp4",
                        "requested_formats": [video, audio],
                        "protocol": "https+https",
                    }
                    return
        for item in combined:
            if fits((item,), unknown):
                yield item
                return


def _download_ydl(url: str, output: Path, max_bytes: int, max_duration: float) -> None:
    from msu_hub_bot.providers.ydl import _SingleVideoYoutubeDL

    downloaded: dict[str, int] = {}

    def progress(status: dict[str, Any]) -> None:
        downloaded[str(status.get("filename", ""))] = int(status.get("downloaded_bytes") or 0)
        if sum(downloaded.values()) > max_bytes:
            raise _WorkerFailure(LinkReason.TOO_LARGE)

    def policy(info: dict[str, Any], *, incomplete: bool) -> str | None:
        return None if incomplete or _video_policy(info, max_duration) else "Video outside automatic-download policy"

    options = {
        **ydl_options(),
        "ignore_no_formats_error": False,
        "max_filesize": max_bytes,
        "outtmpl": str(output.parent / "media.%(ext)s"),
        "nopart": True,
        "continuedl": False,
        "format": lambda context: _select_formats(context, max_bytes),
        "merge_output_format": "mp4",
        "fixup": "never",
        "postprocessor_args": {
            "merger+ffmpeg_i": ["-protocol_whitelist", "file,pipe", "-format_whitelist", "mov"],
            "videoremuxer+ffmpeg_i": ["-protocol_whitelist", "file,pipe", "-format_whitelist", "mov"],
        },
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        "progress_hooks": [progress],
        "match_filter": policy,
    }
    with _SingleVideoYoutubeDL(options) as client:
        info = client.extract_info(url, download=False)
        if not isinstance(info, dict) or not _video_policy(info, max_duration):
            raise _WorkerFailure(LinkReason.UNSUPPORTED)
        client.process_ie_result(info, download=True)
    media = output.parent / "media.mp4"
    if not media.exists():
        raise _WorkerFailure(LinkReason.UNAVAILABLE)
    if media.stat().st_size > max_bytes:
        raise _WorkerFailure(LinkReason.TOO_LARGE)
    if not media.stat().st_size:
        raise _WorkerFailure(LinkReason.INVALID_RESPONSE)
    media.replace(output)


def _video(payload: dict[str, Any], output: Path) -> dict[str, Any]:
    if payload["allowed_hosts"]:
        _, data, content_type = _request(payload["url"], tuple(payload["allowed_hosts"]), payload["max_bytes"], payload.get("referer"))
        if content_type not in ("video/mp4", "application/octet-stream") or b"ftyp" not in data[:32]:
            raise _WorkerFailure(LinkReason.INVALID_RESPONSE)
        output.write_bytes(data)
    else:
        _download_ydl(payload["url"], output, payload["max_bytes"], payload["max_duration"])
    probe_path = output.parent / "probe.json"
    with probe_path.open("wb") as probe:
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                "mov",
                "-show_entries",
                "stream=codec_type,codec_name,width,height:format=duration",
                "-of",
                "json",
                str(output),
            ],
            stdout=probe,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=8,
        )
    if probe_path.stat().st_size > 128 * 1024:
        raise _WorkerFailure(LinkReason.TOO_LARGE)
    info = json.loads(probe_path.read_bytes())
    streams = info.get("streams", [])
    video: dict[str, Any] = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    audio: dict[str, Any] = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
    duration = float(info.get("format", {}).get("duration", 0))
    if (
        video.get("codec_name") != "h264"
        or (audio and audio.get("codec_name") != "aac")
        or (payload.get("require_audio") and not audio)
        or not 0 < duration <= payload["max_duration"] + 1
    ):
        raise _WorkerFailure(LinkReason.UNSUPPORTED)
    return {"width": video["width"], "height": video["height"], "duration": duration}


def _main() -> None:
    import resource

    # Children do not need application secrets, proxy configuration or browser profiles.
    child_environment = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}
    os.environ.clear()
    os.environ.update(child_environment)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        request = Path(sys.argv[1])
        payload = json.loads(request.read_text(encoding="utf-8"))
        resource.setrlimit(resource.RLIMIT_FSIZE, (VIDEO_BYTES, VIDEO_BYTES))
        output = request.parent / "result.bin"
        match payload["operation"]:
            case "page":
                url, data, _ = _request(payload["url"], tuple(payload["allowed_hosts"]), payload["max_bytes"])
                output.write_bytes(data)
                metadata = {"url": url}
            case "json":
                _, data, _ = _request(payload["url"], tuple(payload["allowed_hosts"]), payload["max_bytes"])
                metadata = json.loads(data)
                if not isinstance(metadata, dict):
                    raise _WorkerFailure(LinkReason.INVALID_RESPONSE)
            case "extract":
                metadata = _extract(payload["url"], payload.get("provider"), payload["flat"])
            case "image":
                metadata = _image(payload, output)
            case "video":
                metadata = _video(payload, output)
            case _:
                raise _WorkerFailure(LinkReason.UNSUPPORTED)
        encoded = json.dumps({"ok": True, "metadata": metadata}, ensure_ascii=False, allow_nan=False)
    except Exception as error:
        reason, status = classify_link_error(error)
        encoded = json.dumps({"ok": False, "reason": reason.value, "http_status": status})
    print(encoded)


if __name__ == "__main__":
    _main()
