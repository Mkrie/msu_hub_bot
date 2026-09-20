import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message, PollAnswer, User
from pydantic import ValidationError

from msu_hub_bot.telegram.middlewares.settings import Settings, SettingsMiddleware


def message(chat_id=100):
    return Message(message_id=1, date=1_700_000_000, chat=Chat(id=chat_id, type="private", first_name="Friend"))


def callback():
    return CallbackQuery(
        id="callback", from_user=User(id=10, is_bot=False, first_name="Friend"), chat_instance="synthetic", message=message()
    )


@pytest.fixture
def storage():
    rows = {}

    async def load(chat):
        await asyncio.sleep(0)
        row = rows.setdefault(chat.chat_id, SimpleNamespace(metadata={}))
        return deepcopy(row.metadata.get("settings", {}))

    async def patch(chat_id, changes):
        rows[chat_id].metadata.setdefault("settings", {}).update(deepcopy(changes))
        return deepcopy(rows[chat_id].metadata["settings"])

    db = SimpleNamespace(load_settings=AsyncMock(side_effect=load), patch_settings=patch)
    return db, rows, db


async def test_first_contact_initializes_before_handler_and_saves(storage):
    db, rows, _ = storage

    async def handler(event, data):
        assert data["settings"].auto_speech_recognition
        data["settings"].auto_speech_recognition = False
        return "reply"

    assert await SettingsMiddleware(db)(handler, message(), {}) == "reply"
    assert rows[100].metadata["settings"]["auto_speech_recognition"] is False


async def test_concurrent_callbacks_share_preferences_and_preserve_extras(storage):
    db, rows, _ = storage
    rows[100] = SimpleNamespace(metadata={"other": 7, "settings": {"auto_video_links": False, "future_option": {"enabled": True}}})
    middleware = SettingsMiddleware(db)
    first_seen, both_seen = asyncio.Event(), asyncio.Event()
    objects = []

    async def handler(event, data):
        objects.append(data["settings"])
        if len(objects) == 1:
            first_seen.set()
            await both_seen.wait()
        else:
            both_seen.set()
        data["settings"].auto_speech_recognition = False

    first = asyncio.create_task(middleware(handler, callback(), {}))
    await first_seen.wait()
    await middleware(handler, callback(), {})
    await first
    assert objects[0] is objects[1]
    assert not objects[0].auto_video_links
    assert rows[100].metadata["other"] == 7
    assert rows[100].metadata["settings"]["future_option"] == {"enabled": True}
    db.load_settings.assert_awaited_once()


async def test_poll_answer_has_no_previous_chat_preferences(storage):
    db, _, _ = storage
    seen = []

    async def handler(event, data):
        seen.append(dict(data))

    answer = PollAnswer(
        poll_id="poll", option_ids=[0], option_persistent_ids=["option"], user=User(id=10, is_bot=False, first_name="Friend")
    )
    await SettingsMiddleware(db)(handler, answer, {"settings": Settings()})
    assert seen == [{}]
    db.load_settings.assert_not_awaited()


async def test_pending_write_keeps_new_mutation_dirty_until_second_save(storage):
    db, rows, query = storage
    middleware = SettingsMiddleware(db)
    preferences = await middleware.proxy(message().chat)
    preferences.auto_speech_recognition = False
    started, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def update(chat_id, changes):
        snapshot = deepcopy(changes)
        writes.append(snapshot)
        if len(writes) == 1:
            started.set()
            await release.wait()
        rows[chat_id].metadata.setdefault("settings", {}).update(snapshot)
        return deepcopy(rows[chat_id].metadata["settings"])

    query.patch_settings = update
    first = asyncio.create_task(preferences.save(db))
    await started.wait()
    preferences.auto_video_links = False
    second = asyncio.create_task(preferences.save(db))
    await asyncio.sleep(0)
    assert len(writes) == 1
    release.set()
    await asyncio.gather(first, second)
    assert len(writes) == 2
    assert writes[0] == {"auto_speech_recognition": False}
    assert writes[1] == {"auto_video_links": False}
    assert rows[100].metadata["settings"]["auto_video_links"] is False
    assert rows[100].metadata["settings"]["auto_speech_recognition"] is False
    assert not preferences._is_dirty


async def test_preference_save_patches_only_changed_keys_after_an_external_update(storage):
    db, rows, _ = storage
    rows[100] = SimpleNamespace(metadata={"other": 7, "settings": {"auto_video_links": True}})
    preferences = await SettingsMiddleware(db).proxy(message().chat)
    rows[100].metadata["settings"].update({"auto_video_links": False, "future_option": [1, 2]})
    preferences.with_nsfw = True
    await preferences.save(db)
    assert rows[100].metadata == {"other": 7, "settings": {"auto_video_links": False, "future_option": [1, 2], "with_nsfw": True}}


async def test_external_change_refreshes_shared_object_and_preserves_dirty_keys(storage):
    db, rows, _ = storage
    middleware = SettingsMiddleware(db)
    preferences = await middleware.proxy(message().chat)
    preferences.with_nsfw = True
    preferences.future_option = None
    rows[100].metadata["settings"] = {"auto_x_previews": False, "new_remote_option": [1, 2]}

    await middleware.invalidate(100)
    refreshed = await middleware.proxy(message().chat)

    assert refreshed is preferences
    assert refreshed.auto_x_previews is False
    assert refreshed.with_nsfw is True
    assert refreshed.model_dump()["future_option"] is None
    assert refreshed.model_dump()["new_remote_option"] == [1, 2]
    assert refreshed._is_dirty
    await refreshed.save(db)
    assert rows[100].metadata["settings"] == {
        "auto_x_previews": False,
        "new_remote_option": [1, 2],
        "with_nsfw": True,
        "future_option": None,
    }
    assert not refreshed._is_dirty
    assert await middleware.proxy(message().chat) is preferences
    assert db.load_settings.await_count == 2


@pytest.mark.parametrize("cold_failure", [False, True])
async def test_invalidation_during_first_load_does_not_leave_a_stale_cache(storage, cold_failure):
    db, rows, _ = storage
    middleware = SettingsMiddleware(db)
    started, release = asyncio.Event(), asyncio.Event()
    original = db.load_settings.side_effect

    async def load(chat):
        snapshot = await original(chat)
        if db.load_settings.await_count == 1:
            started.set()
            await release.wait()
            if cold_failure:
                raise RuntimeError("Unavailable")
        return snapshot

    db.load_settings.side_effect = load
    initial = asyncio.create_task(middleware.proxy(message().chat))
    await started.wait()
    rows[100].metadata["settings"] = {"auto_x_previews": False}
    await middleware.invalidate(100)
    assert not initial.done()
    release.set()
    if cold_failure:
        with pytest.raises(RuntimeError, match="Unavailable"):
            await initial
        assert not middleware._stale
        preferences = await middleware.proxy(message().chat)
    else:
        preferences = await initial
        assert await middleware.proxy(message().chat) is preferences
    assert preferences.auto_x_previews is False
    assert db.load_settings.await_count == 2


async def test_invalidation_and_local_edits_during_refresh_survive_the_reload(storage):
    db, rows, _ = storage
    middleware = SettingsMiddleware(db)
    preferences = await middleware.proxy(message().chat)
    rows[100].metadata["settings"] = {"auto_x_previews": False}
    await middleware.invalidate(100)
    started, release = asyncio.Event(), asyncio.Event()
    original = db.load_settings.side_effect

    async def load(chat):
        snapshot = await original(chat)
        started.set()
        await release.wait()
        return snapshot

    db.load_settings.side_effect = load
    first = asyncio.create_task(middleware.proxy(message().chat))
    await started.wait()
    preferences.auto_speech_recognition = False
    rows[100].metadata["settings"]["auto_video_links"] = False
    await middleware.invalidate(100)
    second = asyncio.create_task(middleware.proxy(message().chat))
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    assert await first is preferences
    assert await second is preferences
    assert not preferences.auto_x_previews
    assert not preferences.auto_video_links
    assert not preferences.auto_speech_recognition
    assert preferences._is_dirty
    assert db.load_settings.await_count == 3
    await preferences.save(db)
    assert rows[100].metadata["settings"] == {
        "auto_x_previews": False,
        "auto_video_links": False,
        "auto_speech_recognition": False,
    }


async def test_refresh_waits_for_pending_save_and_preserves_newer_edits(storage):
    db, rows, _ = storage
    middleware = SettingsMiddleware(db)
    preferences = await middleware.proxy(message().chat)
    preferences.with_nsfw = True
    started, release = asyncio.Event(), asyncio.Event()
    original = db.patch_settings

    async def patch(chat_id, changes):
        started.set()
        await release.wait()
        return await original(chat_id, changes)

    db.patch_settings = patch
    save = asyncio.create_task(preferences.save(db))
    await started.wait()
    preferences.auto_speech_recognition = False
    rows[100].metadata["settings"] = {"auto_x_previews": False}
    await middleware.invalidate(100)
    refresh = asyncio.create_task(middleware.proxy(message().chat))
    await asyncio.sleep(0)
    assert db.load_settings.await_count == 1
    release.set()
    await save
    assert await refresh is preferences
    assert not preferences.auto_x_previews
    assert not preferences.auto_speech_recognition
    assert preferences.with_nsfw
    assert preferences._is_dirty
    await preferences.save(db)
    assert rows[100].metadata["settings"] == {
        "auto_x_previews": False,
        "with_nsfw": True,
        "auto_speech_recognition": False,
    }


async def test_failed_refresh_remains_stale_and_does_not_discard_local_edits(storage):
    db, rows, _ = storage
    middleware = SettingsMiddleware(db)
    preferences = await middleware.proxy(message().chat)
    preferences.with_nsfw = True
    rows[100].metadata["settings"] = {"auto_x_previews": False}
    await middleware.invalidate(100)
    original = db.load_settings.side_effect
    db.load_settings.side_effect = RuntimeError("Unavailable")
    with pytest.raises(RuntimeError, match="Unavailable"):
        await middleware.proxy(message().chat)
    assert preferences.auto_x_previews and preferences.with_nsfw
    db.load_settings.side_effect = original
    assert await middleware.proxy(message().chat) is preferences
    assert not preferences.auto_x_previews and preferences.with_nsfw


async def test_refresh_cannot_be_evicted_and_unknown_invalidations_do_not_accumulate(storage):
    db, _, _ = storage
    middleware = SettingsMiddleware(db, cache_size=1)
    first = await middleware.proxy(message(100).chat)
    await middleware.proxy(message(200).chat)
    await middleware.invalidate(100)
    started, release = asyncio.Event(), asyncio.Event()
    original = db.load_settings.side_effect

    async def load(chat):
        started.set()
        await release.wait()
        return await original(chat)

    db.load_settings.side_effect = load
    refresh = asyncio.create_task(middleware.proxy(message(100).chat))
    await started.wait()
    for chat_id in range(300, 400):
        await middleware.invalidate(chat_id)
    await middleware.invalidate(200)
    middleware._trim()
    assert middleware.proxies[100] is first
    assert not middleware._stale
    release.set()
    assert await refresh is first


async def test_cache_pressure_cannot_replace_an_active_preference_object(storage):
    db, _, _ = storage
    middleware = SettingsMiddleware(db, cache_size=1)
    started, finish = asyncio.Event(), asyncio.Event()
    held = []

    async def slow(event, data):
        held.append(data["settings"])
        started.set()
        await finish.wait()

    async def quick(event, data):
        return data["settings"]

    task = asyncio.create_task(middleware(slow, message(100), {}))
    await started.wait()
    await middleware(quick, message(200), {})
    same = await middleware(quick, message(100), {})
    assert same is held[0]
    finish.set()
    await task
    assert len(middleware.proxies) <= 1


async def test_handler_failure_preserves_dirty_settings_and_original_error(storage):
    db, _, query = storage
    middleware = SettingsMiddleware(db)
    original = ValueError("handler failure")
    query.patch_settings = AsyncMock(side_effect=RuntimeError("save failure"))

    async def handler(event, data):
        data["settings"].with_nsfw = True
        raise original

    with pytest.raises(ValueError) as caught:
        await middleware(handler, message(), {})
    assert caught.value is original
    assert middleware.proxies[100]._is_dirty
    assert caught.value.__notes__ == ["Chat preferences also failed to save during cleanup"]


async def test_cancellation_still_saves_preferences(storage):
    db, rows, _ = storage

    async def handler(event, data):
        data["settings"].with_nsfw = True
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await SettingsMiddleware(db)(handler, message(), {})
    assert rows[100].metadata["settings"]["with_nsfw"] is True


async def test_shutdown_attempts_other_dirty_chats_after_one_save_fails(storage):
    db, rows, query = storage
    middleware = SettingsMiddleware(db)
    first = await middleware.proxy(message(100).chat)
    second = await middleware.proxy(message(200).chat)
    first.with_nsfw = second.with_nsfw = True
    original = RuntimeError("one chat save failed")
    update = query.patch_settings

    async def save(chat_id, changes):
        if chat_id == 100:
            raise original
        return await update(chat_id, changes)

    query.patch_settings = save
    with pytest.raises(ExceptionGroup) as caught:
        await middleware.close()
    assert caught.value.exceptions == (original,)
    assert first._is_dirty
    assert rows[200].metadata["settings"]["with_nsfw"] is True
    assert not second._is_dirty


def test_preferences_do_not_read_environment_and_validate_assignment(monkeypatch):
    monkeypatch.setenv("AUTO_VIDEO_LINKS", "false")
    preferences = Settings(future_option={"value": 1})
    assert preferences.auto_video_links is True
    with pytest.raises(ValidationError):
        preferences.with_nsfw = "private-invalid-canary"
    assert preferences.with_nsfw is False
    assert preferences.model_dump()["future_option"] == {"value": 1}
    assert not any(key.startswith("_") for key in preferences.model_dump())
