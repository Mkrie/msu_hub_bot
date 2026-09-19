"""Public source identities for diagnostics, never resolved download URLs."""

import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def source_metadata(value: str) -> dict[str, str]:
    """Retain recognized public post paths; unknown routes expose only their origin."""
    if not isinstance(value, str) or len(value) > 16384 or any(ord(char) < 33 or ord(char) == 127 for char in value) or "\\" in value:
        return {}
    try:
        url = urlsplit(value)
        pairs = parse_qsl(url.query, max_num_fields=100)
        query = {key: item for key, item in pairs if sum(other == key for other, _ in pairs) == 1}
        host = (url.hostname or "").encode("idna").decode("ascii").lower()
        if url.scheme not in {"https", "http"} or url.username is not None or url.password is not None or url.port is not None:
            return {}
        if not host or "." not in host or host.endswith((".local", ".internal", ".localhost")):
            return {}
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return {}
    except UnicodeError, ValueError:
        return {}
    # Media endpoints and signed downloads are not source pages, even if supplied as input.
    if any(
        host == suffix or host.endswith("." + suffix)
        for suffix in (
            "twimg.com",
            "googlevideo.com",
            "ytimg.com",
            "cdninstagram.com",
            "fbcdn.net",
            "tiktokcdn.com",
            "tiktokcdn-us.com",
            "tiktokcdn-eu.com",
            "byteoversea.com",
            "ibytedtos.com",
        )
    ):
        return {}
    name = host.removeprefix("www.").removeprefix("m.").removeprefix("mobile.")
    path = ""
    params: list[tuple[str, str]] = []
    if name in {"x.com", "twitter.com", "fixupx.com", "fxtwitter.com", "twittpr.com", "xfixup.com"}:
        match = re.fullmatch(r"/(?:[A-Za-z0-9_]{1,15}|i/web)/status/(\d{1,20})(/(?:photo|video)/[1-9]\d?)?/?", url.path)
        if match:
            host, path = "x.com", "/i/web/status/" + match[1] + (match[2] or "")
    elif name in {"youtube.com", "music.youtube.com", "youtu.be"}:
        candidate = query.get("v", "") if url.path == "/watch" else url.path.rstrip("/").rsplit("/", 1)[-1]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) and (
            (name == "youtu.be" and re.fullmatch(r"/[A-Za-z0-9_-]{11}/?", url.path))
            or (name != "youtu.be" and (url.path == "/watch" or re.fullmatch(r"/(?:shorts|embed|live|v)/[A-Za-z0-9_-]{11}/?", url.path)))
        ):
            host, path = "www.youtube.com", "/watch"
            params = [("v", candidate)]
            for key in ("t", "start", "end"):
                if re.fullmatch(r"(?:\d{1,6}|(?:\d{1,3}h)?(?:\d{1,3}m)?(?:\d{1,3}s)?)", query.get(key, "")) and query.get(key):
                    params.append((key, query[key]))
    elif name == "instagram.com":
        if match := re.fullmatch(r"/(?:[A-Za-z0-9_.]{1,30}/)?(p|reel|reels|tv)/([A-Za-z0-9_-]{5,64})/?", url.path):
            kind = "reel" if match[1] == "reels" else match[1]
            host, path = "www.instagram.com", f"/{kind}/{match[2]}/"
    elif name == "tiktok.com":
        if re.fullmatch(r"/@[A-Za-z0-9_.]{1,64}/(?:video|photo)/\d{1,20}/?", url.path):
            host, path = "www.tiktok.com", url.path.rstrip("/")
        elif re.fullmatch(r"/t/[A-Za-z0-9_-]{1,64}/?", url.path):
            path = url.path.rstrip("/")
    elif name in {"vt.tiktok.com", "vm.tiktok.com"}:
        if re.fullmatch(r"/[A-Za-z0-9_-]{1,64}/?", url.path):
            path = url.path.rstrip("/")
    elif name in {"vk.com", "vk.ru"}:
        for candidate in (url.path.lstrip("/"), query.get("w", ""), query.get("z", "")):
            if re.fullmatch(r"(?:wall|video|clip)-?\d{1,20}_\d{1,20}", candidate):
                host, path = "vk.com", "/" + candidate
                break
    elif name in {"vimeo.com", "dailymotion.com", "dai.ly", "twitch.tv", "clips.twitch.tv", "reddit.com", "redd.it", "rutube.ru"}:
        patterns = {
            "vimeo.com": r"/\d{1,20}/?",
            "dailymotion.com": r"/video/[A-Za-z0-9]{1,32}/?",
            "dai.ly": r"/[A-Za-z0-9]{1,32}/?",
            "twitch.tv": r"/(?:videos/\d{1,20}|[A-Za-z0-9_]{1,25}/clip/[A-Za-z0-9_-]{1,80})/?",
            "clips.twitch.tv": r"/[A-Za-z0-9_-]{1,80}/?",
            "reddit.com": r"/r/[A-Za-z0-9_]{1,32}/comments/[A-Za-z0-9]{1,16}(?:/[^/]{0,100})?/?",
            "redd.it": r"/[A-Za-z0-9]{1,16}/?",
            "rutube.ru": r"/video/[a-f0-9]{32}/?",
        }
        if re.fullmatch(patterns[name], url.path):
            path = url.path.rstrip("/")
            if name == "reddit.com":
                path = "/".join(path.split("/")[:5])
    safe = urlunsplit(("https", host, path or "/", urlencode(params), ""))
    if len(safe) > 160:
        safe, path = f"https://{host}/", ""
    if len(safe) > 160:
        return {}
    return {"link.source_url": safe, "link.url_scope": "post" if path else "origin"}
