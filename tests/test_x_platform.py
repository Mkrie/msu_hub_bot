"""Native X previews have one owner and respect chat and conversation policies."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher, Router
from aiogram.dispatcher.event.bases import SkipHandler, UNHANDLED
from aiogram.filters import Command
from aiogram.types import Update

from msu_hub_bot.providers.exceptions import BadRequestError
from msu_hub_bot.providers.fxembed import FxPost
from msu_hub_bot.providers.vk.api import VkApi
from msu_hub_bot.telegram.filters import MetaCommand
from msu_hub_bot.telegram.middlewares import viewer as module
from msu_hub_bot.telegram.middlewares.settings import Settings
from msu_hub_bot.telegram.middlewares.telemetry import DispatchTelemetryMiddleware
from msu_hub_bot.telegram.middlewares.viewer import ViewerMiddleware, preview_policy
from msu_hub_bot.telegram.wrapper import BotWrapper
from msu_hub_bot.telemetry import Telemetry
from telegram_helpers import RecordingSession, make_message
from telemetry_helpers import Capture, config


@pytest.fixture
async def runtime(monkeypatch):
    session = RecordingSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    api = VkApi("synthetic-token")
    executor = SimpleNamespace(run=AsyncMock(return_value=(None, False)))
    viewer = ViewerMiddleware(bot, api, executor)
    viewer.fxembed.get_post = AsyncMock(
        return_value=FxPost.model_validate(
            {"id": "123", "text": "SYNTHETIC_PRIVATE_TEXT", "author": {"name": "Author", "screen_name": "example"}}
        )
    )
    publish = AsyncMock()
    monkeypatch.setattr(module, "publish_x_post", publish)
    yield SimpleNamespace(bot=bot, api=api, viewer=viewer, publish=publish, executor=executor, session=session)
    await api.close()
    await session.close()


def source(runtime, url="https://x.com/example/status/123", *, prefix="", **fields):
    return make_message(
        runtime.bot,
        text=prefix + url,
        entities=[{"type": "url", "offset": len(prefix), "length": len(url)}],
        **fields,
    )


@pytest.mark.parametrize(
    "url", ["https://x.com/example/status/123", "twitter.com/example/status/123/video/1", "https://mobile.twitter.com/i/web/status/123"]
)
@pytest.mark.parametrize("topic", [False, True])
async def test_native_preview_preserves_source_reply_topic_and_trigger(runtime, url, topic):
    message = source(runtime, url, message_id=501, message_thread_id=99, is_topic_message=topic)
    await runtime.viewer.view(message, Settings())
    link = runtime.viewer.fxembed.get_post.call_args.args[0]
    assert link.id == "123"
    runtime.publish.assert_awaited_once_with(
        runtime.viewer.fxembed.get_post.return_value,
        runtime.bot,
        message.chat.id,
        501,
        message_thread_id=99 if topic else None,
        link=link,
    )
    runtime.executor.run.assert_not_awaited()
    assert not runtime.session.methods


@pytest.mark.parametrize(
    "url",
    [
        "fixupx.com/c_valenzuelab/status/2101124472661069980/video/1",
        "https://fxtwitter.com/example/status/123",
        "https://i.fixupx.com/example/status/123/photo/1",
        "https://d.fxtwitter.com/example/status/123/video/2",
        "https://xfixup.com/example/status/123",
        "https://twittpr.com/example/status/123",
        "https://x.com/example",
        "https://twitter.com/search?q=cat",
        "https://x.com/i/spaces/123",
        "https://fixupx.com/example/status/123/ru",
        "https://x.com/example/status/123/video/0",
    ],
)
async def test_fixed_previews_and_unsupported_x_routes_never_trigger_ydl(runtime, url):
    await runtime.viewer.view(source(runtime, url), Settings())
    runtime.viewer.fxembed.get_post.assert_not_awaited()
    runtime.publish.assert_not_awaited()
    runtime.executor.run.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/x.com/example/status/123",
        "https://notx.com/example/status/123",
        "https://fixupx.com.example.org/example/status/123",
    ],
)
async def test_domain_ownership_does_not_capture_unrelated_sites(runtime, url):
    await runtime.viewer.view(source(runtime, url), Settings())
    runtime.viewer.fxembed.get_post.assert_not_awaited()
    runtime.executor.run.assert_awaited_once()


async def test_link_labels_work_and_tracking_duplicates_do_not_consume_post_limit(runtime):
    urls = [
        "https://x.com/example/status/123?s=20",
        "https://twitter.com/another/status/123",
        "https://x.com/second/status/124",
        "https://x.com/third/status/125",
    ]
    message = make_message(
        runtime.bot,
        text="abcd",
        entities=[{"type": "text_link", "offset": index, "length": 1, "url": url} for index, url in enumerate(urls)],
    )
    await runtime.viewer.view(message, Settings())
    assert [call.args[0].id for call in runtime.viewer.fxembed.get_post.call_args_list] == ["123", "124"]
    assert runtime.publish.await_count == 2
    runtime.executor.run.assert_not_awaited()


@pytest.mark.parametrize(
    "values,expected",
    [
        ({"auto_video_links": False}, False),
        ({"auto_video_links": False, "auto_x_previews": True}, True),
        ({"auto_video_links": True, "auto_x_previews": False}, False),
    ],
)
async def test_independent_x_preference_never_falls_through_to_ydl(runtime, values, expected):
    await runtime.viewer.view(source(runtime), Settings(**values))
    assert runtime.publish.await_count == int(expected)
    runtime.executor.run.assert_not_awaited()


@pytest.mark.parametrize(
    "fields,prefix,expected",
    [
        ({"link_preview_options": {"is_disabled": True}}, "", False),
        ({"link_preview_options": {"prefer_large_media": True}}, "", True),
        ({"is_automatic_forward": True}, "", False),
        ({"from_user": {"id": 123, "is_bot": True, "first_name": "Other bot"}}, "", False),
        ({}, "/ydl ", False),
    ],
)
async def test_message_preview_policy(runtime, fields, prefix, expected):
    await runtime.viewer.view(source(runtime, prefix=prefix, **fields), Settings())
    assert runtime.publish.await_count == int(expected)
    runtime.executor.run.assert_not_awaited()


async def test_unavailable_provider_is_quiet_and_retains_single_owner(runtime):
    runtime.viewer.fxembed.get_post.side_effect = BadRequestError()
    await runtime.viewer.view(source(runtime), Settings())
    runtime.publish.assert_not_awaited()
    runtime.executor.run.assert_not_awaited()
    assert not runtime.session.methods


async def test_ambiguous_delivery_failure_propagates_without_video_retry(runtime):
    runtime.publish.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await runtime.viewer.view(source(runtime), Settings())
    runtime.executor.run.assert_not_awaited()
    runtime.publish.assert_awaited_once()


def dispatcher(runtime):
    result = Dispatcher(disable_fsm=True)
    result.message.outer_middleware(runtime.viewer)
    result.message.middleware(preview_policy)
    return result


@pytest.mark.parametrize("prefix,filter_", [("#meme ", MetaCommand("meme")), ("/meme ", Command("meme"))])
async def test_selected_commands_do_not_also_preview_their_argument(runtime, prefix, filter_):
    dp = dispatcher(runtime)

    @dp.message(filter_)
    async def command(message):
        return True

    await dp.feed_update(runtime.bot, Update(update_id=1, message=source(runtime, prefix=prefix)), settings=Settings())
    runtime.publish.assert_not_awaited()
    runtime.executor.run.assert_not_awaited()


async def test_finishing_conversation_does_not_preview_its_input(runtime):
    dp = dispatcher(runtime)

    @dp.message()
    async def conversation(message, **data):
        data["raw_state"] = None
        return True

    await dp.feed_update(runtime.bot, Update(update_id=1, message=source(runtime)), settings=Settings(), raw_state="waiting:input")
    runtime.publish.assert_not_awaited()


@pytest.mark.parametrize("completion", ["handled", "unhandled", "skipped"])
async def test_only_completed_handler_can_disable_previews(runtime, completion):
    dp = dispatcher(runtime)
    child = Router()
    dp.include_router(child)

    @dp.message(flags={"automatic_previews": False})
    async def first(message):
        if completion == "skipped":
            raise SkipHandler()
        return UNHANDLED if completion == "unhandled" else True

    @child.message()
    async def second(message):
        return True

    await dp.feed_update(runtime.bot, Update(update_id=1, message=source(runtime)), settings=Settings())
    assert runtime.publish.await_count == (0 if completion == "handled" else 1)


async def test_concurrent_conversation_policy_is_per_update(runtime):
    dp = dispatcher(runtime)
    entered, release = asyncio.Event(), asyncio.Event()

    @dp.message()
    async def selected(message):
        if message.message_id == 501:
            entered.set()
            await release.wait()
        return True

    first = asyncio.create_task(
        dp.feed_update(
            runtime.bot, Update(update_id=1, message=source(runtime, message_id=501)), settings=Settings(), raw_state="waiting:input"
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await dp.feed_update(runtime.bot, Update(update_id=2, message=source(runtime, message_id=502)), settings=Settings())
    finally:
        release.set()
        await first
    runtime.publish.assert_awaited_once()
    assert runtime.publish.call_args.args[3] == 502


async def test_provider_telemetry_has_ids_and_outcomes_without_source_content(runtime, monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    capture = Capture()
    telemetry = Telemetry(config(), transport=capture)
    runtime.viewer.telemetry = telemetry
    dp = dispatcher(runtime)
    dp.update.outer_middleware(DispatchTelemetryMiddleware(telemetry))
    runtime.viewer.fxembed.get_post.side_effect = BadRequestError()
    await telemetry.start()
    try:
        await dp.feed_update(runtime.bot, Update(update_id=17, message=source(runtime)), settings=Settings())
    finally:
        await telemetry.close()
    spans = [span for span in capture.spans() if span.name == "provider.request"]
    assert len(spans) == 1
    attributes = {item.key: item.value for item in spans[0].attributes}
    assert attributes["provider"].string_value == "fxembed"
    assert attributes["operation"].string_value == "fxembed.fetch"
    assert attributes["telegram.user_id"].int_value == 42
    assert attributes["telegram.update_id"].int_value == 17
    serialized = capture.serialized()
    assert "SYNTHETIC_PRIVATE_TEXT" not in serialized
    assert "https://x.com" not in serialized
