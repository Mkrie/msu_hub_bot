"""Derp product entrypoints without provider, pricing or persistence decisions."""

from dataclasses import dataclass
from typing import Protocol

from aiogram import F
from aiogram.methods import AnswerInlineQuery, AnswerPreCheckoutQuery
from aiogram.types import (
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
    PhotoSize,
    PreCheckoutQuery,
)
from pydantic import BaseModel, ConfigDict, Field
from teleforge import (
    App,
    Context,
    Feature,
    ImageInput,
    InputError,
    MessageContext,
    TextInput,
    chosen_inline_result,
    command,
    event,
    inline_query,
    job,
    pre_checkout_query,
)


@dataclass(frozen=True, slots=True)
class RequestScope:
    """Identity supplied to Derp, which decides permissions and duplicate handling."""

    request_key: str
    actor_id: int
    chat_id: int | None = None
    thread_id: int | None = None
    business_connection_id: str | None = None


def _scope(ctx: MessageContext, prompt: str) -> RequestScope:
    if not prompt.strip():
        raise InputError("Send a prompt or reply to the text you want to use.")
    if ctx.user is None or not isinstance(ctx.event, Message):
        raise InputError("This request needs an identifiable sender.")
    message = ctx.event
    return RequestScope(
        request_key=f"message:{ctx.bot.id}:{message.business_connection_id or '-'}:{message.chat.id}:{message.message_id}",
        actor_id=ctx.user.id,
        chat_id=message.chat.id,
        thread_id=message.message_thread_id if message.is_topic_message else None,
        business_connection_id=message.business_connection_id,
    )


class AnswerService(Protocol):
    async def answer(self, prompt: str, scope: RequestScope) -> str:
        """Derp owns model selection, context, access and request deduplication."""
        ...


class Assistant(Feature, key="derp.assistant"):
    def __init__(self, answers: AnswerService) -> None:
        self.answers = answers

    @command("ask", "derp", prompt=TextInput(reply=True))
    async def ask(self, ctx: MessageContext, prompt: str) -> str:
        return await self.answers.answer(prompt, _scope(ctx, prompt))

    @inline_query()
    async def offer_inline(self, ctx: Context, event: InlineQuery) -> AnswerInlineQuery:
        if not event.query.strip():
            return event.answer([], cache_time=0, is_personal=True)
        article = InlineQueryResultArticle(
            id="answer",
            title="Ask Derp",
            input_message_content=InputTextMessageContent(message_text="Preparing your answer…", parse_mode=None),
            # Telegram supplies inline_message_id for the selected result with a keyboard.
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Ask another", switch_inline_query_current_chat="")]]
            ),
        )
        return event.answer([article], cache_time=0, is_personal=True)

    @chosen_inline_result()
    async def answer_inline(self, ctx: Context, event: ChosenInlineResult) -> None:
        result = event
        if result.result_id != "answer" or not result.inline_message_id or not result.query.strip():
            return
        scope = RequestScope(
            request_key=f"inline:{ctx.bot.id}:{result.from_user.id}:{result.inline_message_id}",
            actor_id=result.from_user.id,
        )
        answer = await self.answers.answer(result.query, scope)
        await ctx.edit(answer, kind="text")


@dataclass(frozen=True, slots=True)
class CreatedImage:
    data: bytes
    caption: str | None = None


class ImageService(Protocol):
    async def create(self, prompt: str, scope: RequestScope, *, source: PhotoSize | None = None) -> CreatedImage:
        """A real service may authorize or defer work; no fabricated agent transcript is needed."""
        ...


class Images(Feature, key="derp.images"):
    def __init__(self, images: ImageService) -> None:
        self.images = images

    @command("imagine", "image", prompt=TextInput(reply=True))
    async def imagine(self, ctx: MessageContext, prompt: str) -> None:
        result = await self.images.create(prompt, _scope(ctx, prompt))
        await ctx.reply(result.caption, photo=result.data)

    @command("edit", prompt=TextInput(reply=False), source=ImageInput(reply=True))
    async def edit(self, ctx: MessageContext, prompt: str, source: PhotoSize) -> None:
        result = await self.images.create(prompt, _scope(ctx, prompt), source=source)
        await ctx.reply(result.caption, photo=result.data)


class RecoverPayment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    receipt_id: str = Field(min_length=1)


class Payments(Protocol):
    async def check(self, query: PreCheckoutQuery) -> str | None:
        """Return a rejection explanation, or None; validate the native invoice facts."""
        ...

    async def accept(self, message: Message) -> None:
        """Atomically deduplicate the payment and enqueue recovery in Derp's store."""
        ...

    async def recover(self, receipt_id: str) -> None:
        """Recover from persisted state; Derp owns claims and uncertain-delivery decisions."""
        ...


class Commerce(Feature, key="derp.commerce"):
    def __init__(self, payments: Payments) -> None:
        self.payments = payments

    @pre_checkout_query()
    async def checkout(self, ctx: Context, event: PreCheckoutQuery) -> AnswerPreCheckoutQuery:
        reason = await self.payments.check(event)
        return event.answer(ok=reason is None, error_message=reason)

    @event("message", F.successful_payment)
    async def paid(self, ctx: MessageContext, message: Message) -> None:
        await self.payments.accept(message)

    @job("recover-payment", payload=RecoverPayment)
    async def recover_payment(self, payload: RecoverPayment) -> None:
        await self.payments.recover(payload.receipt_id)


def create_app(answers: AnswerService, images: ImageService, payments: Payments) -> App:
    return App(Assistant(answers), Images(images), Commerce(payments))
