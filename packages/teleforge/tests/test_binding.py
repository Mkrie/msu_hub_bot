import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, InaccessibleMessage, Message, MessageId, Update, User

from teleforge.app import App
from teleforge.cards import Button, Card, action, card, show
from teleforge.context import CallbackContext, MessageContext
from teleforge.declarations import callback, command
from teleforge.delivery import DeliveryError
from teleforge.feature import Feature
from teleforge.formatting import ResponseError
from teleforge.inputs import Argument, InputError, TextInput
from teleforge.outcome import Invocation, InvocationMiddleware
from teleforge.testing import RecordingBot


def incoming(text: str) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=10,
            date=datetime.now(UTC),
            chat=Chat(id=1, type="private"),
            from_user=User(id=7, is_bot=False, first_name="actor"),
            text=text,
        ),
    )


class Count(CallbackData, prefix="count"):
    count: int


def clicked(data: str) -> Update:
    return Update(
        update_id=2,
        callback_query=CallbackQuery(
            id="click",
            chat_instance="instance",
            from_user=User(id=7, is_bot=False, first_name="actor"),
            message=Message(
                message_id=100,
                date=datetime.now(UTC),
                chat=Chat(id=1, type="private"),
                from_user=User(id=42, is_bot=True, first_name="bot"),
                text="UI",
            ),
            data=data,
        ),
    )


@pytest.mark.parametrize(("text", "expected"), [("/roll", "3"), ("/roll nope", "3"), ("/roll 1000", "100")])
async def test_typed_command_default_and_explicit_clamp(text: str, expected: str) -> None:
    class Dice(Feature):
        @command("roll", digits=Argument(clamp=(1, 100)))
        async def roll(self, digits: int = 3) -> str:
            return str(digits)

    bot = RecordingBot()
    async with App(Dice()) as app:
        await app.feed_update(bot, incoming(text))
    assert bot.requests[-1].text == expected


async def test_callback_payload_actor_explicit_edit_and_single_answer() -> None:
    observed: list[object] = []

    class Buttons(Feature):
        @callback(Count)
        async def press(self, ctx: CallbackContext, count: int, callback_data: Count) -> None:
            observed.extend((count, callback_data, ctx.user.id, ctx.message.from_user.id))
            await ctx.edit(str(count))
            await ctx.answer("Saved")

    bot = RecordingBot()
    async with App(Buttons()) as app:
        await app.feed_update(bot, clicked(Count(count=5).pack()))
    assert observed == [5, Count(count=5), 7, 42]
    assert [request.__api_method__ for request in bot.requests] == ["editMessageText", "answerCallbackQuery"]


async def test_callback_bare_content_is_programmer_error_without_implicit_ack() -> None:
    class Wrong(Feature):
        @callback(Count)
        async def press(self, count: int) -> str:
            return str(count)

    bot = RecordingBot()
    async with App(Wrong()) as app:
        with pytest.raises(TypeError, match="ctx.answer, ctx.edit or ctx.reply"):
            await app.feed_update(bot, clicked(Count(count=1).pack()))
    assert bot.requests == []


async def test_callback_cancellation_is_not_success_acknowledgement() -> None:
    class Cancelled(Feature):
        @callback(Count)
        async def press(self, ctx: CallbackContext, count: int) -> None:
            raise asyncio.CancelledError

    bot = RecordingBot()
    async with App(Cancelled()) as app:
        with pytest.raises(asyncio.CancelledError):
            await app.feed_update(bot, clicked(Count(count=1).pack()))
    assert bot.requests == []


async def test_native_ack_optout_does_not_double_answer() -> None:
    class Native(Feature):
        @callback(Count, ack="manual")
        async def press(self, query: CallbackQuery, count: int) -> None:
            await query.answer(str(count))

    bot = RecordingBot()
    async with App(Native()) as app:
        await app.feed_update(bot, clicked(Count(count=1).pack()))
    assert [request.__api_method__ for request in bot.requests] == ["answerCallbackQuery"]


async def test_native_method_return_delivered_inside_handler_scope() -> None:
    order: list[str] = []

    class Native(Feature):
        @command("native")
        async def native(self, ctx: MessageContext) -> SendMessage:
            return SendMessage(chat_id=1, text="native")

    async def middleware(handler: Any, event: Any, data: Any) -> object:
        order.append("enter")
        result = await handler(event, data)
        order.append("exit")
        assert len(bot.requests) == 1
        return result

    bot, app = RecordingBot(), App(Native())
    dispatcher = app.create_dispatcher()
    dispatcher.message.middleware(middleware)
    async with app:
        await app.feed_update(bot, incoming("/native"))
    assert order == ["enter", "exit"]


async def test_returned_input_file_remains_alive_through_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from teleforge import binding

    path = tmp_path / "borrowed.txt"

    @asynccontextmanager
    async def prepare(*args: Any, **kwargs: Any) -> AsyncIterator[dict[str, object]]:
        path.write_bytes(b"live input")
        try:
            yield {"ctx": args[2], "file": path}
        finally:
            path.unlink()

    class File(Feature):
        @command("file")
        async def file(self, ctx: MessageContext, *, file: Path) -> Message:
            return await ctx.reply(document=file, fixed=True)

    monkeypatch.setattr(binding, "prepare_arguments", prepare)
    bot = RecordingBot()
    async with App(File()) as app:
        await app.feed_update(bot, incoming("/file"))
    assert bot.recording.uploads[-1] == {"document": b"live input"}
    assert not path.exists()


async def test_handled_native_result_is_not_sent_again() -> None:
    class Native(Feature):
        @command("native")
        async def native(self, ctx: MessageContext) -> Message | list[Message]:
            return await ctx.reply("once")

    bot = RecordingBot()
    async with App(Native()) as app:
        await app.feed_update(bot, incoming("/native"))
    assert len(bot.requests) == 1


async def test_missing_text_never_uses_command_name_as_input() -> None:
    class Caption(Feature):
        @command("caption", text=TextInput())
        async def caption(self, text: str) -> str:
            raise AssertionError("Empty command must provide input guidance")

    bot = RecordingBot()
    async with App(Caption()) as app:
        await app.feed_update(bot, incoming("/caption"))
    assert bot.requests[-1].text == "Provide text for 'text'."


async def test_planning_error_after_successful_effect_does_not_send_guidance() -> None:
    class Partial(Feature):
        @command("partial")
        async def partial(self, ctx: MessageContext) -> None:
            await ctx.reply("saved")
            raise ResponseError("later planning failed")

    bot = RecordingBot()
    async with App(Partial()) as app:
        with pytest.raises(ResponseError, match="later planning"):
            await app.feed_update(bot, incoming("/partial"))
    assert len(bot.requests) == 1


async def test_native_nonmessage_results_are_already_handled() -> None:
    class Native(Feature):
        @command("native")
        async def native(self) -> list[MessageId | bool]:
            return [MessageId(message_id=50), True]

    bot = RecordingBot()
    async with App(Native()) as app:
        result = await app.feed_update(bot, incoming("/native"))
    assert result == [MessageId(message_id=50), True]
    assert bot.requests == []


async def test_returned_native_answer_has_one_acknowledgement_owner() -> None:
    class Native(Feature):
        @callback(Count)
        async def press(self, query: CallbackQuery, count: int) -> AnswerCallbackQuery:
            return query.answer(str(count))

    bot = RecordingBot()
    async with App(Native()) as app:
        await app.feed_update(bot, clicked(Count(count=1).pack()))
    assert [request.__api_method__ for request in bot.requests] == ["answerCallbackQuery"]


async def test_managed_card_uses_native_compiled_callback_and_strict_payload() -> None:
    class Counter(Feature):
        def __init__(self) -> None:
            self.count = 0

        @command("counter")
        async def open(self, ctx: MessageContext) -> None:
            await show(ctx, self.panel)

        @card
        async def panel(self, ctx: CallbackContext | MessageContext) -> Card:
            return Card(text=str(self.count), buttons=[[Button("Add", self.add, amount=1)]])

        @action(key="add", card="panel")
        async def add(self, ctx: CallbackContext, amount: int) -> None:
            self.count += amount

    bot, feature = RecordingBot(), Counter()
    async with App(feature) as app:
        await app.feed_update(bot, incoming("/counter"))
        callback_data = bot.requests[-1].reply_markup.inline_keyboard[0][0].callback_data
        await app.feed_update(bot, clicked(callback_data))
    assert feature.count == 1
    assert [request.__api_method__ for request in bot.requests] == [
        "sendMessage",
        "editMessageText",
        "answerCallbackQuery",
    ]


@pytest.mark.parametrize("inaccessible", [False, True])
async def test_manual_ack_guidance_never_guesses_unavailable_ui_scope(inaccessible: bool) -> None:
    primary = InputError("argument-missing", parameter="value")

    class Missing(Feature):
        @callback(Count, ack="manual")
        async def press(self, ctx: CallbackContext, count: int) -> None:
            await ctx.guide(primary)

    bot = RecordingBot()
    update = clicked(Count(count=1).pack())
    query = update.callback_query
    assert query is not None
    update = update.model_copy(
        update={
            "callback_query": query.model_copy(
                update={
                    "message": InaccessibleMessage(chat=Chat(id=-100, type="supergroup"), message_id=5, date=0)
                    if inaccessible
                    else None,
                    "inline_message_id": None if inaccessible else "inline",
                }
            )
        }
    )
    async with App(Missing()) as app:
        with pytest.raises(InputError) as caught:
            await app.feed_update(bot, update)
    assert caught.value is primary
    assert not bot.requests


async def test_failed_guidance_preserves_primary_error_and_does_not_ack_success() -> None:
    primary = InputError("argument-missing", parameter="value")

    class Missing(Feature):
        @callback(Count)
        async def press(self, ctx: CallbackContext, count: int) -> None:
            await ctx.guide(primary)

    bot = RecordingBot()
    bot.recording.responses.append(TimeoutError())
    async with App(Missing()) as app:
        with pytest.raises(InputError) as caught:
            await app.feed_update(bot, clicked(Count(count=1).pack()))
    assert caught.value is primary
    assert len(bot.requests) == 1
    assert "could not be delivered" in primary.__notes__[0]


async def test_callback_error_alert_obeys_utf16_budget() -> None:
    class Missing(Feature):
        @callback(Count)
        async def press(self, ctx: CallbackContext, count: int) -> None:
            await ctx.guide("😀" * 150)

    bot = RecordingBot()
    async with App(Missing()) as app:
        await app.feed_update(bot, clicked(Count(count=1).pack()))
    assert len(bot.requests) == 1
    assert bot.requests[0].text == "😀" * 90


@pytest.mark.parametrize("uncertain", [False, True])
async def test_ack_failure_preserves_handler_return_and_confirmed_edit(uncertain: bool) -> None:
    observations = []

    class Updated(Feature):
        @callback(Count)
        async def press(self, ctx: CallbackContext, count: int) -> None:
            await ctx.edit("already saved")

    bot, app = RecordingBot(), App(Updated())
    dispatcher = app.create_dispatcher()

    async def observe(handler: Any, event: Any, data: Any) -> object:
        invocation = data["teleforge_invocation"]
        assert isinstance(invocation, Invocation)
        try:
            return await handler(event, data)
        finally:
            observations.append(invocation.outcome)

    dispatcher.callback_query.middleware(observe)
    edit = bot.recording._default(bot, SendMessage(chat_id=1, text="saved"))
    failure = (
        TimeoutError()
        if uncertain
        else TelegramBadRequest(method=AnswerCallbackQuery(callback_query_id="click"), message="query too old")
    )
    bot.recording.responses.extend([edit, failure])
    async with app:
        with pytest.raises(DeliveryError) as caught:
            await app.feed_update(bot, clicked(Count(count=1).pack()))
    outcome = caught.value.teleforge_outcome
    assert outcome == observations[0]
    assert outcome.handler_returned
    assert outcome.acknowledgement.attempted and not outcome.acknowledgement.confirmed
    assert outcome.acknowledgement.uncertain is uncertain
    assert len(outcome.presentations) == 1
    assert outcome.presentations[0].confirmed == 1
    assert not outcome.presentations[0].uncertain
    with pytest.raises(FrozenInstanceError):
        outcome.handler_returned = False


async def test_localized_acquisition_rejection_is_visible_to_host_outer_middleware() -> None:
    observations = []

    class Caption(Feature):
        @command("caption", text=TextInput())
        async def caption(self, text: str) -> str:
            raise AssertionError("No input must not reach the handler")

    from aiogram import Dispatcher

    bot = RecordingBot()
    app = App(Caption(), input_formatter=lambda issue: "Пришли текст." if issue.code == "text-missing" else str(issue))
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(InvocationMiddleware())

    async def observe(handler: Any, event: Any, data: Any) -> object:
        invocation = data["teleforge_invocation"]
        result = await handler(event, data)
        observations.append(invocation.outcome)
        return result

    dispatcher.update.outer_middleware(observe)
    dispatcher.include_router(app.build_router())
    await dispatcher.feed_update(bot, incoming("/caption"))
    await dispatcher.fsm.close()
    assert bot.requests[-1].text == "Пришли текст."
    outcome = observations[0]
    assert not outcome.handler_returned
    assert outcome.input_issue.code == "text-missing"
    assert outcome.input_issue.params == (("parameter", "text"),)
    assert outcome.presentations[0].confirmed == 1


@pytest.mark.parametrize("kind", ["input", "response"])
async def test_handler_errors_are_not_converted_to_acquisition_guidance(kind: str) -> None:
    primary = InputError("text-missing", parameter="value") if kind == "input" else ResponseError("invalid output")

    class Broken(Feature):
        @command("broken")
        async def broken(self) -> None:
            raise primary

    bot = RecordingBot()
    async with App(Broken()) as app:
        with pytest.raises(type(primary)) as caught:
            await app.feed_update(bot, incoming("/broken"))
    assert caught.value is primary and not bot.requests
    assert not primary.teleforge_outcome.handler_returned
    assert primary.teleforge_outcome.input_issue is None


async def test_missing_dependency_is_configuration_failure_without_user_guidance() -> None:
    from teleforge.issues import ConfigurationError

    class Configured(Feature):
        @command("run")
        async def run(self, *, service: object) -> None:
            raise AssertionError("Missing injection must fail before invocation")

    bot = RecordingBot()
    async with App(Configured()) as app:
        with pytest.raises(ConfigurationError):
            await app.feed_update(bot, incoming("/run"))
    assert not bot.requests


async def test_resource_cleanup_failure_retains_confirmed_presentation(monkeypatch: pytest.MonkeyPatch) -> None:
    from teleforge import binding

    @asynccontextmanager
    async def prepare(*args: Any, **kwargs: Any) -> AsyncIterator[dict[str, object]]:
        try:
            yield {}
        finally:
            raise RuntimeError("cleanup failure")

    class Reply(Feature):
        @command("run")
        async def run(self) -> str:
            return "delivered"

    monkeypatch.setattr(binding, "prepare_arguments", prepare)
    bot = RecordingBot()
    async with App(Reply()) as app:
        with pytest.raises(RuntimeError, match="cleanup failure") as caught:
            await app.feed_update(bot, incoming("/run"))
    assert caught.value.teleforge_outcome.handler_returned
    assert caught.value.teleforge_outcome.presentations[0].confirmed == 1
    assert len(bot.requests) == 1


async def test_cancelled_auto_ack_retains_prior_confirmed_edit() -> None:
    entered = asyncio.Event()

    class Edited(Feature):
        @callback(Count)
        async def press(self, ctx: CallbackContext, count: int) -> None:
            await ctx.edit("saved")

    bot = RecordingBot()

    async def responder(selected: Any, method: Any) -> object:
        if isinstance(method, AnswerCallbackQuery):
            entered.set()
            await asyncio.Event().wait()
        return bot.recording._default(selected, method)

    bot.recording.responder = responder
    async with App(Edited()) as app:
        task = asyncio.create_task(app.feed_update(bot, clicked(Count(count=1).pack())))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
    outcome = caught.value.teleforge_outcome
    assert outcome.handler_returned and outcome.presentations[0].confirmed == 1
    assert outcome.acknowledgement.attempted and outcome.acknowledgement.uncertain


async def test_early_callback_fails_explicitly_for_unbridged_host_isolation() -> None:
    from aiogram import Dispatcher
    from aiogram.fsm.storage.memory import SimpleEventIsolation

    from teleforge.isolation import IsolationError

    class Panel(Feature):
        @command("open")
        async def open(self, ctx: MessageContext) -> None:
            await show(ctx, self.panel)

        @card
        async def panel(self) -> Card:
            return Card(text="panel", buttons=[[Button("Refresh", self.refresh)]])

        @action(key="refresh", card="panel", ack="early", coalesce=True)
        async def refresh(self) -> None:
            raise AssertionError("An unsupported release contract must fail before invocation")

    bot, app = RecordingBot(), App(Panel())
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    dispatcher.include_router(app.build_router())
    await dispatcher.feed_update(bot, incoming("/open"))
    payload = bot.requests[0].reply_markup.inline_keyboard[0][0].callback_data
    with pytest.raises(IsolationError, match="terminal FSM release"):
        await dispatcher.feed_update(bot, clicked(payload))
    await dispatcher.fsm.close()
    assert len(bot.requests) == 1
