"""Real bot services exercised through TeleForge's native update dispatch."""

import io
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from teleforge import App
from teleforge.testing import RecordingBot

from msu_hub_bot.features import captions
from msu_hub_bot.features import roll
from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.features.compiler import Compiler
from msu_hub_bot.features.roll import Roll


def message(bot, *, text=None, message_id=1, actor=7, topic=55, **kwargs):
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=-100123, type="supergroup"),
        from_user=User(id=actor, is_bot=actor == bot.id, first_name="Tester"),
        text=text,
        message_thread_id=topic,
        is_topic_message=topic is not None,
        **kwargs,
    ).as_(bot)


@pytest.mark.parametrize(
    "text,length",
    [
        ("/roll", 3),
        ("/roll xyz", 3),
        ("/roll -6", 3),
        ("/roll +6", 3),
        ("/roll 6", 6),
        ("#ролл_7", 7),
        ("/roll 101", 100),
        ("/roll ١٢", 12),
    ],
)
async def test_roll_grammar_and_typed_default(text, length):
    bot = RecordingBot()
    app = App().include(Roll())
    try:
        await app.feed_update(bot, Update(update_id=1, message=message(bot, text=text)))
        sent = bot.requests[-1]
        assert len(sent.text.split()[0]) == length
        assert sent.entities[0].type == "code"
        assert sent.message_thread_id == 55
        assert sent.reply_parameters.message_id == 1
    finally:
        await app.aclose()


@pytest.mark.parametrize("index,emoji", [(0, "🎲"), (1, "🎯"), (2, "🏀"), (3, "⚽"), (4, "🎰")])
async def test_dice_retains_all_native_variants(monkeypatch, index, emoji):
    monkeypatch.setattr(roll.random, "choice", lambda values: values[index])
    bot = RecordingBot()
    app = App().include(Roll())
    try:
        await app.feed_update(bot, Update(update_id=1, message=message(bot, text="/dice")))
        sent = bot.requests[-1]
        assert sent.__api_method__ == "sendDice" and sent.emoji == emoji
        assert sent.reply_parameters.message_id == 1 and sent.message_thread_id == 55
    finally:
        await app.aclose()


@pytest.mark.parametrize("kind", ["photo", "video", "video_sticker"])
async def test_caption_sources_delivery_and_resource_ownership(monkeypatch, kind):
    bot = RecordingBot()
    base = {"file_id": "original", "file_unique_id": "file", "width": 24, "height": 16}
    if kind == "photo":
        fields = {"photo": [base]}
        output = Image.new("RGB", (24, 16), "red")
    elif kind == "video":
        fields = {"video": {**base, "duration": 1}}
        output = io.BytesIO(b"rendered video")
    else:
        fields = {"sticker": {**base, "is_video": True, "is_animated": False, "type": "regular"}}
        output = io.BytesIO(b"rendered video")
    renderer = AsyncMock(return_value=(output, False))
    monkeypatch.setattr(captions, "run_downloaded", renderer)

    @asynccontextmanager
    async def action(*args):
        yield

    monkeypatch.setattr(captions, "ChatActioner", action)
    source = message(bot, message_id=8, **fields)
    executor = TPExecutor(1)
    app = App(data={"cpu_executor": executor}).include(captions.Captions())
    try:
        await app.feed_update(bot, Update(update_id=1, message=message(bot, text="/meme длинная подпись", reply_to_message=source)))
        call = renderer.call_args.args
        assert call[1].file_id == "original" and call[3] == "длинная подпись"
        assert call[2] is (captions.caption_image if kind == "photo" else captions.caption_video)
        sent = bot.requests[-1]
        assert sent.__api_method__ == ("sendPhoto" if kind == "photo" else "sendVideo")
        assert sent.reply_parameters.message_id == 8 and sent.message_thread_id == 55
        assert bot.recording.uploads[-1]
        if kind == "photo":
            with pytest.raises(ValueError):
                output.getpixel((0, 0))
        else:
            assert output.closed
    finally:
        await app.aclose()
        executor.shutdown(wait=False)


async def test_compiler_draft_survives_new_feature_instance_and_isolates_actor_topic(monkeypatch):
    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock(return_value="hello")))
    storage = MemoryStorage()
    app = App().include(Compiler(provider))
    app.create_dispatcher(storage=storage)
    replacement = App().include(Compiler(provider))
    replacement.create_dispatcher(storage=storage)
    try:
        await app.feed_update(bot, Update(update_id=1, message=message(bot, text="#py_stdin print(input())")))
        preview = bot.requests[-1]
        ui = message(bot, message_id=101, text=preview.text, entities=preview.entities, actor=bot.id)
        button = preview.reply_markup.inline_keyboard[0][0]
        click = CallbackQuery(
            id="q", from_user=User(id=7, is_bot=False, first_name="Actor"), chat_instance="c", message=ui, data=button.callback_data
        )
        await app.feed_update(bot, Update(update_id=2, callback_query=click))
        assert any(request.__api_method__ == "answerCallbackQuery" for request in bot.requests)
        for actor, topic in [(8, 55), (7, 56)]:
            await replacement.feed_update(bot, Update(update_id=3, message=message(bot, text="wrong input", actor=actor, topic=topic)))
        provider.instance.request_and_parse.assert_not_awaited()
        await replacement.feed_update(bot, Update(update_id=4, message=message(bot, text="hello")))
        provider.instance.request_and_parse.assert_awaited_once_with("print(input())", "hello", "python3")
        assert bot.requests[-1].__api_method__ == "editMessageText"
        assert "hello" in bot.requests[-1].text
        await replacement.feed_update(bot, Update(update_id=5, message=message(bot, text="again")))
        assert provider.instance.request_and_parse.await_count == 1
    finally:
        await app.aclose()
        await replacement.aclose()


async def test_compiler_cancel_does_not_execute_provider():
    bot = RecordingBot()
    provider = SimpleNamespace(instance=SimpleNamespace(request_and_parse=AsyncMock()))
    app = App().include(Compiler(provider))
    try:
        await app.feed_update(bot, Update(update_id=1, message=message(bot, text="#py_stdin print(input())")))
        preview = bot.requests[-1]
        ui = message(bot, message_id=101, text=preview.text, entities=preview.entities, actor=bot.id)
        click = CallbackQuery(
            id="q", from_user=User(id=7, is_bot=False, first_name="Actor"), chat_instance="c", message=ui, data="prog:input"
        )
        await app.feed_update(bot, Update(update_id=2, callback_query=click))
        await app.feed_update(bot, Update(update_id=3, message=message(bot, text="/cancel")))
        assert bot.requests[-1].text == "🆗 Ввод отменён"
        await app.feed_update(bot, Update(update_id=4, message=message(bot, text="hello")))
        provider.instance.request_and_parse.assert_not_awaited()
    finally:
        await app.aclose()
