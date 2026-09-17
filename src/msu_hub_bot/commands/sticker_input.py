"""Select sticker artwork and parse its small, explicit metadata syntax."""

from dataclasses import dataclass

import emoji
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, Sticker

from msu_hub_bot.media.sticker_media import StickerMediaError


@dataclass(frozen=True)
class StickerMetadata:
    emojis: tuple[str, ...] = ("✨",)
    keywords: tuple[str, ...] | None = None
    emojis_explicit: bool = False


def parse_metadata(text: str, fallback_text: str = "") -> StickerMetadata:
    """A vertical bar separates associated emoji from comma-separated keywords."""
    left, separator, right = text.partition("|")
    explicit = tuple(dict.fromkeys(item["emoji"] for item in emoji.emoji_list(left)))
    fallback = fallback_text if not text else ""
    emojis = explicit or tuple(dict.fromkeys(item["emoji"] for item in emoji.emoji_list(fallback))) or ("✨",)
    if len(emojis) > 20:
        raise StickerMediaError("Для стикера можно указать до 20 разных эмодзи.")
    if not separator:
        return StickerMetadata(emojis=emojis, emojis_explicit=bool(explicit))
    if emoji.replace_emoji(left).strip():
        raise StickerMediaError("До «|» укажите эмодзи, после — метки через запятую. Например: /sc 😀🔥 | кот, мем")
    keywords = []
    seen = set()
    for item in right.split(","):
        keyword = " ".join(item.split())
        if any(ord(character) < 32 or ord(character) == 127 for character in keyword):
            raise StickerMediaError("В поисковых метках не должно быть служебных символов.")
        if keyword and keyword.casefold() not in seen:
            seen.add(keyword.casefold())
            keywords.append(keyword)
    if len(keywords) > 20 or sum(map(len, keywords)) > 64:
        raise StickerMediaError("Можно указать до 20 поисковых меток, суммарно не больше 64 символов.")
    return StickerMetadata(emojis, tuple(keywords), bool(explicit))


def reusable_sticker(sticker: Sticker) -> bool:
    """Telegram-registered regular artwork needs no download or lossy conversion."""
    if sticker.type != "regular" or not sticker.file_id or not sticker.file_unique_id:
        return False
    if not 0 < min(sticker.width, sticker.height) <= max(sticker.width, sticker.height) == 512:
        return False
    if sticker.is_animated and sticker.width != sticker.height:
        return False
    limit = 64 * 1024 if sticker.is_animated else 256 * 1024 if sticker.is_video else 512 * 1024
    return sticker.file_size is None or 0 < sticker.file_size <= limit


async def custom_emoji_source(message: Message, bot: Bot) -> Sticker | None:
    for target in (message, message.reply_to_message):
        if target is None:
            continue
        entities = [*(target.entities or ()), *(target.caption_entities or ())]
        ids = list(dict.fromkeys(entity.custom_emoji_id for entity in entities if entity.type == "custom_emoji" and entity.custom_emoji_id))
        if not ids:
            continue
        if len(ids) > 1:
            raise StickerMediaError("Здесь несколько разных кастомных эмодзи. Пришлите нужный отдельно и ответьте /sc.")
        try:
            resolved = await bot.get_custom_emoji_stickers(custom_emoji_ids=ids)
        except TelegramBadRequest as exc:
            raise StickerMediaError("Не удалось получить этот кастомный эмодзи. Пришлите его ещё раз или выберите другой.") from exc
        for sticker in resolved:
            if sticker.type == "custom_emoji" and sticker.custom_emoji_id == ids[0]:
                return sticker
        raise StickerMediaError("Этот кастомный эмодзи недоступен. Пришлите другой или его картинку, GIF или видео.")
    return None
