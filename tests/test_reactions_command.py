"""Reaction rankings stay chat-local, bounded and navigable through real Telegram methods."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetChatMember, SendMessage
from aiogram.types import CallbackQuery, InaccessibleMessage
from aiogram.utils.formatting import Text
from cachetools import LRUCache, TTLCache
from pydantic import ValidationError

from msu_hub_bot.commands.reactions import PERIODS, TITLES, ReactionCallback, Reactions, render_scoreboard
from msu_hub_bot.storage.reactions import ReactionScoreboard
from telegram_helpers import RecordingSession, make_message

CHAT_ID = -1001234567890
MAX_COUNT = 2**63 - 1


def scoreboard(*, days=30, size=2, name="Друг", count=3, **totals):
    def ranks(start):
        return [
            dict(
                user_id=start + index, first_name=name, last_name=name, username="private_handle", score=count, people=count, messages=count
            )
            for index in range(size)
        ]

    return ReactionScoreboard.model_validate(
        dict(
            days=days,
            getters=ranks(100),
            givers=ranks(200),
            posts=[
                dict(message_id=400 + index, thread_id=17 if index % 2 else None, author_id=100 + index, score=count, people=count)
                for index in range(size)
            ],
            emoji=[dict(key="e:👍", count=count), dict(key="c:0007", count=count), dict(key="c:0008", count=count)],
            summary={
                "points": count,
                "reactions": count,
                "givers": count,
                "getters": count,
                "messages": count,
                "anonymous": 0,
                "paid": 0,
                "unattributed": 0,
                "channel_reactions": 0,
                **totals,
            },
        )
    )


def entity_text(text, entity):
    return text.encode("utf-16-le")[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")


def assert_valid_text(content):
    text, entities = content.render()
    assert 0 < len(Text(text)) <= 4096
    assert len(entities) <= 100
    for entity in entities:
        assert entity.length > 0 and entity.offset + entity.length <= len(Text(text))
        assert entity_text(text, entity)
    return text, entities


@pytest.mark.parametrize("view", TITLES)
@pytest.mark.parametrize("days", [1, 7, 30])
@pytest.mark.parametrize("name", ["Я" * 1000, "👨‍👩‍👧‍👦" * 300, '<b>Друг</b>\n& <a href="https://example.invalid">' * 100])
def test_all_views_and_periods_fit_telegram_with_ten_extreme_names_and_counts(view, days, name):
    board = scoreboard(
        days=days,
        size=10,
        name=name,
        count=MAX_COUNT,
        unattributed=MAX_COUNT,
        anonymous=MAX_COUNT,
        paid=MAX_COUNT,
        channel_reactions=MAX_COUNT,
    )
    message = make_message(chat=dict(id=CHAT_ID, type="supergroup", title=name))
    text, entities = assert_valid_text(render_scoreboard(board, message, view, administrator=None))
    assert TITLES[view] in text and PERIODS[days] in text
    assert "Автор не виден, либо это бот или чат" in text and "Не удалось проверить права" in text
    links = [entity for entity in entities if entity.type == "text_link"]
    if view in {"getters", "givers"}:
        assert len(links) == 10
        assert all(len(Text(entity_text(text, link))) <= 48 for link in links)
    assert all(not entity.url or entity.url.startswith(("tg://user?id=", "https://t.me/c/")) for entity in entities)


@pytest.mark.parametrize("view", TITLES)
def test_empty_views_explain_how_rankings_start(view):
    board = scoreboard(size=0, count=0)
    text, entities = assert_valid_text(render_scoreboard(board, make_message(), view))
    assert TITLES[view] in text
    assert not any(entity.url for entity in entities)


def test_names_remain_literal_and_profiles_have_only_intended_links():
    name = '<b>Друг</b>\n<a href="https://example.invalid"> &'
    content = render_scoreboard(scoreboard(size=1, name=name), make_message(), "getters")
    text, entities = assert_valid_text(content)
    links = [entity for entity in entities if entity.type == "text_link"]
    assert len(links) == 1 and links[0].url == "tg://user?id=100"
    assert "<b>Друг</b>" in entity_text(text, links[0])
    assert "\n" not in entity_text(text, links[0])
    assert "&lt;b&gt;Друг&lt;/b&gt;" in content.as_html()
    assert "private_handle" not in text


def test_anonymous_paid_and_channel_counts_do_not_enter_personal_leaderboards():
    board = scoreboard(anonymous=17, paid=700, channel_reactions=23, unattributed=5)
    for view in ("getters", "givers"):
        text, _ = render_scoreboard(board, make_message(), view).render()
        assert "700" not in text and "Платные реакции" not in text and "Анонимные реакции" not in text
        assert "Без личного получателя: 5 баллов" in text
    pulse, _ = render_scoreboard(board, make_message(), "pulse").render()
    assert "Анонимные реакции: 17" in pulse and "Платные реакции: 700" in pulse and "От имени чатов: 23" in pulse
    assert "отдельно от личного рейтинга" in pulse
    assert "Свои эмодзи" in pulse and "0007" not in pulse and "0008" not in pulse


@pytest.mark.parametrize("chat", [dict(id=CHAT_ID, type="supergroup"), dict(id=CHAT_ID, type="supergroup", username="public_chat")])
def test_post_links_use_current_chat_and_original_forum_topics(chat):
    message = make_message(chat=chat, message_thread_id=999, is_topic_message=True)
    _, entities = assert_valid_text(render_scoreboard(scoreboard(), message, "posts"))
    links = [entity.url for entity in entities if entity.type == "text_link"]
    assert links == ["https://t.me/c/1234567890/400", "https://t.me/c/1234567890/17/401"]
    assert all("999" not in link for link in links)


def test_basic_group_posts_have_no_invented_private_message_links():
    message = make_message(chat=dict(id=-123456, type="group"))
    text, entities = assert_valid_text(render_scoreboard(scoreboard(), message, "posts"))
    assert "Сообщение №400" in text
    assert not any(entity.url for entity in entities)


@pytest.mark.parametrize("view", TITLES)
@pytest.mark.parametrize("days", [1, 7, 30])
def test_every_keyboard_button_roundtrips_through_the_actual_wire_parser(view, days):
    keyboard = Reactions.keyboard(view, days)
    assert [len(row) for row in keyboard.inline_keyboard] == [2, 2, 3, 1]
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    parsed = []
    for button in buttons:
        assert 0 < len(button.callback_data.encode()) <= 64
        value = ReactionCallback.unpack(button.callback_data)
        assert set(value.model_dump()) == {"view", "days"}
        assert CHAT_ID not in value.model_dump().values()
        parsed.append(value)
    assert [item.view for item in parsed[:4]] == list(TITLES)
    assert all(item.days == days for item in parsed[:4])
    assert [item.days for item in parsed[4:7]] == [1, 7, 30]
    assert all(item.view == view for item in parsed[4:7])
    assert parsed[-1] == ReactionCallback(view=view, days=days)


@pytest.mark.parametrize(
    "payload",
    ["react:getters:0", "react:getters:31", "react:secret:30", "react:getters:30:-10099", "react:getters:30.0", "react:getters:030"],
)
def test_callback_payload_cannot_supply_another_chat_or_an_unbounded_window(payload):
    with pytest.raises((ValidationError, TypeError, ValueError)):
        ReactionCallback.unpack(payload)


class ReactionSession(RecordingSession):
    def __init__(self):
        super().__init__()
        self.status = ChatMemberStatus.ADMINISTRATOR
        self.admin_error = False
        self.edit_error = None

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetChatMember):
            self.methods.append(method)
            if self.admin_error:
                raise TelegramNetworkError(method=method, message="Synthetic permission check outage")
            return SimpleNamespace(status=self.status)
        if isinstance(method, EditMessageText) and self.edit_error:
            self.methods.append(method)
            raise TelegramBadRequest(method=method, message=self.edit_error)
        return await super().make_request(bot, method, timeout)


@pytest.fixture
async def rig(monkeypatch):
    session = ReactionSession()
    bot = Bot("123456789:" + "a" * 35, session=session)
    clock = [0.0]
    monkeypatch.setattr(Reactions, "permissions", TTLCache(maxsize=512, ttl=60, timer=lambda: clock[0]))
    monkeypatch.setattr(Reactions, "locks", LRUCache(maxsize=512))
    db = SimpleNamespace(reaction_scoreboard=AsyncMock(return_value=scoreboard()))
    message = make_message(bot, message_id=100, message_thread_id=17, is_topic_message=True)
    yield SimpleNamespace(session=session, bot=bot, db=db, message=message, clock=clock)
    await bot.session.close()


def callback(rig, message=None, *, view="pulse", days=7):
    data = ReactionCallback(view=view, days=days)
    query = CallbackQuery.model_validate(
        dict(
            id="synthetic",
            chat_instance="synthetic",
            from_user=dict(id=77, is_bot=False, first_name="Clicker"),
            data=data.pack(),
            message=message or rig.message,
        ),
        context={"bot": rig.bot},
    )
    return query, data


async def test_command_queries_only_current_chat_and_replies_with_default_period(rig):
    await Reactions.process(rig.message, rig.db)
    rig.db.reaction_scoreboard.assert_awaited_once_with(CHAT_ID)
    sent = next(method for method in rig.session.methods if isinstance(method, SendMessage))
    assert sent.chat_id == CHAT_ID and sent.message_thread_id == 17
    assert sent.reply_parameters.message_id == rig.message.message_id
    assert "30 дней" in sent.text and "Магниты реакций" in sent.text
    assert sent.parse_mode is None and sent.entities
    assert sent.disable_web_page_preview is True


@pytest.mark.parametrize("chat_type", ["private", "channel"])
async def test_command_outside_groups_explains_scope_without_reading_database(rig, chat_type):
    message = make_message(rig.bot, chat=dict(id=123, type=chat_type))
    await Reactions.process(message, rig.db)
    rig.db.reaction_scoreboard.assert_not_awaited()
    assert [type(method) for method in rig.session.methods] == [SendMessage]
    assert "групповом чате" in rig.session.methods[0].text


@pytest.mark.parametrize("kind", ["private", "channel", "inaccessible", "inline"])
async def test_callback_without_accessible_group_is_acknowledged_without_database_or_edits(rig, kind):
    if kind == "inline":
        query = CallbackQuery.model_validate(
            dict(
                id="synthetic",
                chat_instance="synthetic",
                from_user=dict(id=77, is_bot=False, first_name="Clicker"),
                inline_message_id="synthetic-inline",
            ),
            context={"bot": rig.bot},
        )
        data = ReactionCallback(view="getters", days=30)
    else:
        message = (
            InaccessibleMessage(chat=rig.message.chat, message_id=100, date=0)
            if kind == "inaccessible"
            else make_message(rig.bot, chat=dict(id=123, type=kind))
        )
        query, data = callback(rig, message)
    await Reactions.process_cb(query, data, rig.db)
    rig.db.reaction_scoreboard.assert_not_awaited()
    assert [type(method) for method in rig.session.methods] == [AnswerCallbackQuery]
    assert "групповом чате" in rig.session.methods[0].text


async def test_callback_acknowledges_before_database_and_edits_only_its_current_chat_message(rig):
    async def read(chat_id, *, days):
        assert [type(method) for method in rig.session.methods] == [AnswerCallbackQuery]
        assert chat_id == -1009876543210 and days == 7
        return scoreboard(days=days)

    rig.db.reaction_scoreboard.side_effect = read
    other = make_message(rig.bot, chat=dict(id=-1009876543210, type="supergroup"), message_id=900)
    query, data = callback(rig, other)
    await Reactions.process_cb(query, data, rig.db)
    edited = next(method for method in rig.session.methods if isinstance(method, EditMessageText))
    assert edited.chat_id == other.chat.id and edited.message_id == 900
    assert edited.parse_mode is None and edited.entities
    assert "7 дней" in edited.text and "Пульс реакций" in edited.text
    assert not any(isinstance(method, SendMessage) for method in rig.session.methods)


async def test_overlapping_callbacks_acknowledge_both_and_perform_only_one_read_and_edit(rig):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def read(chat_id, *, days):
        entered.set()
        await finish.wait()
        return scoreboard(days=days)

    rig.db.reaction_scoreboard.side_effect = read
    query, data = callback(rig)
    first = asyncio.create_task(Reactions.process_cb(query, data, rig.db))
    await entered.wait()
    second_query, second_data = callback(rig, view="posts", days=30)
    assert await Reactions.process_cb(second_query, second_data, rig.db) is True
    finish.set()
    await first
    rig.db.reaction_scoreboard.assert_awaited_once()
    assert sum(isinstance(method, AnswerCallbackQuery) for method in rig.session.methods) == 2
    assert sum(isinstance(method, EditMessageText) for method in rig.session.methods) == 1
    assert not Reactions.lock(rig.message).locked()


@pytest.mark.parametrize(
    "error", ["Bad Request: message is not modified", "message is not modified: specified new content is exactly the same"]
)
async def test_noop_refresh_is_successful_after_callback_acknowledgement(rig, error):
    rig.session.edit_error = error
    assert await Reactions.process_cb(*callback(rig), rig.db) is True
    assert isinstance(rig.session.methods[0], AnswerCallbackQuery)
    assert not Reactions.lock(rig.message).locked()


async def test_real_edit_failure_propagates_and_releases_message_lock(rig):
    rig.session.edit_error = "Bad Request: chat not found"
    with pytest.raises(TelegramBadRequest, match="chat not found"):
        await Reactions.process_cb(*callback(rig), rig.db)
    assert not Reactions.lock(rig.message).locked()


async def test_database_failure_is_acknowledged_and_releases_message_lock(rig):
    rig.db.reaction_scoreboard.side_effect = RuntimeError("Synthetic database outage")
    with pytest.raises(RuntimeError, match="database outage"):
        await Reactions.process_cb(*callback(rig), rig.db)
    assert [type(method) for method in rig.session.methods] == [AnswerCallbackQuery]
    assert not Reactions.lock(rig.message).locked()


@pytest.mark.parametrize(
    "status,expected",
    [
        (ChatMemberStatus.ADMINISTRATOR, True),
        (ChatMemberStatus.CREATOR, True),
        (ChatMemberStatus.MEMBER, False),
        (ChatMemberStatus.LEFT, False),
    ],
)
async def test_administrator_status_cache_is_short_lived_and_scoped_to_chat_and_bot(rig, status, expected):
    rig.session.status = status
    assert await Reactions._administrator(rig.message) is expected
    assert await Reactions._administrator(rig.message) is expected
    assert len(rig.session.methods) == 1
    assert (rig.session.methods[0].chat_id, rig.session.methods[0].user_id) == (CHAT_ID, rig.bot.id)
    other_chat = make_message(rig.bot, chat=dict(id=-1009876543210, type="supergroup"))
    await Reactions._administrator(other_chat)
    assert len(rig.session.methods) == 2
    other_bot = Bot("987654321:" + "b" * 35, session=rig.session)
    await Reactions._administrator(make_message(other_bot))
    assert len(rig.session.methods) == 3
    rig.clock[0] = 61
    rig.session.status = ChatMemberStatus.MEMBER if expected else ChatMemberStatus.ADMINISTRATOR
    assert await Reactions._administrator(rig.message) is (not expected)
    assert len(rig.session.methods) == 4


async def test_permission_api_failure_is_not_cached_or_reported_as_definite_missing_rights(rig):
    rig.session.admin_error = True
    await Reactions.process(rig.message, rig.db)
    sent = next(method for method in rig.session.methods if isinstance(method, SendMessage))
    assert "Не удалось проверить права" in sent.text
    assert len(Reactions.permissions) == 0
    rig.session.admin_error = False
    assert await Reactions._administrator(rig.message) is True


async def test_missing_admin_permissions_preserves_saved_scoreboard_with_actionable_note(rig):
    rig.session.status = ChatMemberStatus.MEMBER
    await Reactions.process(rig.message, rig.db)
    text = next(method for method in rig.session.methods if isinstance(method, SendMessage)).text
    assert "Магниты реакций" in text
    assert "нужны права администратора" in text and "сохранённые данные" in text
