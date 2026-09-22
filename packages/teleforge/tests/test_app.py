import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, InaccessibleMessage, Message, Update, User

from teleforge.app import App
from teleforge.cards import Button, Card, action, card, show
from teleforge.context import CallbackContext, MessageContext
from teleforge.declarations import command, event
from teleforge.feature import Feature
from teleforge.isolation import IsolationError
from teleforge.testing import RecordingBot


def message_update(text: str, *, topic: int | None = None, update_id: int = 1) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=10,
            date=datetime.now(UTC),
            chat=Chat(id=-100, type="supergroup"),
            from_user=User(id=7, first_name="actor", is_bot=False),
            text=text,
            is_topic_message=topic is not None,
            message_thread_id=topic,
        ),
    )


class Commands(Feature):
    def __init__(self) -> None:
        self.seen: list[str] = []

    @command("begin")
    async def begin(self, ctx: MessageContext, state: FSMContext) -> str:
        await state.set_state("pending")
        self.seen.append("begin")
        return "begun"

    @command("ordinary")
    async def ordinary(self, ctx: MessageContext) -> None:
        self.seen.append("ordinary")

    @command("cancel", filters=(StateFilter("*"),))
    async def cancel(self, ctx: MessageContext, state: FSMContext) -> str:
        await state.clear()
        self.seen.append("cancel")
        return "cancelled"

    @event("message", StateFilter("pending"))
    async def step(self, message: Message) -> None:
        self.seen.append(f"step:{message.text}")


async def test_command_state_default_preserves_cancel_and_topics() -> None:
    feature, bot = Commands(), RecordingBot()
    async with App(feature) as app:
        await app.feed_update(bot, message_update("/begin", topic=10))
        await app.feed_update(bot, message_update("/ordinary", topic=10))
        await app.feed_update(bot, message_update("/ordinary", topic=20))
        await app.feed_update(bot, message_update("/cancel", topic=10))
        await app.feed_update(bot, message_update("/ordinary", topic=10))
    assert feature.seen == ["begin", "step:/ordinary", "ordinary", "cancel", "ordinary"]
    assert not bot.recording.closed  # Caller-supplied Bot lifetime remains explicit.


async def test_native_first_match_and_workflow_di() -> None:
    seen: list[object] = []

    class First(Feature):
        @command("same")
        async def incoming(self, ctx: MessageContext, *, service: object) -> None:
            seen.append(service)

    class Second(Feature):
        @command("same")
        async def incoming(self, ctx: MessageContext) -> None:
            raise AssertionError("First matching handler must stop dispatch")

    original, override = object(), object()
    async with App(First(), Second(), data={"service": original}) as app:
        await app.feed_update(RecordingBot(), message_update("/same"), service=override)
    assert seen == [override]


@pytest.mark.parametrize("inaccessible", [False, True])
async def test_inline_or_inaccessible_callback_cannot_borrow_private_or_general_fsm(inaccessible: bool) -> None:
    observed: list[object] = []

    class Callbacks(Feature):
        @event("callback_query")
        async def click(self, ctx: CallbackContext, state: FSMContext | None = None) -> None:
            observed.append(state)

    user = User(id=7, first_name="actor", is_bot=False)
    query = CallbackQuery(
        id="callback",
        from_user=user,
        chat_instance="instance",
        data="any",
        message=InaccessibleMessage(chat=Chat(id=-100, type="supergroup"), message_id=20, date=0)
        if inaccessible
        else None,
        inline_message_id=None if inaccessible else "inline",
    )
    storage = MemoryStorage()
    await storage.set_state(StorageKey(bot_id=42, chat_id=-100 if inaccessible else 7, user_id=7), "sensitive")
    app = App(Callbacks())
    app.create_dispatcher(storage=storage)
    async with app:
        await app.feed_update(RecordingBot(), Update(update_id=1, callback_query=query))
    assert observed == [None]


async def test_lifespan_order_and_failed_startup_cleanup() -> None:
    history: list[str] = []

    @asynccontextmanager
    async def resource() -> AsyncIterator[object]:
        history.append("resource:start")
        try:
            yield object()
        finally:
            history.append("resource:stop")

    class Healthy(Feature):
        @asynccontextmanager
        async def lifespan(self, app: App) -> AsyncIterator[None]:
            history.append("healthy:start")
            try:
                yield
            finally:
                history.append("healthy:stop")

    class Broken(Feature):
        @asynccontextmanager
        async def lifespan(self, app: App) -> AsyncIterator[None]:
            history.append("broken:start")
            raise RuntimeError("startup")
            yield

    app = App(Healthy(), Broken()).resource(resource)
    with pytest.raises(RuntimeError, match="startup"):
        await app.start()
    assert history == ["resource:start", "healthy:start", "broken:start", "healthy:stop", "resource:stop"]
    await app.aclose()


async def test_standalone_shutdown_stops_feature_before_fsm_storage() -> None:
    history: list[str] = []

    class Storage(MemoryStorage):
        async def close(self) -> None:
            history.append("storage")

    class Worker(Feature):
        @asynccontextmanager
        async def lifespan(self, app: App) -> AsyncIterator[None]:
            try:
                yield
            finally:
                history.append("worker")

    app = App(Worker())
    dispatcher = app.create_dispatcher(storage=Storage())
    await dispatcher.emit_startup()
    await dispatcher.emit_shutdown()
    await app.aclose()
    assert history == ["worker", "storage"]


async def test_custom_command_filter_owns_grammar_tail_and_flags() -> None:
    async def hashtag(message: Message) -> bool | dict[str, object]:
        return {"_teleforge_tail": "4"} if message.text == "anything #roll" else False

    class Rolls(Feature):
        @command("roll", filter=hashtag, flags={"handler_key": "legacy.roll", "fsm_release": True})
        async def roll(self, digits: int = 3) -> str:
            return str(digits)

    bot = RecordingBot()
    app = App(Rolls())
    handler = app.build_router().sub_routers[0].message.handlers[0]
    assert handler.flags["handler_key"] == "legacy.roll"
    assert handler.flags["fsm_release"] is True
    async with app:
        await app.feed_update(bot, message_update("anything #roll"))
    assert bot.requests[-1].text == "4"


async def test_same_actor_refreshes_ack_and_coalesce_through_default_dispatcher() -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class Refresh(Feature):
        calls = 0

        @command("open")
        async def open(self, ctx: MessageContext) -> None:
            await show(ctx, self.panel)

        @card
        async def panel(self) -> Card:
            return Card(text="panel", buttons=[[Button("Refresh", self.refresh)]])

        @action(key="refresh", card="panel", ack="early", coalesce=True)
        async def refresh(self, ctx: CallbackContext) -> None:
            self.calls += 1
            started.set()
            await release.wait()

    feature, bot = Refresh(), RecordingBot()
    async with App(feature) as app:
        await app.feed_update(bot, message_update("/open"))
        request = bot.requests[0]
        callback = CallbackQuery(
            id="first",
            chat_instance="instance",
            from_user=User(id=7, first_name="actor", is_bot=False),
            message=bot.recording._default(bot, request),
            data=request.reply_markup.inline_keyboard[0][0].callback_data,
        )
        first = asyncio.create_task(app.feed_update(bot, Update(update_id=2, callback_query=callback)))
        await asyncio.wait_for(started.wait(), 1)
        try:
            await asyncio.wait_for(
                app.feed_update(bot, Update(update_id=3, callback_query=callback.model_copy(update={"id": "second"}))),
                1,
            )
            assert [item.__api_method__ for item in bot.requests] == [
                "sendMessage",
                "answerCallbackQuery",
                "answerCallbackQuery",
            ]
            assert feature.calls == 1
        finally:
            release.set()
            await first
    assert [item.__api_method__ for item in bot.requests].count("editMessageText") == 1


@pytest.mark.parametrize("misuse", ["state", "storage", "skip", "child"])
async def test_terminal_release_prevents_state_reuse_and_route_continuation(misuse: str) -> None:
    seen = []

    class Terminal(Feature):
        @command("release")
        async def release(self, ctx: MessageContext, state: FSMContext) -> None:
            if misuse == "child":
                await asyncio.create_task(ctx.release_isolation())
                return
            await ctx.release_isolation()
            if misuse == "state":
                await state.get_state()
            elif misuse == "storage":
                await ctx.data["fsm_storage"].get_data(state.key)
            else:
                raise SkipHandler

        @event("message")
        async def fallback(self) -> None:
            seen.append("wrong continuation")

    bot = RecordingBot()
    async with App(Terminal()) as app:
        with pytest.raises(IsolationError):
            await app.feed_update(bot, message_update("/release"))
    assert seen == [] and bot.requests == []


async def test_state_load_and_route_selection_remain_under_isolation() -> None:
    started, release = asyncio.Event(), asyncio.Event()
    seen = []

    class Conversation(Feature):
        @command("begin")
        async def begin(self, state: FSMContext) -> None:
            started.set()
            await release.wait()
            await state.set_state("pending")
            seen.append("begin")

        @command("ordinary")
        async def ordinary(self) -> None:
            seen.append("ordinary")

        @command("cancel", filters=(StateFilter("*"),))
        async def cancel(self, state: FSMContext) -> None:
            await state.clear()
            seen.append("cancel")

        @event("message", StateFilter("pending"))
        async def step(self) -> None:
            seen.append("step")

    async with App(Conversation()) as app:
        bot = RecordingBot()
        first = asyncio.create_task(app.feed_update(bot, message_update("/begin")))
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(app.feed_update(bot, message_update("/ordinary", update_id=2)))
        await asyncio.sleep(0)
        assert not second.done() and not seen
        release.set()
        await asyncio.gather(first, second)
        await app.feed_update(bot, message_update("/cancel", update_id=3))
        await app.feed_update(bot, message_update("/ordinary", update_id=4))
    assert seen == ["begin", "step", "cancel", "ordinary"]
