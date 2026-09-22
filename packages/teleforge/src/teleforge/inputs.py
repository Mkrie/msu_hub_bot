"""Typed invocation inputs with explicit provenance and bounded resource ownership."""

import asyncio
import inspect
import io
import re
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints

from aiogram.types import (
    Animation,
    Audio,
    CallbackQuery,
    Document,
    Message,
    PhotoSize,
    Sticker,
    TelegramObject,
    Video,
    VideoNote,
    Voice,
)
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from .context import Context
from .rich_input import rich_media, rich_text

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
_MISSING = object()
_RESERVED = {"ctx", "context", "event", "message", "query", "bot", "state", "callback_data"}
type Downloadable = PhotoSize | Document | Video | Animation | VideoNote | Sticker | Audio | Voice


class InputError(ValueError):
    """Brief, safe guidance for a selected invocation's unusable input."""


class _ValidatedPayload(dict[str, Any]):
    """Internal card-codec output; do not execute its field validators again."""


@contextmanager
def _owned_inputs() -> Iterator[ExitStack]:
    resources = ExitStack()
    try:
        yield resources
    except BaseException as primary:
        try:
            resources.close()
        except Exception as cleanup:  # noqa: BLE001 - retain the primary operation/cancellation failure
            # A retained BytesIO view can prevent close. Preserve the operation's
            # error while ExitStack still attempts every other resource cleanup.
            primary.add_note(f"Input cleanup also failed ({type(cleanup).__name__})")
        raise
    else:
        resources.close()


@dataclass(frozen=True, slots=True)
class Argument:
    strict: bool = False
    clamp: tuple[int | float, int | float] | None = None

    def __post_init__(self) -> None:
        if self.clamp is not None and self.clamp[0] > self.clamp[1]:
            raise ValueError("Argument clamp bounds are reversed")


@dataclass(frozen=True, slots=True)
class TextInput:
    reply: bool = True
    document: bool = False
    max_chars: int | None = None
    max_bytes: int = MAX_DOWNLOAD_BYTES

    def __post_init__(self) -> None:
        if self.max_bytes <= 0 or self.max_chars is not None and self.max_chars <= 0:
            raise ValueError("Text input limits must be positive")


@dataclass(frozen=True, slots=True)
class ImageInput:
    reply: bool = True
    avatar: bool = False
    max_bytes: int = MAX_DOWNLOAD_BYTES
    max_pixels: int = 16_000_000
    max_dimension: int = 8192

    def __post_init__(self) -> None:
        if min(self.max_bytes, self.max_pixels, self.max_dimension) <= 0:
            raise ValueError("Image input limits must be positive")


@dataclass(frozen=True, slots=True)
class VideoInput:
    reply: bool = True
    max_bytes: int = MAX_DOWNLOAD_BYTES

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("Video input limits must be positive")


@dataclass(frozen=True, slots=True)
class DocumentInput:
    reply: bool = True
    max_bytes: int = MAX_DOWNLOAD_BYTES

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("Document input limits must be positive")


@dataclass(frozen=True, slots=True)
class MediaInput:
    reply: bool = True
    avatar: bool = False
    max_bytes: int = MAX_DOWNLOAD_BYTES
    kinds: tuple[Literal["image", "video", "document", "audio"], ...] = ("image", "video", "document", "audio")

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("Media input limits must be positive")
        if not self.kinds or any(kind not in {"image", "video", "document", "audio"} for kind in self.kinds):
            raise ValueError("Media kinds must select image, video, document or audio")
        if self.avatar and "image" not in self.kinds:
            raise ValueError("Avatar fallback requires images")


type MediaDeclaration = ImageInput | VideoInput | DocumentInput | MediaInput
type Declaration = Argument | TextInput | MediaDeclaration
_MEDIA = (ImageInput, VideoInput, DocumentInput, MediaInput)


def representation(annotation: Any) -> Any:
    """Unwrap Annotated and one optional type without flattening meaningful unions."""
    if get_origin(annotation) is Annotated:
        return representation(get_args(annotation)[0])
    if get_origin(annotation) in (Union, UnionType):
        choices = [choice for choice in get_args(annotation) if choice is not type(None)]
        if len(choices) == 1:
            return representation(choices[0])
    return annotation


def ordinary(annotation: Any) -> bool:
    annotation = representation(annotation)
    if annotation in (str, int, float, bool):
        return True
    if get_origin(annotation) is Literal:
        return True
    if get_origin(annotation) in (Union, UnionType):
        return all(choice is type(None) or ordinary(choice) for choice in get_args(annotation))
    return isinstance(annotation, type) and issubclass(annotation, Enum)


def annotations_for(handler: Callable[..., Any]) -> dict[str, Any]:
    owner = getattr(handler, "__self__", None)
    namespace: dict[str, Any] = {}
    if owner is not None:
        for cls in reversed(type(owner).__mro__):
            namespace.update(vars(cls))
            namespace[cls.__name__] = cls
    return get_type_hints(handler, localns=namespace or None, include_extras=True)


def _adapter(annotation: Any) -> TypeAdapter[Any]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return TypeAdapter(annotation)
    return TypeAdapter(annotation, config=ConfigDict(arbitrary_types_allowed=True))


def _validate(annotation: Any, value: Any, *, strict: bool = False) -> Any:
    return _adapter(annotation).validate_python(value, strict=strict)


def _targets(event: TelegramObject, reply: bool, selected: Message | None = None) -> tuple[Message, ...]:
    # A callback's card is never automatically interpreted as input content.
    origin = selected or (event if isinstance(event, Message) else None)
    if origin is None:
        return ()
    if reply and isinstance(origin.reply_to_message, Message):
        return origin, origin.reply_to_message
    return (origin,)


class _LimitedBuffer(io.BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit

    def write(self, data: Any) -> int:
        if self.tell() + len(data) > self.limit:
            raise InputError("The attachment is too large. Please send a smaller file.")
        return super().write(data)


async def _download(
    media: Downloadable,
    ctx: Context,
    limit: int,
    resources: ExitStack,
    downloads: dict[str, io.BytesIO],
) -> io.BytesIO:
    if (media.file_size or 0) > limit:
        raise InputError("The attachment is too large. Please send a smaller file.")
    if media.file_id in downloads:
        cached = downloads[media.file_id]
        if cached.getbuffer().nbytes > limit:
            raise InputError("The attachment is too large. Please send a smaller file.")
        cached.seek(0)
        return cached
    stream = _LimitedBuffer(limit)
    resources.callback(stream.close)
    await ctx.bot.download(media.file_id, destination=stream, timeout=30)
    stream.seek(0)
    downloads[media.file_id] = stream
    return stream


def _check_text(text: str, declaration: TextInput) -> str:
    if declaration.max_chars is not None and len(text) > declaration.max_chars:
        raise InputError(f"Text is too long; use at most {declaration.max_chars} characters.")
    if len(text.encode("utf-8")) > declaration.max_bytes:
        raise InputError("Text is too large. Please send less text.")
    return text


async def _text(
    event: TelegramObject,
    ctx: Context,
    declaration: TextInput,
    tail: str | None,
    resources: ExitStack,
    downloads: dict[str, io.BytesIO],
    selected: Message | None,
) -> tuple[Message | None, str]:
    targets = _targets(event, declaration.reply, selected)
    if tail and isinstance(event, Message):
        return event, _check_text(tail, declaration)
    for index, target in enumerate(targets):
        # For a selected command, None means a non-command event; an empty tail
        # must not fall back to the literal command token itself.
        text = target.text or target.caption or rich_text(target)
        if index == 0 and target is event and tail is not None:
            text = ""
        if text:
            return target, _check_text(text, declaration)
        if declaration.document:
            candidates = [target.document, *rich_media(target)]
            for item in candidates:
                if isinstance(item, Document) and (item.mime_type or "").startswith("text/"):
                    stream = await _download(item, ctx, declaration.max_bytes, resources, downloads)
                    try:
                        text = stream.getvalue().decode("utf-8")
                    except UnicodeDecodeError:
                        raise InputError("Please send a UTF-8 text file.") from None
                    return target, _check_text(text, declaration)
    return None, ""


def _pick_media(message: Message, kinds: tuple[str, ...]) -> Downloadable | None:
    candidates: list[Downloadable] = []
    if message.video or message.animation or message.video_note:
        candidates.append(message.video or message.animation or message.video_note)  # type: ignore[arg-type]
    if message.photo:
        candidates.append(message.photo[-1])
    if message.sticker:
        candidates.append(message.sticker)
    if message.document:
        candidates.append(message.document)
    if message.audio or message.voice:
        candidates.append(message.audio or message.voice)  # type: ignore[arg-type]
    candidates.extend(rich_media(message))
    for media in candidates:
        if isinstance(media, (Video, Animation, VideoNote)) and "video" in kinds:
            return media
        if isinstance(media, PhotoSize) and "image" in kinds:
            return media
        if isinstance(media, Sticker) and (
            media.is_video and "video" in kinds or not (media.is_video or media.is_animated) and "image" in kinds
        ):
            return media
        if isinstance(media, Document):
            mime = media.mime_type or ""
            if (
                "document" in kinds
                or "image" in kinds
                and mime.startswith("image/")
                or "video" in kinds
                and mime.startswith("video/")
            ):
                return media
        if isinstance(media, (Audio, Voice)) and "audio" in kinds:
            return media
    return None


async def _select_media(
    event: TelegramObject, ctx: Context, declaration: MediaDeclaration, selected: Message | None
) -> tuple[Message | None, Downloadable | None]:
    kinds = (
        declaration.kinds
        if isinstance(declaration, MediaInput)
        else ("image",)
        if isinstance(declaration, ImageInput)
        else ("video",)
        if isinstance(declaration, VideoInput)
        else ("document",)
    )
    targets = _targets(event, declaration.reply, selected)
    for target in targets:
        if media := _pick_media(target, kinds):
            return target, media
    if isinstance(declaration, (ImageInput, MediaInput)) and declaration.avatar:
        for target in reversed(targets):
            if target.from_user is not None:
                photos = await ctx.bot.get_user_profile_photos(target.from_user.id, limit=1)
                if photos.photos and photos.photos[0]:
                    return target, photos.photos[0][-1]
    return None, None


async def _decode(payload: bytes, declaration: MediaDeclaration, ctx: Context, resources: ExitStack) -> Any:
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        raise InputError("Image decoding requires the teleforge[media] extra.") from None
    max_pixels = declaration.max_pixels if isinstance(declaration, ImageInput) else 16_000_000
    max_dimension = declaration.max_dimension if isinstance(declaration, ImageInput) else 8192

    def load() -> Image.Image:
        with io.BytesIO(payload) as source:
            image = Image.open(source)
            try:
                width, height = image.size
                if width * height > max_pixels or max(width, height) > max_dimension:
                    raise InputError("The image dimensions are too large. Please resize it.")
                image.load()
            except BaseException:
                image.close()
                raise
            return image

    async def run() -> Image.Image:
        task = asyncio.create_task(asyncio.to_thread(load))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A worker owns its immutable bytes; join it before returning a live
            # image to nowhere. Repeated cancellation must not skip cleanup.
            while not task.cancelled():
                try:
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:  # noqa: BLE001 - preserve cancellation after joining the owned worker
                    break
                else:
                    result.close()
                    break
            raise

    try:
        semaphore = ctx.data.get("_teleforge_media_slots")
        if isinstance(semaphore, asyncio.Semaphore):
            async with semaphore:
                image = await run()
        else:
            image = await run()
    except UnidentifiedImageError, OSError, Image.DecompressionBombError:
        raise InputError("Could not decode the image. Please send another image.") from None
    resources.callback(image.close)
    return image


async def _media_value(
    media: Downloadable,
    annotation: Any,
    declaration: MediaDeclaration,
    ctx: Context,
    resources: ExitStack,
    downloads: dict[str, io.BytesIO],
) -> Any:
    if (media.file_size or 0) > declaration.max_bytes:
        raise InputError("The attachment is too large. Please send a smaller file.")
    requested = representation(annotation)
    is_image = getattr(requested, "__module__", "") == "PIL.Image" and getattr(requested, "__name__", "") == "Image"
    if requested not in (bytes, io.BytesIO, Path) and not is_image:
        return _validate(annotation, media)
    stream = await _download(media, ctx, declaration.max_bytes, resources, downloads)
    if requested is bytes:
        return stream.getvalue()
    if requested is io.BytesIO:
        return stream
    if requested is Path:
        directory = Path(resources.enter_context(TemporaryDirectory(prefix="teleforge-input-")))
        suffix = Path(getattr(media, "file_name", None) or "input").suffix
        suffix = suffix if len(suffix) <= 16 and suffix.removeprefix(".").isalnum() else ""
        path = directory / ("input" + suffix)
        path.write_bytes(stream.getvalue())
        return path
    return await _decode(stream.getvalue(), declaration, ctx, resources)


@asynccontextmanager
async def prepare_arguments(
    handler: Callable[..., Any],
    event: TelegramObject,
    ctx: Context,
    data: Mapping[str, Any],
    declarations: Mapping[str, Declaration],
    *,
    tail: str | None = None,
    payload: BaseModel | Mapping[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Prepare arguments once; keep streams/images/paths alive through delivery.

    Callback values come from a validated native model or the card codec. They
    are never interpreted as command tokens or rescued with command defaults.
    An explicitly selected callback input can be supplied in input_sources.
    """
    signature = inspect.signature(handler)
    annotations = annotations_for(handler)
    payload_values = (
        {name: getattr(payload, name) for name in type(payload).model_fields}
        if isinstance(payload, BaseModel)
        else dict(payload or {})
    )
    if payload_values.keys() & _RESERVED:
        raise InputError("Callback data conflicts with invocation context.")
    tokens = list(re.finditer(r"\S+", tail or ""))
    position = consumed = 0
    contiguous = True
    values: dict[str, Any] = {}
    text_parameters: list[tuple[str, Any, Any, TextInput]] = []
    media_parameters: list[tuple[str, Any, Any, MediaDeclaration]] = []
    source_text: Message | None = None
    source_media: Message | None = None
    with _owned_inputs() as resources:
        downloads: dict[str, io.BytesIO] = {}
        for name, parameter in signature.parameters.items():
            if name in {"self", "cls"}:
                continue
            annotation = annotations.get(name, parameter.annotation)
            declaration = declarations.get(name)
            default = parameter.default
            if isinstance(declaration, TextInput):
                text_parameters.append((name, annotation, default, declaration))
                continue
            if isinstance(declaration, _MEDIA):
                media_parameters.append((name, annotation, default, declaration))
                continue
            if name in {"ctx", "context"}:
                if isinstance(annotation, type) and not isinstance(ctx, annotation):
                    raise InputError("This handler requires a different Telegram context.")
                values[name] = ctx
            elif name == "event":
                values[name] = event
            elif name == "bot":
                values[name] = ctx.bot
            elif (
                name == "message" and isinstance(event, Message) or name == "query" and isinstance(event, CallbackQuery)
            ):
                values[name] = event
            elif name in payload_values:
                if name in data:
                    raise InputError(f"Callback field '{name}' conflicts with a supplied dependency.")
                if isinstance(payload, BaseModel | _ValidatedPayload):
                    # Native CallbackData and the managed codec already validated
                    # these fields. Re-running a transforming validator changes IDs.
                    values[name] = payload_values[name]
                    continue
                try:
                    # Pydantic CallbackData has already run its validators. The
                    # function's compatible annotation cannot change its values.
                    values[name] = _validate(annotation, payload_values[name], strict=True)
                except ValidationError:
                    raise InputError("This button is no longer valid. Please open the feature again.") from None
            elif name in data and declaration is None:
                values[name] = data[name]
            elif payload is not None:
                if default is inspect.Parameter.empty:
                    raise InputError(f"Callback data is missing '{name}'.")
                values[name] = default
            elif isinstance(declaration, Argument) or ordinary(annotation):
                rule = declaration if isinstance(declaration, Argument) else Argument()
                raw = tokens[position].group() if position < len(tokens) else _MISSING
                position += 1
                parsed: Any = _MISSING
                if raw is not _MISSING:
                    try:
                        parsed = _validate(annotation, raw)
                    except ValidationError:
                        if rule.strict:
                            raise InputError(f"Invalid value for '{name}'.") from None
                if parsed is _MISSING:
                    contiguous = False
                    if default is inspect.Parameter.empty:
                        raise InputError(f"Provide a valid value for '{name}'.")
                    parsed = _validate(annotation, default)
                elif contiguous:
                    consumed = tokens[position - 1].end()
                if rule.clamp is not None and isinstance(parsed, (int, float)):
                    parsed = max(rule.clamp[0], min(rule.clamp[1], parsed))
                    parsed = _validate(annotation, parsed)
                values[name] = parsed
            elif default is not inspect.Parameter.empty:
                values[name] = default
            else:
                raise TypeError(f"Missing injected dependency '{name}' for {handler.__qualname__}")

        remaining = (tail[consumed:].lstrip() if consumed else tail) if tail is not None else None
        for name, annotation, default, declaration_text in text_parameters:
            source, value = await _text(
                event, ctx, declaration_text, remaining, resources, downloads, ctx.input_sources.get(name)
            )
            if not value:
                if default is inspect.Parameter.empty:
                    raise InputError(f"Provide text for '{name}'.")
                value = default
            values[name] = _validate(annotation, value)
            if source is not None:
                ctx.input_sources[name] = source
                source_text = source
        for name, annotation, default, declaration_media in media_parameters:
            source, media = await _select_media(event, ctx, declaration_media, ctx.input_sources.get(name))
            if media is None:
                if default is inspect.Parameter.empty:
                    raise InputError(f"Attach or reply to media for '{name}'.")
                value_media = _validate(annotation, default)
            else:
                try:
                    value_media = await _media_value(media, annotation, declaration_media, ctx, resources, downloads)
                except ValidationError:
                    raise InputError(f"The attachment has the wrong media type for '{name}'.") from None
            values[name] = value_media
            if source is not None:
                ctx.input_sources[name] = source
                source_media = source
        if isinstance(event, Message) and (selected_source := source_media or source_text) is not None:
            ctx.response_target = selected_source
        yield values
