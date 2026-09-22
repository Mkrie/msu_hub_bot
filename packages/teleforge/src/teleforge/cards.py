"""Managed cards bind typed feature methods without owning application state."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar, cast, get_type_hints

from aiogram.filters import Filter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity
from aiogram.utils.formatting import Text
from pydantic import TypeAdapter, ValidationError

from .context import CallbackContext, Context
from .declarations import Declaration, attach_declaration, declarations_of
from .delivery import MediaSource
from .feature import Feature

_Handler = TypeVar("_Handler", bound=Callable[..., Any])
_PREFIX = "tf:"


class CardError(ValueError):
    """Invalid card declaration, button arguments, or Telegram target."""


class CardRefreshError(RuntimeError):
    """An action completed, but refreshing its presentation failed. Do not replay it."""

    applied = True

    def __init__(self, renderer: str) -> None:
        super().__init__(f"Action completed but card {renderer!r} could not be refreshed")


@dataclass(frozen=True, init=False)
class Button:
    """A label and a bound, declared feature action with small typed arguments."""

    text: str
    action: Callable[..., Any]
    arguments: Mapping[str, object]

    def __init__(self, text: str, action: Callable[..., Any], **arguments: object) -> None:
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "arguments", dict(arguments))


@dataclass(frozen=True)
class Card:
    """One editable message and a freshly rendered keyboard; rendering has no effects."""

    text: str | Text | None = None
    buttons: Sequence[Sequence[Button | InlineKeyboardButton]] = ()
    photo: MediaSource | None = None
    video: MediaSource | None = None
    animation: MediaSource | None = None
    audio: MediaSource | None = None
    document: MediaSource | None = None
    entities: Sequence[MessageEntity] | None = None

    def _content(self, keyboard: InlineKeyboardMarkup) -> dict[str, Any]:
        result: dict[str, Any] = {"text": self.text, "reply_markup": keyboard}
        for name in ("photo", "video", "animation", "audio", "document", "entities"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result


def _bound(method: Callable[..., Any]) -> tuple[Feature, str]:
    owner = getattr(method, "__self__", None)
    if not isinstance(owner, Feature):
        raise CardError("Card renderers and actions must be bound feature methods")
    return owner, method.__name__


def _declaration(method: Callable[..., Any], marker: str) -> Declaration:
    _, name = _bound(method)
    found = [item for item in declarations_of(method) if marker in item.metadata]
    if len(found) != 1:
        raise CardError(f"Method {name!r} must have exactly one {marker} declaration")
    return found[0]


@dataclass(frozen=True)
class _Parameter:
    name: str
    adapter: TypeAdapter[Any]
    annotation: object
    default: object = inspect.Parameter.empty


def _parameters(method: Callable[..., Any]) -> list[_Parameter]:
    feature, _ = _bound(method)
    namespace = {name: value for cls in reversed(type(feature).__mro__) for name, value in vars(cls).items()}
    hints = get_type_hints(method, localns=namespace, include_extras=True)
    parameters = []
    for name, parameter in inspect.signature(method).parameters.items():
        if name == "ctx":
            continue
        if parameter.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            raise CardError("Card methods accept named arguments, without *args or **kwargs")
        if name not in hints:
            raise CardError(f"Card argument {name!r} needs a type annotation")
        parameters.append(_Parameter(name, TypeAdapter(hints[name]), hints[name], parameter.default))
    return parameters


def _schema(action_method: Callable[..., Any]) -> tuple[str, list[_Parameter], Callable[..., Any]]:
    feature, name = _bound(action_method)
    declaration = _declaration(action_method, "card_action")
    renderer_name = cast(str, declaration.metadata["card_action"])
    renderer = cast(Callable[..., Any], getattr(feature, renderer_name))
    _declaration(renderer, "card")
    parameters = _parameters(action_method)
    by_name = {parameter.name: parameter for parameter in parameters}
    for parameter in _parameters(renderer):
        previous = by_name.get(parameter.name)
        if previous is not None and previous.annotation != parameter.annotation:
            raise CardError(f"Card and action disagree on argument {parameter.name!r}")
        if previous is None:
            parameters.append(parameter)
            by_name[parameter.name] = parameter
    identity = repr((feature.key, name, renderer_name, [(p.name, p.annotation) for p in parameters]))
    digest = hashlib.blake2s(identity.encode(), digest_size=6).digest()
    return base64.urlsafe_b64encode(digest).decode(), parameters, renderer


def _validate(
    parameters: Sequence[_Parameter], arguments: Mapping[str, object], *, json_values: bool = False
) -> dict[str, Any]:
    unknown = arguments.keys() - {parameter.name for parameter in parameters}
    if unknown:
        raise CardError(f"Unknown card arguments: {', '.join(sorted(unknown))}")
    result = {}
    for parameter in parameters:
        value = arguments.get(parameter.name, parameter.default)
        if value is inspect.Parameter.empty:
            raise CardError(f"Missing card argument {parameter.name!r}")
        try:
            result[parameter.name] = (
                parameter.adapter.validate_json(json.dumps(value, allow_nan=False), strict=True)
                if json_values
                else parameter.adapter.validate_python(value, strict=True)
            )
        except (ValueError, TypeError, ValidationError) as exc:
            raise CardError(f"Invalid card argument {parameter.name!r}") from exc
    return result


def _pack(method: Callable[..., Any], arguments: Mapping[str, object]) -> str:
    identity, parameters, _ = _schema(method)
    values = _validate(parameters, arguments)
    raw = [parameter.adapter.dump_python(values[parameter.name], mode="json") for parameter in parameters]
    encoded = _PREFIX + identity + ":" + json.dumps(raw, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 64:
        raise CardError("Card button exceeds Telegram's 64-byte limit; pass a short application record ID")
    return encoded


class _ActionFilter(Filter):
    def __init__(self, method: Callable[..., Any]) -> None:
        self.identity, self.parameters, _ = _schema(method)

    async def __call__(self, query: CallbackQuery) -> bool | dict[str, Any]:
        if not query.data or len(query.data.encode()) > 64 or not query.data.startswith(_PREFIX + self.identity + ":"):
            return False
        try:
            values = json.loads(query.data[len(_PREFIX) + len(self.identity) + 1 :])
            if not isinstance(values, list) or len(values) != len(self.parameters):
                return False
            arguments = _validate(
                self.parameters, dict(zip((p.name for p in self.parameters), values, strict=True)), json_values=True
            )
        except ValueError, TypeError:
            return False
        return {"_teleforge_payload": arguments, "_teleforge_card_arguments": arguments}


@dataclass
class _Lock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class _CardLocks:
    def __init__(self) -> None:
        self._locks: dict[tuple[int, int, int], _Lock] = {}

    @asynccontextmanager
    async def hold(self, key: tuple[int, int, int], *, coalesce: bool = False) -> AsyncIterator[bool]:
        entry = self._locks.setdefault(key, _Lock())
        entry.users += 1
        try:
            if coalesce and entry.users > 1:
                yield False
                return
            async with entry.lock:
                yield True
        finally:
            entry.users -= 1
            if entry.users == 0:
                del self._locks[key]


def _keyboard(view: Card, renderer: Callable[..., Any], arguments: Mapping[str, object]) -> InlineKeyboardMarkup:
    owner, renderer_name = _bound(renderer)
    render_arguments = {p.name: arguments[p.name] for p in _parameters(renderer) if p.name in arguments}
    rows = []
    for row in view.buttons:
        buttons = []
        for button in row:
            if isinstance(button, InlineKeyboardButton):
                buttons.append(button)
                continue
            action_owner, _ = _bound(button.action)
            declaration = _declaration(button.action, "card_action")
            if action_owner is not owner or declaration.metadata["card_action"] != renderer_name:
                raise CardError("A managed button must target an action of the card's feature and renderer")
            buttons.append(
                InlineKeyboardButton(
                    text=button.text, callback_data=_pack(button.action, {**render_arguments, **button.arguments})
                )
            )
        rows.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render(ctx: Context, renderer: Callable[..., Any], arguments: Mapping[str, object]) -> Card:
    _declaration(renderer, "card")
    parameters = _parameters(renderer)
    values = _validate(parameters, {p.name: arguments[p.name] for p in parameters if p.name in arguments})
    result = renderer(ctx=ctx, **values)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, Card):
        raise CardError("A card renderer must return Card")
    return result


async def show(ctx: Context, renderer: Callable[..., Any], **arguments: object) -> object:
    """Render and send one card. The application binds durable UI identity if needed."""
    view = await _render(ctx, renderer, arguments)
    return await ctx.reply(**view._content(_keyboard(view, renderer, arguments)), fixed=True)


def card[H: Callable[..., Any]](method: H) -> H:
    """Mark a pure feature renderer; direct method calls remain ordinary Python."""
    attach_declaration(method, Declaration(kind="card", metadata={"card": True, "_locks": _CardLocks()}))
    return method


def action(
    *,
    card: str,
    refresh: bool = True,
    ack: Literal["auto", "early"] = "auto",
    coalesce: bool = False,
    flags: Mapping[str, Any] | None = None,
    filters: tuple[Callable[..., Any], ...] = (),
) -> Callable[[_Handler], _Handler]:
    """Declare an action; application code guards actor, origin and revision.

    Early acknowledgement happens before the card lock and forfeits later alert
    results. Coalescing drops clicks while the same UI is busy: opt in only for
    disposable refresh requests, never for mutations that each need to run.
    """
    if ack not in {"auto", "early"}:
        raise CardError("Managed action acknowledgement must be 'auto' or 'early'")

    def decorate(method: _Handler) -> _Handler:
        name = method.__name__

        def bound_filters(feature: Feature) -> tuple[Callable[..., Any], ...]:
            return (_ActionFilter(getattr(feature, name)),)

        async def hook(
            feature: Feature, ctx: Context, data: dict[str, Any], invoke: Callable[[], Awaitable[object]]
        ) -> object:
            if not isinstance(ctx, CallbackContext) or not isinstance(ctx.event, CallbackQuery):
                raise CardError("Managed actions require a callback context")
            message = ctx.event.message
            if not isinstance(message, Message) or message.from_user is None or message.from_user.id != ctx.bot.id:
                raise CardError("Managed actions require an accessible message sent by this bot")
            arguments = cast(dict[str, object], data["_teleforge_card_arguments"])
            renderer = cast(Callable[..., Any], getattr(feature, card))
            locks = cast(_CardLocks, _declaration(renderer, "card").metadata["_locks"])
            if ack == "early":
                await ctx.answer()
            async with locks.hold((ctx.bot.id, message.chat.id, message.message_id), coalesce=coalesce) as acquired:
                if not acquired:
                    return None
                result = await invoke()
                if refresh and result is None:
                    try:
                        view = await _render(ctx, renderer, arguments)
                        await ctx.edit(**view._content(_keyboard(view, renderer, arguments)))
                    except Exception as exc:
                        raise CardRefreshError(card) from exc
                return result

        attach_declaration(
            method,
            Declaration(
                kind="callback",
                event="callback_query",
                flags=flags or {},
                filters=filters,
                filter_factory=bound_filters,
                hook=hook,
                metadata={"card_action": card, "refresh": refresh, "ack_timing": ack, "coalesce": coalesce},
            ),
        )
        return method

    return decorate
