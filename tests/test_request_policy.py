import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.client.default import DefaultBotProperties
from aiogram import Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import GetMe, GetUpdates, SendMessage
from aiogram.types import Update
from aiogram.utils.backoff import BackoffConfig

from msu_hub_bot.telegram.membership_inbox import MembershipInbox, MembershipInboxError
from msu_hub_bot.telegram.wrapper import BotWrapper
from telegram_helpers import RecordingSession, make_message


class FailingSession(RecordingSession):
    def __init__(self, failure, times=1):
        super().__init__()
        self.failure = failure
        self.remaining = times
        self.attempts = 0

    async def make_request(self, bot, method, timeout=None):
        self.attempts += 1
        if self.remaining:
            self.remaining -= 1
            raise self.failure(method)
        return await super().make_request(bot, method, timeout)


@pytest.mark.parametrize("error", [TelegramNetworkError, TelegramServerError])
async def test_ambiguous_mutations_are_never_replayed(error):
    session = FailingSession(lambda method: error(method=method, message="ambiguous"))
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    with pytest.raises(error):
        await bot(SendMessage(chat_id=42, text="one write"))
    assert session.attempts == 1


async def test_reads_retry_with_a_bound(monkeypatch):
    monkeypatch.setattr("msu_hub_bot.telegram.wrapper.asyncio.sleep", AsyncMock())
    session = FailingSession(lambda method: TelegramNetworkError(method=method, message="offline"), times=10)
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    with pytest.raises(TelegramNetworkError):
        await bot(GetMe())
    assert session.attempts == 3


async def test_explicit_rejection_can_retry_a_write(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("msu_hub_bot.telegram.wrapper.asyncio.sleep", sleep)
    session = FailingSession(lambda method: TelegramRetryAfter(method=method, message="wait", retry_after=2))
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    await bot.send_message(42, "one accepted write")
    assert session.attempts == 2
    sleep.assert_awaited_once_with(2)


async def test_long_retry_after_is_returned_to_error_policy():
    session = FailingSession(lambda method: TelegramRetryAfter(method=method, message="wait", retry_after=120))
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    with pytest.raises(TelegramRetryAfter):
        await bot.send_message(42, "later")
    assert session.attempts == 1


async def test_reply_fallback_is_nested_before_transport():
    session = RecordingSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    message = make_message(bot)
    await message.reply("hi", allow_sending_without_reply=True)
    sent = session.methods[0]
    assert sent.reply_parameters.message_id == message.message_id
    assert sent.reply_parameters.allow_sending_without_reply is True


async def test_heartbeat_records_only_successful_getupdates(monkeypatch):
    marked = []
    monkeypatch.setattr("msu_hub_bot.telegram.wrapper.mark_poll_success", lambda: marked.append(True))
    session = RecordingSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    await bot(GetMe())
    assert marked == []
    await bot(GetUpdates(timeout=0))
    assert marked == [True]


async def test_poll_telemetry_uses_metrics_without_traces(monkeypatch):
    from msu_hub_bot.telemetry import Telemetry
    from telemetry_helpers import Capture, config

    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    capture = Capture()
    telemetry = Telemetry(config(), transport=capture)
    await telemetry.start()
    session = RecordingSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session, telemetry=telemetry)
    try:
        await bot(GetUpdates(timeout=0))
        await bot(GetUpdates(timeout=0))
    finally:
        await bot.session.close()
        await telemetry.close()
    assert capture.spans() == []
    output = capture.serialized()
    assert "bot.poll.requests" in output
    assert "success" in output
    assert "123456789" not in output


async def test_poll_returns_and_marks_healthy_only_after_durable_capture(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    marked = Mock()
    monkeypatch.setattr("msu_hub_bot.telegram.wrapper.mark_poll_success", marked)

    async def capture(updates):
        entered.set()
        await release.wait()

    inbox = Mock(spec=MembershipInbox)
    inbox.capture = AsyncMock(side_effect=capture)
    bot = BotWrapper("123456789:" + "a" * 35, session=RecordingSession(), membership_inbox=inbox)
    polling = asyncio.create_task(bot(GetUpdates(timeout=0)))
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert not polling.done()
    marked.assert_not_called()
    release.set()
    assert await polling == []
    inbox.capture.assert_awaited_once_with([])
    marked.assert_called_once_with()


async def test_aiogram_keeps_offset_after_inbox_failure_and_retries_before_yield(monkeypatch):
    marked = Mock()
    monkeypatch.setattr("msu_hub_bot.telegram.wrapper.mark_poll_success", marked)

    class PollSession(RecordingSession):
        def __init__(self):
            super().__init__()
            self.offsets = []

        async def make_request(self, bot, method, timeout=None):
            self.offsets.append(method.offset)
            return [Update(update_id=100 if len(self.offsets) < 3 else 101)]

    session = PollSession()
    inbox = Mock(spec=MembershipInbox)
    inbox.capture = AsyncMock(side_effect=[MembershipInboxError("Membership inbox persistence failed"), None, None])
    bot = BotWrapper("123456789:" + "a" * 35, session=session, membership_inbox=inbox)
    updates = Dispatcher._listen_updates(
        bot, polling_timeout=0, backoff_config=BackoffConfig(min_delay=0.001, max_delay=0.01, factor=2, jitter=0)
    )
    try:
        first = await asyncio.wait_for(anext(updates), timeout=1)
        assert first.update_id == 100 and session.offsets == [None, None]
        assert marked.call_count == 1
        second = await asyncio.wait_for(anext(updates), timeout=1)
        assert second.update_id == 101 and session.offsets == [None, None, 101]
        assert marked.call_count == 2
    finally:
        await updates.aclose()


async def test_real_journal_is_committed_when_getupdates_returns(tmp_path):
    update = Update(update_id=123, message=make_message(new_chat_members=[{"id": 42, "is_bot": False, "first_name": "Synthetic"}]))

    class PollSession(RecordingSession):
        async def make_request(self, bot, method, timeout=None):
            return [update]

    inbox = MembershipInbox(tmp_path / "inbox.sqlite3", 999, AsyncMock())
    await inbox.open()
    bot = BotWrapper("123456789:" + "a" * 35, session=PollSession(), membership_inbox=inbox)
    try:
        assert await bot(GetUpdates(timeout=0)) == [update]
        assert await inbox.pending() == 1
    finally:
        await inbox.close()


async def test_invalid_poll_result_cannot_be_acknowledged(monkeypatch):
    marked = Mock()
    monkeypatch.setattr("msu_hub_bot.telegram.wrapper.mark_poll_success", marked)

    class InvalidSession(RecordingSession):
        async def make_request(self, bot, method, timeout=None):
            return [{"update_id": 123}]

    inbox = Mock(spec=MembershipInbox)
    bot = BotWrapper("123456789:" + "a" * 35, session=InvalidSession(), membership_inbox=inbox)
    with pytest.raises(MembershipInboxError, match="invalid"):
        await bot(GetUpdates(timeout=0))
    inbox.capture.assert_not_called()
    marked.assert_not_called()
