"""One attributed rich post for successfully extracted social media."""

import math
import re
from urllib.parse import quote

from aiogram.types import (
    BufferedInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    InputRichBlockCollage,
    InputRichBlockDetails,
    InputRichBlockPhoto,
    InputRichBlockSlideshow,
    InputRichBlockUnion,
    InputRichBlockVideo,
    InputRichMessage,
    Message,
)

from msu_hub_bot.providers.fxembed import safe_public_url
from msu_hub_bot.providers.link_models import LinkPost, LinkSite
from msu_hub_bot.telegram.links.rich import _paragraphs, _Span, publish_messages, split_messages
from msu_hub_bot.telegram.wrapper import BotWrapper

_BRANDING: dict[LinkSite, tuple[str, str]] = {
    "youtube": ("📺", "5278611117130653414"),
    "instagram": ("📷", "5281024850096301559"),
    "tiktok": ("🎥", "5280662183057825163"),
}
_LINK = re.compile(r"https?://[^\s<>]+|(?<![\w@])@[\w.]{1,30}")


def _body(text: str, site: LinkSite) -> list[_Span]:
    spans: list[_Span] = []
    start = 0
    for match in _LINK.finditer(text):
        spans.append(_Span(text[start : match.start()]))
        label = match[0]
        if label.startswith("@"):
            label = label.rstrip(".")
            handle = quote(label[1:], safe="")
            url = f"https://www.instagram.com/{handle}/" if site == "instagram" else f"https://www.{site}.com/@{handle}"
        else:
            label = label.rstrip(".,;:!?")
            while label.endswith(")") and label.count(")") > label.count("("):
                label = label[:-1]
            url = label
        spans.append(_Span(label, url if safe_public_url(url) else None))
        spans.append(_Span(match[0][len(label) :]))
        start = match.end()
    spans.append(_Span(text[start:]))
    return [span for span in spans if span.text]


def render_native_post(post: LinkPost) -> list[InputRichMessage]:
    """Keep captions and source order; do not manufacture unavailable content."""
    if not post.assets or len(post.assets) > 50 or not safe_public_url(post.url):
        return []
    if post.site == "youtube" and not any(asset.kind == "video" and asset.data for asset in post.assets):
        return []
    if sum(len(asset.data) for asset in post.assets) > 100 * 1024 * 1024:
        return []
    emoji, emoji_id = _BRANDING[post.site]
    header = [_Span(emoji, custom_emoji_id=emoji_id), _Span(" "), _Span(post.author or post.site, style="BOLD")]
    if post.username:
        header.extend(
            [
                _Span(" · "),
                _Span("@" + post.username.lstrip("@"), post.author_url if post.author_url and safe_public_url(post.author_url) else None),
            ]
        )
    header.extend([_Span(" · "), _Span("↗", post.url)])
    if post.title:
        header.extend([_Span("\n\n"), _Span(post.title, style="BOLD")])
    if post.text and post.site != "youtube":
        header.extend([_Span("\n\n"), *_body(post.text, post.site)])
    blocks = _paragraphs(header)
    gallery: list[InputRichBlockUnion] = []
    for index, asset in enumerate(post.assets):
        limit = (9 if asset.kind == "photo" else 49) * 1024 * 1024
        if not asset.data or len(asset.data) > limit or min(asset.width, asset.height) <= 0 or max(asset.width, asset.height) > 10000:
            return []
        file = BufferedInputFile(asset.data, filename=f"{post.site}-{index}.{'jpg' if asset.kind == 'photo' else 'mp4'}")
        if asset.kind == "photo":
            gallery.append(InputRichBlockPhoto(photo=InputMediaPhoto(media=file)))
        else:
            duration = asset.duration
            if duration is not None and (not math.isfinite(duration) or duration < 0):
                return []
            gallery.append(
                InputRichBlockVideo(
                    video=InputMediaVideo(
                        media=file,
                        width=asset.width,
                        height=asset.height,
                        duration=round(duration) if duration is not None else None,
                        supports_streaming=True,
                    )
                )
            )
    # Bound groups so a large album remains valid even when it must be split.
    for offset in range(0, len(gallery), 10):
        group = gallery[offset : offset + 10]
        if len(group) == 1:
            blocks.extend(group)
        elif post.site == "tiktok" and all(isinstance(block, InputRichBlockPhoto) for block in group):
            blocks.append(InputRichBlockSlideshow(blocks=group))
        else:
            blocks.append(InputRichBlockCollage(blocks=group))
    if post.text and post.site == "youtube":
        blocks.append(InputRichBlockDetails(summary="Описание", blocks=_paragraphs(_body(post.text, post.site))))
    return split_messages(blocks)


async def publish_native_post(
    post: LinkPost,
    bot: BotWrapper,
    chat_id: int,
    reply_to: int,
    *,
    message_thread_id: int | None = None,
) -> Message | None:
    messages = render_native_post(post)
    if not messages:
        return None
    return await publish_messages(messages, bot, chat_id, reply_to, message_thread_id=message_thread_id, text_fallback=False)
