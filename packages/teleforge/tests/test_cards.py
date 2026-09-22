from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User
from pydantic import AfterValidator

from teleforge.cards import Button, Card, CardError, CardRefreshError, action, card, show
from teleforge.context import CallbackContext, Context
from teleforge.declarations import declarations_of, disable
from teleforge.feature import Feature
from teleforge.testing import RecordingBot


def message(*, actor: int = 7, message_id: int = 10, topic: int = 5, chat_id: int = -100) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="supergroup"),
        from_user=User(id=actor, is_bot=actor == 42, first_name="Synthetic"),
        text="card",
        message_thread_id=topic,
        is_topic_message=True,
    )


class StaleClick(ValueError):
    pass


class Counter(Feature, key="counter"):
    def __init__(self) -> None:
        self.value = 0
        self.revision = 0
        self.ui_id = 0
        self.fail_render = False
        self.active = 0
        self.max_active = 0

    @card
    async def panel(self, ctx: Context, item: int) -> Card:
        if self.fail_render:
            raise RuntimeError("render failed")
        return Card(
            f"Item {item}: {self.value}",
            buttons=(
                (
                    Button("+", self.increase, revision=self.revision),
                    Button("-", self.decrease, revision=self.revision),
                ),
            ),
        )

    def guard(self, ctx: Context, revision: int) -> None:
        ui = ctx.message
        if ctx.user is None or ctx.user.id != 7 or not isinstance(ui, Message):
            raise PermissionError("actor")
        if (ui.chat.id, ui.message_id, ui.message_thread_id) != (-100, self.ui_id, 5):
            raise PermissionError("origin")
        if revision != self.revision:
            raise StaleClick("revision")

    async def change(self, ctx: Context, revision: int, delta: int) -> None:
        self.guard(ctx, revision)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            self.value += delta
            self.revision += 1
        finally:
            self.active -= 1

    @action(card="panel")
    async def increase(self, ctx: Context, item: int, revision: int) -> None:
        await self.change(ctx, revision, 1)

    @action(card="panel")
    async def decrease(self, ctx: Context, item: int, revision: int) -> None:
        await self.change(ctx, revision, -1)


async def open_card(bot: RecordingBot, feature: Counter) -> tuple[Message, list[str]]:
    sent = await show(Context(bot, message()), feature.panel, item=3)
    assert isinstance(sent, Message)
    feature.ui_id = sent.message_id
    assert sent.reply_markup is not None
    callbacks = [button.callback_data for button in sent.reply_markup.inline_keyboard[0]]
    assert all(isinstance(value, str) for value in callbacks)
    return sent, callbacks  # type: ignore[return-value]


async def click(
    bot: RecordingBot, feature: Counter, ui: Message, value: str, *, method: str = "increase", actor: int = 7
) -> object:
    callback = CallbackQuery(
        id=value,
        from_user=User(id=actor, is_bot=False, first_name="Clicker"),
        chat_instance="test",
        message=ui,
        data=value,
    )
    declaration = declarations_of(getattr(feature, method))[0]
    assert declaration.filter_factory is not None
    matched = await declaration.filter_factory(feature)[0](callback)
    if matched is False:
        return False
    assert isinstance(matched, dict)
    ctx = CallbackContext(bot, callback, data=matched)

    async def invoke() -> object:
        values: dict[str, Any] = matched["_teleforge_payload"]
        return await getattr(feature, method)(ctx=ctx, **values)

    assert declaration.hook is not None
    result = await declaration.hook(feature, ctx, matched, invoke)
    await ctx.finish()
    return result


async def test_action_changes_once_refreshes_keyboard_and_rejects_stale_click() -> None:
    bot, feature = RecordingBot(), Counter()
    ui, callbacks = await open_card(bot, feature)
    await click(bot, feature, ui, callbacks[0])
    assert feature.value == 1
    edits = [request for request in bot.requests if isinstance(request, EditMessageText)]
    assert len(edits) == 1 and edits[0].message_id == ui.message_id
    assert edits[0].reply_markup is not None
    assert edits[0].reply_markup.inline_keyboard[0][0].callback_data != callbacks[0]
    with pytest.raises(StaleClick):
        await click(bot, feature, ui, callbacks[0])
    assert feature.value == 1


async def test_concurrent_different_buttons_share_ui_lock() -> None:
    bot, feature = RecordingBot(), Counter()
    ui, callbacks = await open_card(bot, feature)
    results = await asyncio.gather(
        click(bot, feature, ui, callbacks[0]),
        click(bot, feature, ui, callbacks[1], method="decrease"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, StaleClick) for result in results) == 1
    assert feature.max_active == 1 and feature.revision == 1


@pytest.mark.parametrize("alteration", ["actor", "message", "topic", "chat", "bot"])
async def test_application_guards_and_native_ui_boundary(alteration: str) -> None:
    bot, feature = RecordingBot(), Counter()
    ui, callbacks = await open_card(bot, feature)
    update: dict[str, object] = {}
    if alteration == "message":
        update["message_id"] = ui.message_id + 1
    elif alteration == "topic":
        update["message_thread_id"] = 9
    elif alteration == "chat":
        update["chat"] = Chat(id=-200, type="supergroup")
    elif alteration == "bot":
        update["from_user"] = User(id=99, is_bot=True, first_name="Other bot")
    with pytest.raises((PermissionError, CardError)):
        await click(bot, feature, ui.model_copy(update=update), callbacks[0], actor=8 if alteration == "actor" else 7)
    assert feature.value == 0


async def test_invalid_typed_payload_is_not_forgiving() -> None:
    bot, feature = RecordingBot(), Counter()
    ui, callbacks = await open_card(bot, feature)
    prefix = callbacks[0].rsplit(":", 1)[0]
    assert await click(bot, feature, ui, prefix + ':["3",0]') is False
    assert await click(bot, feature, ui, prefix + ":[3,true]") is False
    assert await click(bot, feature, ui, prefix + ":[3]") is False
    assert feature.value == 0


async def test_successful_mutation_is_not_replayed_after_refresh_failure() -> None:
    bot, feature = RecordingBot(), Counter()
    ui, callbacks = await open_card(bot, feature)
    feature.fail_render = True
    with pytest.raises(CardRefreshError) as caught:
        await click(bot, feature, ui, callbacks[0])
    assert caught.value.applied and isinstance(caught.value.__cause__, RuntimeError)
    assert feature.value == 1 and feature.revision == 1
    with pytest.raises(StaleClick):
        await click(bot, feature, ui, callbacks[0])
    assert len([request for request in bot.requests if isinstance(request, SendMessage)]) == 1


async def test_oversized_button_fails_before_sending() -> None:
    class Large(Feature):
        @card
        async def panel(self, ctx: Context) -> Card:
            return Card("large", buttons=((Button("run", self.run, value="x" * 70),),))

        @action(card="panel")
        async def run(self, ctx: Context, value: str) -> None:
            pass

    bot, feature = RecordingBot(), Large()
    with pytest.raises(CardError, match="64-byte"):
        await show(Context(bot, message()), feature.panel)
    assert not bot.requests


async def test_ordinary_override_keeps_card_and_action_declarations() -> None:
    class Child(Counter):
        async def increase(self, ctx: Context, item: int, revision: int) -> None:
            await self.change(ctx, revision, 2)

    bot, feature = RecordingBot(), Child()
    ui, callbacks = await open_card(bot, feature)
    await click(bot, feature, ui, callbacks[0])
    assert feature.value == 2


async def test_disabled_action_cannot_render_a_managed_button() -> None:
    class Child(Counter):
        @disable
        async def increase(self, ctx: Context, item: int, revision: int) -> None:
            pass

    with pytest.raises(CardError, match="card_action"):
        await open_card(RecordingBot(), Child())


async def test_read_only_refresh_acknowledges_early_and_coalesces_busy_clicks() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Refresh(Counter):
        @action(card="panel", ack="early", coalesce=True)
        async def increase(self, ctx: Context, item: int, revision: int) -> None:
            assert any(isinstance(request, AnswerCallbackQuery) for request in ctx.bot.requests)
            entered.set()
            await release.wait()
            self.value += 1

    bot, feature = RecordingBot(), Refresh()
    ui, callbacks = await open_card(bot, feature)
    first = asyncio.create_task(click(bot, feature, ui, callbacks[0]))
    await entered.wait()
    await click(bot, feature, ui, callbacks[0])
    assert len([request for request in bot.requests if isinstance(request, AnswerCallbackQuery)]) == 2
    assert feature.value == 0
    release.set()
    await first
    assert feature.value == 1
    assert len([request for request in bot.requests if isinstance(request, EditMessageText)]) == 1


async def test_explicit_refresh_barrier_preserves_committed_action_without_edit() -> None:
    class Preview(Counter):
        @action(card="panel", refresh=False)
        async def increase(self, ctx: Context, item: int, revision: int) -> None:
            await self.change(ctx, revision, 1)

    bot, feature = RecordingBot(), Preview()
    ui, callbacks = await open_card(bot, feature)
    await click(bot, feature, ui, callbacks[0])
    assert feature.value == 1
    assert not any(isinstance(request, EditMessageText) for request in bot.requests)


async def test_numeric_literal_payload_does_not_accept_boolean_or_float_equivalents() -> None:
    class LiteralCounter(Counter):
        @action(card="panel")
        async def increase(self, ctx: Context, item: int, revision: Literal[0, 1]) -> None:
            await self.change(ctx, revision, 1)

    bot, feature = RecordingBot(), LiteralCounter()
    ui, callbacks = await open_card(bot, feature)
    prefix = callbacks[0].rsplit(":", 1)[0]
    assert await click(bot, feature, ui, prefix + ":[3,false]") is False
    assert await click(bot, feature, ui, prefix + ":[3,0.0]") is False
    assert feature.value == 0


async def test_renderer_defaults_are_bound_into_actions_before_action_defaults() -> None:
    class Defaults(Counter):
        @card
        async def panel(self, ctx: Context, item: int = 1) -> Card:
            return Card(f"Item {item}", buttons=((Button("+", self.increase, revision=0),),))

        @action(card="panel")
        async def increase(self, ctx: Context, item: int = 2, revision: int = 0) -> None:
            self.value = item

    bot, feature = RecordingBot(), Defaults()
    sent = await show(Context(bot, message()), feature.panel)
    assert isinstance(sent, Message) and sent.reply_markup is not None
    payload = sent.reply_markup.inline_keyboard[0][0].callback_data
    assert payload is not None
    await click(bot, feature, sent, payload)
    assert feature.value == 1


async def test_unknown_renderer_argument_fails_before_send() -> None:
    with pytest.raises(CardError, match="Unknown"):
        await show(Context(RecordingBot(), message()), Counter().panel, item=3, typo=4)


async def test_card_coalescing_is_owned_by_each_feature_instance() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Refresh(Counter):
        def __init__(self, *, pause: bool) -> None:
            super().__init__()
            self.pause = pause

        @action(card="panel", coalesce=True)
        async def increase(self, ctx: Context, item: int, revision: int) -> None:
            if self.pause:
                entered.set()
                await release.wait()
            self.value += 1

    first, second = Refresh(pause=True), Refresh(pause=False)
    first_bot, second_bot = RecordingBot(), RecordingBot()
    first_ui, first_callbacks = await open_card(first_bot, first)
    second_ui, second_callbacks = await open_card(second_bot, second)
    assert first_ui.message_id == second_ui.message_id
    pending = asyncio.create_task(click(first_bot, first, first_ui, first_callbacks[0]))
    try:
        await entered.wait()
        await click(second_bot, second, second_ui, second_callbacks[0])
        assert second.value == 1
    finally:
        release.set()
        await pending


async def test_validator_function_addresses_do_not_change_button_identity() -> None:
    def application() -> Counter:
        def validate(value: int) -> int:
            return value

        class Typed(Counter, key="stable"):
            Revision = Annotated[int, AfterValidator(validate)]

            @action(card="panel")
            async def increase(self, ctx: Context, item: int, revision: Revision) -> None:
                await self.change(ctx, revision, 1)

        return Typed()

    first, second = application(), application()
    _, before_restart = await open_card(RecordingBot(), first)
    bot = RecordingBot()
    ui, after_restart = await open_card(bot, second)
    assert before_restart == after_restart
    await click(bot, second, ui, before_restart[0])
    assert second.value == 1


async def test_cancelled_waiter_releases_only_its_own_card_lock_registration() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Waiting(Counter):
        @action(card="panel")
        async def increase(self, ctx: Context, item: int, revision: int) -> None:
            entered.set()
            await release.wait()
            self.value += 1

    bot, feature = RecordingBot(), Waiting()
    ui, callbacks = await open_card(bot, feature)
    first = asyncio.create_task(click(bot, feature, ui, callbacks[0]))
    await entered.wait()
    waiter = asyncio.create_task(click(bot, feature, ui, callbacks[0]))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await first
    await click(bot, feature, ui, callbacks[0])
    assert feature.value == 2


async def test_transforming_callback_validator_cannot_retarget_the_displayed_record() -> None:
    def change_identity(value: int) -> int:
        return value + 1

    class Transforming(Counter):
        Item = Annotated[int, AfterValidator(change_identity)]

        @card
        async def panel(self, ctx: Context, item: Item) -> Card:
            return Card(f"Record {item}", buttons=((Button("+", self.increase, revision=0),),))

        @action(card="panel")
        async def increase(self, ctx: Context, item: Item, revision: int) -> None:
            self.value = item

    bot = RecordingBot()
    with pytest.raises(CardError, match="normalize before building"):
        await open_card(bot, Transforming())
    assert not bot.requests
