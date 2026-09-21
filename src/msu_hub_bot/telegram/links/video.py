"""Generic yt-dlp presentation with ordinary video emojis."""

from msu_hub_bot.providers.vk.utils import href
from msu_hub_bot.providers.ydl import YDL, Preview


def text_with_preview(url: str) -> tuple[str, Preview | None] | None:
    # Mirrors and embedded links can resolve to YouTube through the generic route.
    result = YDL.extract(url, require_youtube_video=True)
    if result is None:
        return None
    title, links, preview = result
    text = href(preview[0], "📺") + " " if preview else "🎞 "
    text += href(url, title) + "\n\n— "
    text += ", ".join(href(link, label) for link, label, _, _ in links)
    return text, preview
