"""Generic yt-dlp presentation with ordinary video emojis."""

from msu_hub_bot.providers.vk.utils import href
from msu_hub_bot.providers.ydl import YDL, Preview


def text_with_preview(url: str) -> tuple[str, Preview | None] | None:
    result = YDL.extract(url)
    if result is None:
        return None
    title, links, preview = result
    text = href(preview[0], "📺") + " " if preview else "🎞 "
    text += href(url, title) + "\n\n— "
    text += ", ".join(href(link, label) for link, label, _, _ in links)
    return text, preview
