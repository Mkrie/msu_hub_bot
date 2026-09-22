"""One native event-first adapter owns preparation, invocation and delivery."""

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from typing import Any, cast

from aiogram import Bot
from aiogram.filters.command import CommandObject
from aiogram.methods import AnswerCallbackQuery, TelegramMethod
from aiogram.types import Message, TelegramObject
from aiogram.utils.formatting import Text
from pydantic import BaseModel

from .context import CallbackContext, Context, context_for
from .delivery import MediaSource, ResponsePolicy
from .feature import CompiledHandler
from .formatting import ResponseError
from .inputs import Declaration as InputDeclaration
from .inputs import InputError, prepare_arguments

type Adapter = Callable[..., Awaitable[object]]


async def _deliver(ctx: Context, value: object, compiled: CompiledHandler) -> object:
    if value is None or isinstance(value, bool | TelegramObject):
        return value
    if isinstance(value, list) and all(isinstance(item, bool | TelegramObject) for item in value):
        return value
    if isinstance(value, TelegramMethod):
        # Native method returns must execute here, while telemetry/input scopes are open.
        if (
            isinstance(ctx, CallbackContext)
            and isinstance(value, AnswerCallbackQuery)
            and value.callback_query_id == ctx.query.id
        ):
            ctx.manual_ack()
        return await ctx.bot(value)
    if isinstance(ctx, CallbackContext):
        raise TypeError("Callback output must use ctx.answer, ctx.edit or ctx.reply explicitly")
    if isinstance(value, str | Text):
        return await ctx.reply(value)
    media = cast(MediaSource, value)
    match compiled.declaration.output:
        case "photo":
            return await ctx.reply(photo=media)
        case "video":
            return await ctx.reply(video=media)
        case "audio":
            return await ctx.reply(audio=media)
        case "document":
            return await ctx.reply(document=media)
        case "animation":
            return await ctx.reply(animation=media)
        case _:
            raise TypeError(f"{compiled.key} returned unsupported output; declare a media output or send explicitly")


async def invoke_handler(compiled: CompiledHandler, event: TelegramObject, **data: Any) -> object:
    bot = data.get("bot")
    if not isinstance(bot, Bot):
        bot = event.bot
    if bot is None:
        raise TypeError("An invocation requires an aiogram Bot")
    ctx = context_for(bot, event, data=data, policy=compiled.declaration.policy)
    data["_teleforge_context"] = ctx
    if isinstance(ctx, CallbackContext) and compiled.declaration.ack == "manual":
        ctx.manual_ack()
    command = data.get("command")
    tail = data.get("_teleforge_tail", (command.args or "") if isinstance(command, CommandObject) else None)
    if tail is not None and not isinstance(tail, str):
        raise TypeError("A custom command filter must provide a string _teleforge_tail")
    called = False
    async with AsyncExitStack() as resources:

        async def call() -> object:
            nonlocal called
            if called:
                raise RuntimeError("An invocation hook cannot execute the handler more than once")
            called = True
            payload = data.get("_teleforge_payload", data.get("callback_data"))
            if payload is not None and not isinstance(payload, BaseModel | Mapping):
                raise TypeError("Callback payload must be a validated model or mapping")
            kwargs = await resources.enter_async_context(
                prepare_arguments(
                    compiled.handler,
                    event,
                    ctx,
                    data,
                    cast(Mapping[str, InputDeclaration], compiled.declaration.inputs),
                    tail=tail,
                    payload=payload,
                )
            )
            return await compiled.handler(**kwargs)

        try:
            hook = compiled.declaration.hook
            result = await call() if hook is None else await hook(compiled.feature, ctx, data, call)
            delivered = await _deliver(ctx, result, compiled)
        except (InputError, ResponseError) as error:
            if ctx.has_effects:
                raise
            # Input guidance is a new reply to the invocation, never a replacement UI.
            if isinstance(ctx, CallbackContext):
                if not ctx.acknowledgement.attempted and ctx.acknowledgement.owned:
                    await ctx.answer(str(error)[:180], show_alert=True)
                else:
                    await ctx.reply(str(error), to=ctx.message, policy=ResponsePolicy(rich=False, soft_messages=1))
            elif isinstance(event, Message):
                await ctx.reply(str(error), to=event, policy=ResponsePolicy(rich=False, soft_messages=1))
            else:
                raise
            delivered = None
        await ctx.finish()
        return delivered


def adapter_for(compiled: CompiledHandler) -> Adapter:
    async def adapter(event: TelegramObject, **data: Any) -> object:
        return await invoke_handler(compiled, event, **data)

    # aiogram unwraps functions before inspecting them. Deliberately no __wrapped__.
    adapter.__name__ = compiled.handler.__name__
    adapter.__qualname__ = compiled.handler.__qualname__
    adapter.__module__ = compiled.handler.__module__
    adapter.__doc__ = compiled.handler.__doc__
    return adapter
