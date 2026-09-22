"""Invocation-local Telegram context; feature instances never own request state."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TypedDict, Unpack

from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChosenInlineResult,
    InaccessibleMessage,
    InlineKeyboardMarkup,
    InputRichMessage,
    Message,
    MessageEntity,
    ReplyMarkupUnion,
    User,
)
from aiogram.types.base import TelegramObject
from aiogram.utils.formatting import Text

from .delivery import (
    DeliveryError,
    DeliveryProgress,
    DeliveryTarget,
    MediaSource,
    NativeResult,
    ResponsePolicy,
    Target,
    TargetKind,
    edit_response,
    rejected,
    send_response,
)
from .formatting import ResponseError, units


class ReplyOptions(TypedDict, total=False):
    policy: ResponsePolicy
    photo: MediaSource | None
    video: MediaSource | None
    audio: MediaSource | None
    document: MediaSource | None
    animation: MediaSource | None
    entities: Sequence[MessageEntity] | None
    reply_markup: ReplyMarkupUnion | None
    fixed: bool
    width: int | None
    height: int | None
    duration: int | None
    supports_streaming: bool | None
    allow_sending_without_reply: bool
    allow_remote_media: bool
    request_timeout: int | None
    progress: DeliveryProgress | None


class EditOptions(TypedDict, total=False):
    policy: ResponsePolicy
    kind: TargetKind | None
    rich_message: InputRichMessage | None
    photo: MediaSource | None
    video: MediaSource | None
    audio: MediaSource | None
    document: MediaSource | None
    animation: MediaSource | None
    entities: Sequence[MessageEntity] | None
    reply_markup: InlineKeyboardMarkup | None
    allow_remote_media: bool
    request_timeout: int | None
    progress: DeliveryProgress | None


class Context:
    """The native event, acquired input provenance and delivery address are separate."""

    def __init__(
        self,
        bot: Bot,
        event: TelegramObject,
        *,
        data: dict[str, Any] | None = None,
        policy: ResponsePolicy | None = None,
    ) -> None:
        self.bot = bot
        self.event = event
        self.data = data if data is not None else {}
        self.policy = policy if policy is not None else ResponsePolicy()
        self.input_sources: dict[str, Message] = {}
        self.response_target: Target | None = self.message
        self.delivery_progress: DeliveryProgress | None = None
        self.has_effects = False
        if isinstance(event, CallbackQuery | ChosenInlineResult) and event.inline_message_id:
            self.response_target = DeliveryTarget(inline_message_id=event.inline_message_id)
        elif self.response_target is None:
            chat = getattr(event, "chat", None)
            if isinstance(chat, Chat):
                self.response_target = DeliveryTarget(chat_id=chat.id)

    @property
    def user(self) -> User | None:
        actor = getattr(self.event, "from_user", None)
        if isinstance(actor, User):
            return actor
        actor = getattr(self.event, "user", None)
        return actor if isinstance(actor, User) else None

    @property
    def message(self) -> Message | InaccessibleMessage | None:
        if isinstance(self.event, Message):
            return self.event
        if isinstance(self.event, CallbackQuery):
            return self.event.message
        return None

    @property
    def actor(self) -> User | None:
        return self.user

    async def reply(
        self,
        text: str | Text | None = None,
        *,
        to: Target | None = None,
        **options: Unpack[ReplyOptions],
    ) -> Message | list[Message]:
        target = to if to is not None else self.response_target
        if target is None:
            raise ResponseError("This event has no response target; supply to explicitly")
        if to is None and isinstance(target, InaccessibleMessage):
            raise ResponseError("The callback message is inaccessible; supply an explicit DeliveryTarget for a reply")
        options.setdefault("policy", self.policy)
        progress = options.get("progress") or DeliveryProgress()
        options["progress"] = self.delivery_progress = progress
        try:
            result = await send_response(self.bot, target, text, **options)
        finally:
            self.has_effects |= bool(progress.confirmed) or progress.uncertain
        self.has_effects = True
        return result

    async def edit(
        self,
        text: str | Text | None = None,
        *,
        to: Target | None = None,
        **options: Unpack[EditOptions],
    ) -> NativeResult:
        # Input acquisition may retarget reply(), but edit() always addresses the actual UI.
        target = to if to is not None else self.message
        if (
            target is None
            and isinstance(self.event, CallbackQuery | ChosenInlineResult)
            and self.event.inline_message_id
        ):
            target = DeliveryTarget(inline_message_id=self.event.inline_message_id)
        if target is None:
            raise ResponseError("This event has no editable UI; supply to explicitly")
        options.setdefault("policy", self.policy)
        progress = options.get("progress") or DeliveryProgress()
        options["progress"] = self.delivery_progress = progress
        try:
            result = await edit_response(self.bot, target, text, **options)
        finally:
            self.has_effects |= bool(progress.confirmed) or progress.uncertain
        self.has_effects = True
        return result

    async def finish(self) -> None:
        """Normal completion hook. It must not be called while unwinding an error."""


class MessageContext(Context):
    """A message invocation; its acquired inputs need not come from that message."""

    event: Message

    def __init__(
        self, bot: Bot, event: Message, *, data: dict[str, Any] | None = None, policy: ResponsePolicy | None = None
    ) -> None:
        super().__init__(bot, event, data=data, policy=policy)

    @property
    def message(self) -> Message:
        return self.event


@dataclass(slots=True)
class Acknowledgement:
    owned: bool = True
    attempted: bool = False
    confirmed: bool = False
    uncertain: bool = False


class CallbackContext(Context):
    event: CallbackQuery

    def __init__(
        self,
        bot: Bot,
        event: CallbackQuery,
        *,
        data: dict[str, Any] | None = None,
        policy: ResponsePolicy | None = None,
    ) -> None:
        super().__init__(bot, event, data=data, policy=policy)
        self.query = event
        self.acknowledgement = Acknowledgement()

    def manual_ack(self) -> CallbackQuery:
        """Give acknowledgement ownership to a native handler; finish becomes inert."""
        self.acknowledgement.owned = False
        return self.query

    async def answer(
        self,
        text: str | None = None,
        *,
        show_alert: bool = False,
        url: str | None = None,
        cache_time: int = 0,
        request_timeout: int | None = None,
    ) -> bool | None:
        state = self.acknowledgement
        if state.attempted:
            return None
        if text is not None:
            try:
                size = units(text)
            except UnicodeError:
                raise ResponseError("The callback notification contains invalid Unicode") from None
            if size > 200:
                raise ResponseError("Callback notifications must fit 200 UTF-16 units")
        if type(cache_time) is not int or cache_time < 0:
            raise ResponseError("Callback cache_time must be nonnegative")
        method = AnswerCallbackQuery(
            callback_query_id=self.query.id, text=text, show_alert=show_alert, url=url, cache_time=cache_time
        )
        state.attempted, state.uncertain = True, True
        try:
            async with asyncio.timeout(self.policy.timeout):
                result = await self.bot(method, request_timeout=request_timeout)
            if result is not True:
                raise ValueError("Telegram did not confirm the acknowledgement")
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - acknowledgement write boundary
            state.uncertain = not rejected(error)
            raise DeliveryError(
                DeliveryProgress(attempted_part=0, total_parts=1, phase="failed", uncertain=state.uncertain), error
            ) from None
        state.confirmed, state.uncertain = True, False
        return True

    async def finish(self) -> None:
        if self.acknowledgement.owned and not self.acknowledgement.attempted:
            await self.answer()


def context_for(
    bot: Bot,
    event: TelegramObject,
    *,
    data: dict[str, Any] | None = None,
    policy: ResponsePolicy | None = None,
) -> Context:
    if isinstance(event, CallbackQuery):
        return CallbackContext(bot, event, data=data, policy=policy)
    if isinstance(event, Message):
        return MessageContext(bot, event, data=data, policy=policy)
    return Context(bot, event, data=data, policy=policy)
