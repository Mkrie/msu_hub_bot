"""Bounded rich text assembly and ordered Telegram delivery."""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InputRichBlockBlockQuotation,
    InputRichBlockCollage,
    InputRichBlockDetails,
    InputRichBlockFooter,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockSectionHeading,
    InputRichBlockSlideshow,
    InputRichBlockUnion,
    InputRichBlockVideo,
    InputRichMessage,
    LinkPreviewOptions,
    Message,
    ReplyParameters,
    RichTextBold,
    RichTextCode,
    RichTextCustomEmoji,
    RichTextItalic,
    RichTextStrikethrough,
    RichTextUnderline,
    RichTextUnion,
    RichTextUrl,
)

from msu_hub_bot.telegram.wrapper import BotWrapper

_MAX_TEXT = 32768
_MAX_BLOCKS = 500
_MAX_MEDIA = 50


@dataclass(frozen=True, slots=True)
class _Span:
    text: str
    url: str | None = None
    style: str | None = None
    custom_emoji_id: str | None = None

    def rich(self) -> RichTextUnion:
        text: RichTextUnion = (
            RichTextCustomEmoji(custom_emoji_id=self.custom_emoji_id, alternative_text=self.text) if self.custom_emoji_id else self.text
        )
        if self.url:
            text = RichTextUrl(text=text, url=self.url)
        for style in (self.style or "").split(","):
            if style == "BOLD":
                text = RichTextBold(text=text)
            elif style == "ITALIC":
                text = RichTextItalic(text=text)
            elif style == "CODE":
                text = RichTextCode(text=text)
            elif style == "STRIKETHROUGH":
                text = RichTextStrikethrough(text=text)
            elif style == "UNDERLINE":
                text = RichTextUnderline(text=text)
        return text


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _pieces(text: str, limit: int) -> Iterator[str]:
    """Split on code points; never cut a surrogate pair or discard whitespace."""
    start = size = 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            yield text[start:index]
            start, size = index, 0
        size += width
    if start < len(text):
        yield text[start:]


def _paragraphs(spans: Sequence[_Span], *, limit: int = _MAX_TEXT) -> list[InputRichBlockUnion]:
    parts: list[InputRichBlockUnion] = []
    current: list[RichTextUnion] = []
    size = 0
    for span in spans:
        remaining = span.text
        while remaining:
            available = limit - size
            if available < (2 if ord(remaining[0]) > 0xFFFF else 1):
                parts.append(InputRichBlockParagraph(text=current))
                current, size = [], 0
                continue
            piece = next(_pieces(remaining, available))
            current.append(replace(span, text=piece).rich())
            size += _units(piece)
            remaining = remaining[len(piece) :]
    if current:
        parts.append(InputRichBlockParagraph(text=current))
    return parts


def _rich_spans(text: RichTextUnion, *, url: str | None = None, style: str | None = None) -> Iterator[_Span]:
    if isinstance(text, str):
        yield _Span(text, url, style)
    elif isinstance(text, list):
        for child in text:
            yield from _rich_spans(child, url=url, style=style)
    elif isinstance(text, RichTextUrl):
        yield from _rich_spans(text.text, url=text.url, style=style)
    elif isinstance(text, RichTextCustomEmoji):
        yield _Span(text.alternative_text, url, style, text.custom_emoji_id)
    elif isinstance(text, RichTextBold | RichTextItalic | RichTextCode | RichTextStrikethrough | RichTextUnderline):
        combined = ",".join(sorted({*(style or "").split(","), text.type.upper()} - {""}))
        yield from _rich_spans(text.text, url=url, style=combined)
    else:
        raise TypeError("Unsupported rich text")


def _cost(blocks: Sequence[InputRichBlockUnion]) -> tuple[int, int, int]:
    text = media = 0
    count = len(blocks)
    for block in blocks:
        if isinstance(block, InputRichBlockBlockQuotation | InputRichBlockCollage | InputRichBlockSlideshow | InputRichBlockDetails):
            nested = _cost(block.blocks)
            text, count, media = text + nested[0], count + nested[1], media + nested[2]
            if isinstance(block, InputRichBlockDetails):
                text += sum(_units(span.text) for span in _rich_spans(block.summary))
        elif isinstance(block, InputRichBlockPhoto | InputRichBlockVideo):
            media += 1
        elif isinstance(block, InputRichBlockParagraph | InputRichBlockFooter | InputRichBlockSectionHeading):
            text += sum(_units(span.text) for span in _rich_spans(block.text))
    return text, count, media


def _fits(blocks: Sequence[InputRichBlockUnion]) -> bool:
    return all(value <= maximum for value, maximum in zip(_cost(blocks), (_MAX_TEXT, _MAX_BLOCKS, _MAX_MEDIA), strict=True))


def _flat_blocks(blocks: Sequence[InputRichBlockUnion]) -> Iterator[InputRichBlockUnion]:
    for block in blocks:
        if isinstance(block, InputRichBlockBlockQuotation):
            yield InputRichBlockParagraph(text=RichTextBold(text="Цитата"))
            yield from _flat_blocks(block.blocks)
        elif isinstance(block, InputRichBlockCollage | InputRichBlockSlideshow):
            yield from _flat_blocks(block.blocks)
        elif isinstance(block, InputRichBlockDetails):
            yield from _paragraphs(list(_rich_spans(block.summary)))
            yield from _flat_blocks(block.blocks)
        elif isinstance(block, InputRichBlockParagraph | InputRichBlockFooter | InputRichBlockSectionHeading):
            yield from _paragraphs(list(_rich_spans(block.text)))
        else:
            yield block


def _plain(blocks: Sequence[InputRichBlockUnion]) -> str:
    text: list[str] = []
    for block in _flat_blocks(blocks):
        if isinstance(block, InputRichBlockParagraph | InputRichBlockFooter | InputRichBlockSectionHeading):
            text.append(
                "".join(span.text + (f" ({span.url})" if span.url and span.url != span.text else "") for span in _rich_spans(block.text))
            )
        elif isinstance(block, InputRichBlockPhoto | InputRichBlockVideo):
            text.append("Медиа — в оригинале.")
    return "\n\n".join(text)


def _can_fallback(error: TelegramBadRequest) -> bool:
    reason = error.message.lower()
    return any(word in reason for word in ("rich", "media", "photo", "video", "file", "entity", "text is too long"))


def _without_custom_emoji(blocks: Sequence[InputRichBlockUnion]) -> list[InputRichBlockUnion]:
    result: list[InputRichBlockUnion] = []
    for block in blocks:
        if isinstance(block, InputRichBlockBlockQuotation | InputRichBlockCollage | InputRichBlockSlideshow | InputRichBlockDetails):
            block = block.model_copy(update={"blocks": _without_custom_emoji(block.blocks)})
        elif isinstance(block, InputRichBlockParagraph | InputRichBlockFooter | InputRichBlockSectionHeading):
            text = [replace(span, custom_emoji_id=None).rich() for span in _rich_spans(block.text)]
            block = block.model_copy(update={"text": text})
        result.append(block)
    return result


def split_messages(blocks: Sequence[InputRichBlockUnion]) -> list[InputRichMessage]:
    """Preserve layout while it fits; flatten and split actual overflows."""
    if _fits(blocks):
        return [InputRichMessage(blocks=list(blocks), skip_entity_detection=True)]
    result: list[InputRichMessage] = []
    part: list[InputRichBlockUnion] = []
    for block in _flat_blocks(blocks):
        if part and not _fits([*part, block]):
            result.append(InputRichMessage(blocks=part, skip_entity_detection=True))
            part = []
        part.append(block)
    if part:
        result.append(InputRichMessage(blocks=part, skip_entity_detection=True))
    return result


async def publish_messages(
    messages: Sequence[InputRichMessage],
    bot: BotWrapper,
    chat_id: int,
    reply_to: int | None = None,
    *,
    message_thread_id: int | None = None,
    text_fallback: bool = True,
) -> Message | None:
    """Serialize a post's chunks; retry only a definite emoji rejection."""
    result = None
    async with bot.serial_send(chat_id):
        for message in messages:
            reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to is not None else None
            for custom_emoji in (True, False):
                try:
                    result = await bot.send_rich_message(
                        chat_id=chat_id, rich_message=message, reply_parameters=reply, message_thread_id=message_thread_id
                    )
                except TelegramBadRequest as error:
                    if custom_emoji and any(word in error.message.lower() for word in ("custom emoji", "custom_emoji")):
                        message = message.model_copy(update={"blocks": _without_custom_emoji(message.blocks or [])})
                        continue
                    if not text_fallback or not _can_fallback(error):
                        raise
                    for text in _pieces(_plain(message.blocks or []), 4096):
                        if not text.strip():
                            continue
                        result = await bot.send_message(
                            chat_id=chat_id,
                            text=text,
                            parse_mode=None,
                            link_preview_options=LinkPreviewOptions(is_disabled=True),
                            reply_parameters=reply,
                            message_thread_id=message_thread_id,
                        )
                        reply = ReplyParameters(message_id=result.message_id, allow_sending_without_reply=True)
                break
            if result:
                reply_to = result.message_id
    return result
