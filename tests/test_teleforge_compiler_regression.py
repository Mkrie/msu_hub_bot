"""The migrated stdin flow preserves the application's text-only cancel grammar."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Update, User
from teleforge import App, Feature, command
from teleforge.testing import RecordingBot

from msu_hub_bot.features.compiler import Compiler
from msu_hub_bot.commands.prog import ProgCompiler
from tests.test_teleforge_features import message


@pytest.fixture(autouse=True)
def native_preview_cache():
    ProgCompiler.replies.clear()
    yield
    ProgCompiler.replies.clear()


@pytest.mark.parametrize(
    "incoming,stdin",
    [
        ({"text": "#cancel"}, "#cancel"),
        ({"text": "/cancel@otherbot"}, "/cancel@otherbot"),
        (
            {
                "caption": "/cancel",
                "photo": [{"file_id": "synthetic", "file_unique_id": "synthetic", "width": 16, "height": 16}],
            },
            "/cancel",
        ),
    ],
)
async def test_non_cancel_messages_remain_program_input(incoming, stdin):
    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock(return_value="done")))
    async with App(Compiler(provider)) as app:
        await app.feed_update(bot, Update(update_id=1, message=message(bot, text="#py_stdin print(input())")))
        preview = bot.requests[-1]
        ui = message(bot, message_id=101, text=preview.text, entities=preview.entities, actor=bot.id)
        click = CallbackQuery(
            id="q",
            from_user=User(id=7, is_bot=False, first_name="Actor"),
            chat_instance="c",
            message=ui,
            data="prog:input",
        )
        await app.feed_update(bot, Update(update_id=2, callback_query=click))
        await app.feed_update(bot, Update(update_id=3, message=message(bot, **incoming)))
    provider.instance.request_and_parse.assert_awaited_once_with("print(input())", stdin, "python3")


async def test_large_source_document_uses_filename_preview_and_loads_on_click(monkeypatch):
    source = "print(input())\n" * 400
    download = AsyncMock(return_value=source)
    monkeypatch.setattr("msu_hub_bot.commands.prog.download_text", download)
    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock(return_value="done")))
    original = message(
        bot,
        caption="/py_stdin explanation",
        document={
            "file_id": "source",
            "file_unique_id": "source",
            "file_name": "code.py",
            "mime_type": "text/x-python",
            "file_size": len(source),
        },
    )
    async with App(Compiler(provider)) as app:
        await app.feed_update(bot, Update(update_id=1, message=original))
        download.assert_not_awaited()
        preview = bot.requests[-1]
        assert isinstance(preview, SendMessage)
        assert preview.text.endswith("code.py") and len(preview.text) < 100
        assert preview.reply_parameters.message_id == original.message_id
        ui = message(bot, message_id=101, text=preview.text, entities=preview.entities, actor=bot.id, reply_to_message=original)
        click = CallbackQuery(id="q", from_user=original.from_user, chat_instance="c", message=ui, data="prog:input")
        await app.feed_update(bot, Update(update_id=2, callback_query=click))
        download.assert_awaited_once_with("source", bot)
        await app.feed_update(bot, Update(update_id=3, message=message(bot, text="hello", message_id=2)))
    provider.instance.request_and_parse.assert_awaited_once_with(source, "hello", "python3")


async def test_editing_source_refreshes_the_same_preview_and_runs_revised_code():
    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock(return_value="done")))
    async with App(Compiler(provider)) as app:
        await app.feed_update(bot, Update(update_id=1, message=message(bot, text="#py_stdin print(1)")))
        assert isinstance(bot.requests[-1], SendMessage)
        sent_id = bot.recording.next_message_id
        await app.feed_update(bot, Update(update_id=2, edited_message=message(bot, text="#py_stdin print(2)")))
        preview = bot.requests[-1]
        assert isinstance(preview, EditMessageText) and preview.message_id == sent_id
        assert "print(2)" in preview.text and "print(1)" not in preview.text
        ui = message(bot, message_id=sent_id, text=preview.text, entities=preview.entities, actor=bot.id)
        click = CallbackQuery(id="q", from_user=message(bot).from_user, chat_instance="c", message=ui, data="prog:input")
        await app.feed_update(bot, Update(update_id=3, callback_query=click))
        await app.feed_update(bot, Update(update_id=4, message=message(bot, text="hello", message_id=2)))
    provider.instance.request_and_parse.assert_awaited_once_with("print(2)", "hello", "python3")


async def test_program_execution_releases_the_finished_conversation():
    started, finish = asyncio.Event(), asyncio.Event()

    async def run(*args):
        started.set()
        await finish.wait()
        return "done"

    class Ping(Feature, key="ping"):
        @command("ping")
        async def ping(self) -> str:
            return "pong"

    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock(side_effect=run)))
    async with App(Compiler(provider), Ping()) as app:
        await app.feed_update(bot, Update(update_id=1, message=message(bot, text="#py_stdin print(input())")))
        preview = bot.requests[-1]
        ui = message(bot, message_id=101, text=preview.text, entities=preview.entities, actor=bot.id)
        click = CallbackQuery(id="q", from_user=message(bot).from_user, chat_instance="c", message=ui, data="prog:input")
        await app.feed_update(bot, Update(update_id=2, callback_query=click))
        execution = asyncio.create_task(app.feed_update(bot, Update(update_id=3, message=message(bot, text="hello", message_id=2))))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            await asyncio.wait_for(app.feed_update(bot, Update(update_id=4, message=message(bot, text="/ping", message_id=3))), timeout=2)
            assert bot.requests[-1].text == "pong"
            assert not execution.done()
        finally:
            finish.set()
            await execution


@pytest.mark.parametrize("already_deleted", [False, True])
async def test_callback_cancel_deletes_its_saved_prompt_only_in_the_owning_scope(already_deleted):
    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock(return_value="done")))

    async def respond(bot, method):
        if already_deleted and isinstance(method, DeleteMessage):
            return TelegramBadRequest(method=method, message="Bad Request: message to delete not found")
        return bot.recording._default(bot, method)

    bot.recording.responder = respond
    async with App(Compiler(provider)) as app:
        await app.feed_update(bot, Update(update_id=1, message=message(bot, text="#py_stdin print(input())")))
        preview = bot.requests[-1]
        ui = message(bot, message_id=101, text=preview.text, entities=preview.entities, actor=bot.id)

        async def click(action, *, actor=7, topic=55):
            query = CallbackQuery(
                id=f"{action}-{actor}-{topic}",
                from_user=User(id=actor, is_bot=False, first_name="Actor"),
                chat_instance="c",
                message=ui.model_copy(update={"message_thread_id": topic}),
                data=f"prog:{action}",
            )
            await app.feed_update(bot, Update(update_id=2, callback_query=query))

        await click("input")
        waiting_id = bot.recording.next_message_id
        assert bot.requests[-1].text == "Ожидаю ввод ⬇️, или /cancel"
        for actor, topic in ((9, 55), (7, 56)):
            start = len(bot.requests)
            await click("cancel", actor=actor, topic=topic)
            assert len(bot.requests[start:]) == 1
            assert isinstance(bot.requests[-1], AnswerCallbackQuery)
            assert bot.requests[-1].text == "💁🏻‍♂️ Вы не в процессе ввода"
        start = len(bot.requests)
        await click("cancel")
        deleted, answered = bot.requests[start:]
        assert isinstance(deleted, DeleteMessage)
        assert (deleted.chat_id, deleted.message_id) == (ui.chat.id, waiting_id)
        assert isinstance(answered, AnswerCallbackQuery)
        assert answered.text == "🆗 Ввод отменён"
        await app.feed_update(bot, Update(update_id=3, message=message(bot, text="late input", message_id=2)))
    provider.instance.request_and_parse.assert_not_awaited()
