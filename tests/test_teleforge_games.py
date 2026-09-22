"""Real quiz state, leases and settlement through native feature dispatch."""

import asyncio
from datetime import timedelta

from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageCaption, EditMessageMedia, SendPhoto
from aiogram.types import CallbackQuery, Update, User
from teleforge import App

from msu_hub_bot.features.command import format_input_error
from msu_hub_bot.features.games import Games, QuizDelivery
from msu_hub_bot.games.quiz import QuizService
from msu_hub_bot.storage.features import FeatureStore, FeatureWorker
from quiz_helpers import rig as rig, score_values, settle


def mount(rig):
    rig.store = FeatureStore(rig.backend)
    rig.worker = FeatureWorker(rig.store)
    rig.quiz = QuizService(rig.bot, rig.store, rig.worker, sender=QuizDelivery(rig.bot))
    rig.quiz.clock = lambda: rig.backend.now
    return App(Games(rig.quiz), input_formatter=format_input_error)


async def open_game(rig, app):
    message = rig.message.model_copy(update={"text": f"/{rig.feature}"})
    await app.feed_update(rig.bot, Update(update_id=1, message=message))
    key = rig.quiz._token(rig.bot.id, message.chat.id, message.message_id)
    record = await rig.quiz.round(rig.feature, message.chat.id, key)
    assert record is not None and record.value.phase == "active"
    return record


async def vote(rig, app, record, choice, actor):
    query = CallbackQuery(
        id=f"vote-{actor}-{choice}",
        chat_instance="synthetic",
        message=rig.session.messages[record.value.message_id],
        from_user=User(id=actor, is_bot=False, first_name=f"Player {actor}"),
        data=f"{rig.feature}:{record.key}:{choice}",
    )
    await app.feed_update(rig.bot, Update(update_id=actor, callback_query=query))


async def test_game_survives_restart_then_deadline_edits_original_ui(rig):
    app = mount(rig)
    record = await open_game(rig, app)
    question = record.value.question
    await asyncio.gather(
        vote(rig, app, record, question.answer, 42),
        vote(rig, app, record, (question.answer + 1) % 6, 43),
    )
    await app.aclose()

    # New feature/service/worker objects reload the existing store, with no new provider call.
    replacement = mount(rig)
    restored = await rig.quiz.round(rig.feature, record.value.chat_id, record.key)
    assert restored.value.question == question and restored.value.vote_count == 2
    rig.backend.now += timedelta(minutes=11)
    await settle(rig)
    closed = await rig.quiz.round(rig.feature, record.value.chat_id, record.key)
    assert closed.value.phase == "closed" and closed.value.score_status == "recorded"
    assert await score_values(rig, closed.value.score_day) == {42: 1, 43: 0}
    # A repeated delivery/worker wake does not settle twice or create a replacement photo.
    await vote(rig, replacement, closed, "finish", 42)
    await settle(rig)
    assert await score_values(rig, closed.value.score_day) == {42: 1, 43: 0}
    photos = [method for method in rig.session.methods if isinstance(method, SendPhoto)]
    edits = [method for method in rig.session.methods if isinstance(method, (EditMessageCaption, EditMessageMedia))]
    assert len(photos) == 1 and edits
    assert photos[0].reply_parameters.message_id == rig.message.message_id
    assert photos[0].message_thread_id == 17
    assert all(method.message_id == record.value.message_id for method in edits)
    assert not any(isinstance(method, DeleteMessage) for method in rig.session.methods)
    await replacement.aclose()


async def test_failed_worker_edit_keeps_vote_and_repairs_without_second_vote(rig):
    app = mount(rig)
    record = await open_game(rig, app)
    await vote(rig, app, record, record.value.question.answer, 42)
    rig.session.caption_error = True
    await settle(rig)
    unchanged = await rig.quiz.round(rig.feature, record.value.chat_id, record.key)
    assert unchanged.value.vote_count == 1
    rig.session.caption_error = False
    # A later accepted vote schedules a fresh render using authoritative persisted votes.
    await vote(rig, app, record, (record.value.question.answer + 1) % 6, 43)
    await settle(rig)
    repaired = await rig.quiz.round(rig.feature, record.value.chat_id, record.key)
    assert repaired.value.vote_count == 2
    assert len([method for method in rig.session.methods if isinstance(method, SendPhoto)]) == 1
    assert any(isinstance(method, AnswerCallbackQuery) for method in rig.session.methods)
    await app.aclose()
