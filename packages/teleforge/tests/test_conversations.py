from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, User
from pydantic import BaseModel, ConfigDict

from teleforge.context import Context
from teleforge.conversations import ConversationError, enter, leave, step
from teleforge.declarations import declarations_of
from teleforge.feature import Feature
from teleforge.testing import RecordingBot


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: int


class Titles(Feature, key="titles"):
    def __init__(self) -> None:
        self.seen: list[int] = []

    @step("title", draft=Draft)
    async def title(self, ctx: Context, draft: Draft) -> None:
        self.seen.append(draft.source_id)


def context(*, user: int = 7, topic: int = 5) -> Context:
    bot = RecordingBot()
    message = Message(
        message_id=10,
        date=datetime.now(UTC),
        chat=Chat(id=-100, type="supergroup"),
        from_user=User(id=user, is_bot=False, first_name="Synthetic"),
        message_thread_id=topic,
        is_topic_message=True,
    )
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=42, chat_id=-100, user_id=user, thread_id=topic))
    return Context(bot, message, data={"state": state})


async def invoke(ctx: Context, feature: Titles) -> None:
    declaration = declarations_of(feature.title)[0]
    assert declaration.hook is not None

    async def call() -> object:
        return await feature.title(ctx, ctx.data["draft"])

    await declaration.hook(feature, ctx, ctx.data, call)


async def test_typed_step_runs_and_preserves_unrelated_fsm_data() -> None:
    ctx, feature = context(), Titles()
    state: FSMContext = ctx.data["state"]
    await state.update_data({"application": "preserved"})
    await enter(ctx, feature.title, Draft(source_id=12))
    assert await state.get_state() == "teleforge:titles:title"
    await invoke(ctx, feature)
    assert feature.seen == [12]
    await leave(ctx)
    assert await state.get_state() is None
    assert await state.get_data() == {"application": "preserved"}


@pytest.mark.parametrize("value", [{"source_id": "12"}, {"source_id": True}, {"source_id": 12, "extra": 1}, {}])
async def test_saved_draft_is_strictly_validated_before_handler(value: dict[str, Any]) -> None:
    ctx, feature = context(), Titles()
    await enter(ctx, feature.title, Draft(source_id=12))
    state: FSMContext = ctx.data["state"]
    await state.update_data({"__teleforge_draft__": {"feature": "titles", "step": "title", "value": value}})
    with pytest.raises(ConversationError, match="draft"):
        await invoke(ctx, feature)
    assert feature.seen == []


@pytest.mark.parametrize("field,value", [("user_id", 9), ("chat_id", -200), ("thread_id", None), ("bot_id", 99)])
async def test_fsm_key_must_match_actor_chat_topic_and_bot(field: str, value: int | None) -> None:
    ctx, feature = context(), Titles()
    values = {"bot_id": 42, "chat_id": -100, "user_id": 7, "thread_id": 5, field: value}
    ctx.data["state"] = FSMContext(MemoryStorage(), StorageKey(**values))  # type: ignore[arg-type]
    with pytest.raises(ConversationError, match="isolate"):
        await enter(ctx, feature.title, Draft(source_id=12))


async def test_other_feature_or_step_draft_is_never_dispatched() -> None:
    ctx, feature = context(), Titles()
    await enter(ctx, feature.title, Draft(source_id=12))
    state: FSMContext = ctx.data["state"]
    await state.update_data({"__teleforge_draft__": {"feature": "other", "step": "title", "value": {"source_id": 12}}})
    with pytest.raises(ConversationError, match="different"):
        await invoke(ctx, feature)
    await state.set_state("native:other")
    with pytest.raises(ConversationError, match="another"):
        await leave(ctx)
    assert await state.get_state() == "native:other"


async def test_invalid_draft_type_does_not_change_existing_state() -> None:
    class OtherDraft(BaseModel):
        pass

    ctx, feature = context(), Titles()
    state: FSMContext = ctx.data["state"]
    with pytest.raises(ConversationError, match="Draft"):
        await enter(ctx, feature.title, OtherDraft())
    assert await state.get_state() is None


async def test_nonforum_message_thread_hint_does_not_create_a_topic_scope() -> None:
    ctx, feature = context(), Titles()
    ctx.event = ctx.event.model_copy(update={"is_topic_message": False})
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=42, chat_id=-100, user_id=7, thread_id=None))
    ctx.data["state"] = state
    await enter(ctx, feature.title, Draft(source_id=12))
    await invoke(ctx, feature)
    assert feature.seen == [12]
