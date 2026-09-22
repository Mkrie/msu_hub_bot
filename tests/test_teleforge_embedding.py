"""Candidate features run under Hub's real state middleware and ownership rules."""

import asyncio
from contextlib import asynccontextmanager

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText
from teleforge import InvocationMiddleware

from msu_hub_bot.features.integration import HubIsolationBridge
from msu_hub_bot.telegram.state import (
    ReleasableEventIsolation,
    SelectiveIsolationMiddleware,
    StateContextMiddleware,
    TopicFSMContextMiddleware,
)
from test_teleforge_reactions import click, open_card, reaction_feature as reaction_feature


@asynccontextmanager
async def embed(app):
    dispatcher = Dispatcher(disable_fsm=True)
    fsm = TopicFSMContextMiddleware(MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(InvocationMiddleware())
    dispatcher.update.outer_middleware(StateContextMiddleware())
    dispatcher.update.outer_middleware(fsm)
    router = app.build_router()
    for observer in (router.message, router.callback_query):
        observer.middleware(HubIsolationBridge())
        observer.middleware(SelectiveIsolationMiddleware())
    dispatcher.include_router(router)
    try:
        yield dispatcher
    finally:
        await fsm.close()


async def test_real_host_state_scopes_allow_same_user_refresh_coalescing(reaction_feature):
    rig = reaction_feature
    async with embed(rig.app) as dispatcher:
        rig.dispatcher = dispatcher
        ui = await open_card(rig)
        started, release = asyncio.Event(), asyncio.Event()
        original = rig.repository.reaction_scoreboard.side_effect

        async def read(chat_id, *, days):
            started.set()
            await release.wait()
            return original(chat_id, days=days)

        rig.repository.reaction_scoreboard.reset_mock(side_effect=False)
        rig.repository.reaction_scoreboard.side_effect = read
        before = len(rig.bot.requests)
        first = asyncio.create_task(dispatcher.feed_update(rig.bot, click(rig, ui, actor=77)))
        await asyncio.wait_for(started.wait(), timeout=2)
        second = asyncio.create_task(dispatcher.feed_update(rig.bot, click(rig, ui, actor=77)))
        try:
            await asyncio.wait_for(second, timeout=2)
            assert not first.done()
            assert len([r for r in rig.bot.requests[before:] if isinstance(r, AnswerCallbackQuery)]) == 2
        finally:
            release.set()
            await first
        assert rig.repository.reaction_scoreboard.await_count == 1
        assert len([r for r in rig.bot.requests[before:] if isinstance(r, EditMessageText)]) == 1
