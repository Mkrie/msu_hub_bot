"""VK previews share source checks, copied text and automatic-handler ownership."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher, Router
from aiogram.dispatcher.event.bases import SkipHandler, UNHANDLED
from aiogram.methods import SendMessage
from aiogram.types import Update

from msu_hub_bot.community.reposts import Reposts, SourcePreview
from msu_hub_bot.providers.vk.api import VkApi
from msu_hub_bot.storage.features import FeatureStore
from msu_hub_bot.telegram.middlewares.settings import Settings
from msu_hub_bot.telegram.middlewares.viewer import ViewerMiddleware, preview_policy
from msu_hub_bot.telegram.wrapper import BotWrapper
from quiz_helpers import FeatureFixture
from telegram_helpers import RecordingSession, make_message
from test_dispatch_contract import router


@pytest.fixture
async def runtime():
    session = RecordingSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    api = VkApi("synthetic-token")
    executor = SimpleNamespace(run=AsyncMock(return_value=(None, False)))
    yield bot, api, executor, session
    await api.close()
    await session.close()


@pytest.mark.parametrize("domain", ["com", "ru"])
@pytest.mark.parametrize("available", [False, True])
async def test_recognized_wall_link_never_also_uses_video_extractor(runtime, domain, available):
    bot, api, executor, session = runtime
    api.request = AsyncMock(
        side_effect=[
            {"groups": [{"id": 10, "is_closed": 0}]},
            {"items": [{"id": 6, "owner_id": -10, "text": "Public post"}] if available else []},
        ]
    )
    url = f"https://vk.{domain}/wall-10_6"
    message = make_message(bot, text=url, entities=[{"type": "url", "offset": 0, "length": len(url)}])
    await ViewerMiddleware(bot, api, executor).view(message, Settings(auto_video_links=True))
    executor.run.assert_not_awaited()
    assert len(session.methods) == int(available)


async def test_vk_video_link_keeps_generic_video_extraction(runtime):
    bot, api, executor, session = runtime
    api.request = AsyncMock()
    url = "https://vk.ru/video-10_6"
    message = make_message(bot, text=url, entities=[{"type": "url", "offset": 0, "length": len(url)}])
    await ViewerMiddleware(bot, api, executor).view(message, Settings(auto_video_links=True))
    api.request.assert_not_awaited()
    executor.run.assert_awaited_once()
    assert not session.methods


async def test_manual_post_only_publishes_to_the_explicit_destination(runtime):
    bot, api, executor, session = runtime

    async def request(method, **params):
        if method == "groups.getById":
            return {"groups": [{"id": 10, "is_closed": 0}]}
        return {"items": [{"id": 6, "owner_id": -10, "text": "Public post"}]}

    api.request = AsyncMock(side_effect=request)
    dispatcher = Dispatcher()
    dispatcher.include_router(router())
    dispatcher.message.outer_middleware(ViewerMiddleware(bot, api, executor))
    dispatcher.message.middleware(preview_policy)
    url = "https://vk.ru/wall-10_6"
    message = make_message(
        bot,
        text=f"/vk_post {url} -20 0",
        entities=[{"type": "bot_command", "offset": 0, "length": 8}, {"type": "url", "offset": 9, "length": len(url)}],
        from_user={"id": 7, "is_bot": False, "first_name": "Owner"},
    )
    await dispatcher.feed_update(bot, Update(update_id=1, message=message), vk_api=api, settings=Settings(auto_video_links=True))
    sent = [method for method in session.methods if isinstance(method, SendMessage)]
    assert len(sent) == 1 and sent[0].chat_id == -20
    assert api.request.await_count == 2
    executor.run.assert_not_awaited()


@pytest.mark.parametrize("completion", ["handled", "unhandled", "skipped", "failed"])
async def test_nested_handler_preview_policy_applies_only_after_terminal_selection(runtime, completion):
    bot, api, executor, session = runtime
    api.request = AsyncMock(
        side_effect=[{"groups": [{"id": 10, "is_closed": 0}]}, {"items": [{"id": 6, "owner_id": -10, "text": "Public post"}]}]
    )
    dispatcher = Dispatcher()
    dispatcher.message.outer_middleware(ViewerMiddleware(bot, api, executor))
    dispatcher.message.middleware(preview_policy)
    parent, child = Router(), Router()
    dispatcher.include_router(parent)
    parent.include_router(child)

    @parent.message(flags={"automatic_previews": False})
    async def selected(message):
        if completion == "skipped":
            raise SkipHandler()
        if completion == "failed":
            raise RuntimeError("Synthetic handler failure")
        return UNHANDLED if completion == "unhandled" else True

    @child.message()
    async def ordinary(message):
        return True

    url = "https://vk.ru/wall-10_6"
    message = make_message(bot, text=url, entities=[{"type": "url", "offset": 0, "length": len(url)}])
    if completion == "failed":
        with pytest.raises(RuntimeError, match="Synthetic handler failure"):
            await dispatcher.feed_update(bot, Update(update_id=1, message=message), settings=Settings(auto_video_links=True))
    else:
        await dispatcher.feed_update(bot, Update(update_id=1, message=message), settings=Settings(auto_video_links=True))
    assert len(session.methods) == (0 if completion in {"handled", "failed"} else 1)
    executor.run.assert_not_awaited()


async def test_preview_suppression_is_isolated_between_concurrent_updates(runtime):
    bot, api, executor, session = runtime
    api.request = AsyncMock(
        side_effect=[{"groups": [{"id": 10, "is_closed": 0}]}, {"items": [{"id": 6, "owner_id": -10, "text": "Public post"}]}]
    )
    dispatcher = Dispatcher()
    dispatcher.message.outer_middleware(ViewerMiddleware(bot, api, executor))
    dispatcher.message.middleware(preview_policy)
    entered, release = asyncio.Event(), asyncio.Event()

    @dispatcher.message(lambda message: message.message_id == 501, flags={"automatic_previews": False})
    async def selected(message):
        entered.set()
        await release.wait()
        return True

    @dispatcher.message()
    async def ordinary(message):
        return True

    url = "https://vk.ru/wall-10_6"
    fields = {"text": url, "entities": [{"type": "url", "offset": 0, "length": len(url)}]}
    first = asyncio.create_task(
        dispatcher.feed_update(
            bot, Update(update_id=1, message=make_message(bot, message_id=501, **fields)), settings=Settings(auto_video_links=True)
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await dispatcher.feed_update(
            bot, Update(update_id=2, message=make_message(bot, message_id=502, **fields)), settings=Settings(auto_video_links=True)
        )
    finally:
        release.set()
        await first
    assert len(session.methods) == 1 and session.methods[0].reply_parameters.message_id == 502
    executor.run.assert_not_awaited()


def preview_service(posts, *, source=None):
    api = VkApi("synthetic-token")

    async def request(method, **params):
        if method == "groups.getById":
            owner = int(params["group_ids"])
            return {"groups": [source(owner) if source else {"id": owner, "is_closed": 0}]}
        assert method == "wall.get"
        assert params == {"owner_id": -10, "count": 3, "filter": "all", "extended": 1}
        return {"items": posts}

    api.request = AsyncMock(side_effect=request)
    return Reposts(FeatureStore(FeatureFixture()), api)


@pytest.mark.parametrize("outer", ["", "Source introduction"])
async def test_preview_and_filters_include_the_retained_copied_bodies(outer):
    copied = {"id": 7, "owner_id": -11, "text": "Lecture tonight"}
    nested = {"id": 8, "owner_id": -12, "text": "Registration required"}
    copied["copy_history"] = [nested]
    service = preview_service([{"id": 6, "owner_id": -10, "text": outer, "copy_history": [copied]}])
    included = await service.preview(SourcePreview(source="-10", with_reposts=True, include_keywords=["REGISTRATION"]))
    [post] = included["posts"]
    assert post["selected"] and post["is_repost"]
    assert post["text"] == "\n\n".join(filter(None, [outer, copied["text"], nested["text"]]))
    excluded = await service.preview(SourcePreview(source="-10", with_reposts=True, exclude_keywords=["LECTURE"]))
    assert not excluded["posts"][0]["selected"]
    own_only = await service.preview(SourcePreview(source="-10"))
    assert not own_only["posts"][0]["selected"]


async def test_preview_filters_full_body_before_bounding_plain_text_excerpt():
    text = "<b>Literal user text</b> " + "A" * 4100 + " registration"
    service = preview_service([{"id": 6, "owner_id": -10, "text": text}])
    result = await service.preview(SourcePreview(source="-10", include_keywords=["registration"]))
    [post] = result["posts"]
    assert post["selected"]
    assert post["text"] == text[:4000]
    assert '<a href="' not in post["text"]


@pytest.mark.parametrize("metadata", [{"is_closed": 1}, {}, {"is_closed": 0, "deactivated": "deleted"}])
async def test_preview_never_exposes_text_from_a_nonpublic_copied_wall(metadata):
    service = preview_service(
        [{"id": 6, "owner_id": -10, "copy_history": [{"id": 7, "owner_id": -11, "text": "Private copy canary"}]}],
        source=lambda owner: {"id": owner, "is_closed": 0} if owner == 10 else {"id": owner} | metadata,
    )
    result = await service.preview(SourcePreview(source="-10", with_reposts=True))
    assert not result["posts"] and "Private copy canary" not in str(result)


async def test_preview_rejects_wall_responses_larger_than_the_requested_sample():
    service = preview_service([{"id": index + 1, "owner_id": -10, "text": "Public post"} for index in range(4)])
    result = await service.preview(SourcePreview(source="-10"))
    assert not result["available"] and not result["posts"]
