"""The migrated stdin flow preserves the application's text-only cancel grammar."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Update, User
from teleforge import App
from teleforge.testing import RecordingBot

from msu_hub_bot.features.compiler import Compiler
from tests.test_teleforge_features import message


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
