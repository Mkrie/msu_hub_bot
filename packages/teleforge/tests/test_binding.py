import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram.filters.callback_data import CallbackData
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, MessageId, Update, User

from teleforge.app import App
from teleforge.cards import Button, Card, action, card, show
from teleforge.context import CallbackContext, MessageContext
from teleforge.declarations import callback, command
from teleforge.feature import Feature
from teleforge.formatting import ResponseError
from teleforge.inputs import Argument, TextInput
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
            yield {"file": path}
        finally:
            path.unlink()

    class File(Feature):
        @command("file", output="document")
        async def file(self, file: Path) -> Path:
            return file

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

        @action(card="panel")
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
