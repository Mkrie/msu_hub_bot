"""Complete VK posts survive Telegram's text, entity and media boundaries."""

from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.methods import SendMediaGroup, SendMessage, SendPhoto, SendVideo
from aiogram.types import Message, Update
from aiogram.utils.markdown import hide_link

from msu_hub_bot.providers.vk import publish
from msu_hub_bot.providers.vk.utils import href, split_html, utf16_length
from msu_hub_bot.telegram.middlewares.settings import Settings
from msu_hub_bot.telegram.middlewares.viewer import ViewerMiddleware
from msu_hub_bot.telegram.wrapper import BotWrapper
from telegram_helpers import RecordingSession, make_message
from test_vk import attachment, check_html, parse_post


def checked_chunks(html, **limits):
    chunks = list(split_html(html, **limits))
    for chunk in chunks:
        parsed = check_html(chunk)
        assert parsed.text.strip(), "Telegram rejects whitespace-only messages"
        assert utf16_length(parsed.text) <= limits.get("limit", 4096)
        assert len(chunk.encode("utf-8")) <= limits.get("max_bytes", 32768)
        assert len(parsed.links) <= limits.get("max_links", 100)
    return chunks


@pytest.mark.parametrize("text", ["😀" * 10_000, "<&>" * 10_000, "Привет, мир!\n" * 1000])
def test_long_text_is_complete_after_html_splitting(text):
    chunks = checked_chunks(escape(text))
    assert len(chunks) > 1
    assert "".join(check_html(chunk).text for chunk in chunks) == text


def test_long_link_label_keeps_its_target_in_every_chunk():
    label = "😀<&>" * 10_000
    url = "https://vk.ru/id20?first=1&second=2"
    chunks = checked_chunks(href(url, label))
    assert "".join(check_html(chunk).text for chunk in chunks) == label
    assert all(check_html(chunk).links == [url] for chunk in chunks)


def test_markup_overhead_does_not_force_visibly_short_posts_to_split():
    text = "<&>" * 350
    html = href("https://vk.ru/id20", text)
    assert len(html) > 4096
    assert checked_chunks(html) == [html]


@pytest.mark.parametrize("length,expected_parts", [(4095, 1), (4096, 1), (4097, 2)])
def test_exact_text_limit_keeps_every_character(length, expected_parts):
    text = "я" * length
    chunks = checked_chunks(text)
    assert len(chunks) == expected_parts
    assert "".join(chunks) == text


def test_raw_html_byte_limit_keeps_many_long_link_targets_intact():
    targets = [f"https://example.org/{index}?q=" + "&" * 1600 for index in range(20)]
    html = "\n".join(href(target, str(index)) for index, target in enumerate(targets))
    chunks = checked_chunks(html)
    assert len(chunks) > 1
    assert [target for chunk in chunks for target in check_html(chunk).links] == targets
    assert "".join(check_html(chunk).text for chunk in chunks) == "\n".join(map(str, range(20)))


def test_dense_links_split_before_telegram_can_drop_their_entities():
    targets = [f"https://vk.ru/id{index + 1}" for index in range(201)]
    html = " ".join(href(target, "x") for target in targets)
    chunks = checked_chunks(html)
    assert len(chunks) >= 3
    assert [target for chunk in chunks for target in check_html(chunk).links] == targets
    assert "".join(check_html(chunk).text for chunk in chunks) == " ".join("x" for _ in targets)


@pytest.mark.parametrize("max_links", [1, 100])
def test_boundary_at_end_of_link_does_not_reopen_an_empty_entity(max_links):
    targets = [f"https://vk.ru/id{index + 1}" for index in range(max_links + 1)]
    html = "".join(href(target, "x\n") for target in targets)
    chunks = checked_chunks(html, max_links=max_links)
    assert "".join(check_html(chunk).text for chunk in chunks) == "x\n" * len(targets)
    assert [target for chunk in chunks for target in check_html(chunk).links] == targets


def test_custom_small_limits_count_decoded_entities_and_astral_characters():
    html = href("https://vk.ru/id20", "😀<&>" * 25)
    chunks = checked_chunks(html, limit=13, max_bytes=100, max_links=1)
    assert "".join(check_html(chunk).text for chunk in chunks) == "😀<&>" * 25


def test_paragraph_boundary_is_preferred_when_recent():
    first = "я" * 3900 + "\n\n"
    chunks = checked_chunks(first + "next paragraph " * 100)
    assert check_html(chunks[0]).text == first


@pytest.mark.parametrize("html", ["", " " * 20_000, "\n" * 20_000, href("https://vk.ru/id20", " \n " * 10_000)])
def test_empty_and_whitespace_only_text_never_produces_a_message(html):
    assert checked_chunks(html) == []


def test_long_blank_paragraph_cannot_hide_nonempty_tail():
    text = "Начало" + "\n" * 20_000 + "Конец"
    chunks = checked_chunks(text)
    assert "".join(check_html(chunk).text for chunk in chunks).replace("\n", "") == "НачалоКонец"


class NumberedSession(RecordingSession):
    async def make_request(self, bot, method, timeout=None):
        result = await super().make_request(bot, method, timeout)
        if isinstance(result, Message):
            return result.model_copy(update={"message_id": 1000 + len(self.methods)})
        return result


@pytest.fixture
async def delivery(monkeypatch):
    monkeypatch.setattr(publish, "settings", SimpleNamespace(vk_default_chat_id=-10))
    session = NumberedSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    yield bot, session
    await session.close()


def photo(index):
    return attachment("photo", {"sizes": [{"url": f"https://sun9.userapi.com/{index}.jpg"}]})


def sent_text(session, preview=None):
    methods = [method for method in session.methods if isinstance(method, SendMessage)]
    text = []
    for method in methods:
        parsed = check_html(method.text)
        assert utf16_length(parsed.text) <= 4096
        assert len(method.text.encode("utf-8")) <= 32768
        assert len(parsed.links) <= 100
        assert parsed.text.strip()
        visible = parsed.text
        if preview and method.text.startswith(hide_link(preview)):
            visible = visible.removeprefix("\u200b")
        text.append(visible)
    return methods, "".join(text)


async def test_publication_preserves_all_content_reply_chain_topic_and_one_album(delivery):
    bot, session = delivery
    parsed = parse_post(
        text="😀 <first> & " * 1200,
        copy_history=[{"id": 2, "owner_id": -20, "text": "Копия в самом конце", "signer_id": 25}],
        attachments=[photo(1), photo(2), attachment("audio", {"artist": "Автор", "title": "Последнее вложение"})],
    )
    expected = check_html(parsed.render()).text
    await publish.publish_vk_post(parsed, bot, -20, reply_to=41, message_thread_id=17)
    messages, text = sent_text(session)
    assert len(messages) > 2 and text == expected
    assert "Копия в самом конце" in text and "Последнее вложение" in text
    assert messages[0].reply_parameters.message_id == 41
    assert [method.reply_parameters.message_id for method in messages[1:]] == list(range(1001, 1000 + len(messages)))
    assert all(method.message_thread_id == 17 for method in session.methods)
    assert all(method.disable_web_page_preview is True for method in messages)
    albums = [method for method in session.methods if isinstance(method, SendMediaGroup)]
    assert len(albums) == 1 and session.methods[-1] is albums[0]
    assert [media.media for media in albums[0].media] == ["https://sun9.userapi.com/1.jpg", "https://sun9.userapi.com/2.jpg"]
    assert albums[0].reply_parameters.message_id == 1000 + len(messages)


@pytest.mark.parametrize("length", [4095, 4096, 8192])
async def test_exact_text_limits_leave_room_for_the_single_preview(delivery, length):
    bot, session = delivery
    parsed = parse_post(text="я" * length, attachments=[photo(1)])
    preview = "https://sun9.userapi.com/1.jpg"
    await publish.publish_vk_post(parsed, bot, -20, with_header=False)
    messages, text = sent_text(session, preview)
    assert text == "я" * length
    assert sum(method.text.startswith(hide_link(preview)) for method in messages) == 1
    assert messages[-1].text.startswith(hide_link(preview))
    assert all(method.disable_web_page_preview is True for method in messages[:-1])
    assert messages[-1].disable_web_page_preview is False
    assert not any(isinstance(method, SendPhoto | SendMediaGroup | SendVideo) for method in session.methods)


@pytest.mark.parametrize("count,long_targets", [(100, False), (201, False), (12, True)])
async def test_preview_keeps_its_entity_and_raw_bytes_with_dense_link_posts(delivery, count, long_targets):
    bot, session = delivery
    targets = [f"https://example.org/{index}?q=" + ("&" * 1600 if long_targets else "1") for index in range(count)]
    parsed = parse_post(text=" ".join(f"[{target}|x]" for target in targets), attachments=[photo(1)])
    preview = "https://sun9.userapi.com/1.jpg"
    await publish.publish_vk_post(parsed, bot, -20, with_header=False)
    messages, text = sent_text(session, preview)
    assert text == " ".join("x" for _ in targets)
    delivered_targets = [url for method in messages for url in check_html(method.text).links if url != preview]
    assert delivered_targets == targets


async def test_short_url_crossing_a_text_boundary_keeps_its_complete_target(delivery):
    bot, session = delivery
    target = "https://vk.ru/id20"
    source = "я" * 4090 + target
    await publish.publish_vk_post(parse_post(text=source), bot, -20, with_header=False)
    messages, text = sent_text(session)
    assert len(messages) == 2 and text == source
    assert any(target in check_html(method.text).text for method in messages)


@pytest.mark.parametrize("text", ["Текст " * 2000, "😀" * 600, "<&>" * 1000])
async def test_default_destination_uses_full_text_and_media_when_caption_would_overflow(delivery, text):
    bot, session = delivery
    parsed = parse_post(text=text, attachments=[photo(1), photo(2)])
    await publish.publish_vk_post(parsed, bot, -10, reply_to=41, message_thread_id=17)
    messages, visible = sent_text(session)
    assert messages and visible == text.strip()
    assert len([method for method in session.methods if isinstance(method, SendMediaGroup)]) == 1
    assert all(method.message_thread_id == 17 for method in session.methods)


async def test_default_destination_keeps_a_complete_short_post_in_its_photo_caption(delivery):
    bot, session = delivery
    parsed = parse_post(text="Коротко <&> 😀", attachments=[photo(1)])
    await publish.publish_vk_post(parsed, bot, -10, reply_to=41, message_thread_id=17)
    assert len(session.methods) == 1 and isinstance(session.methods[0], SendPhoto)
    method = session.methods[0]
    assert check_html(method.caption).text == "Коротко <&> 😀"
    assert method.reply_parameters.message_id == 41 and method.message_thread_id == 17


async def test_automatic_long_vk_preview_preserves_forum_topic_after_real_dispatch(delivery):
    bot, session = delivery
    url = "https://vk.ru/wall-10_1"
    api = SimpleNamespace(get_wall_post=AsyncMock(return_value=([parse_post(text="я" * 10_000).post], {})))
    dispatcher = Dispatcher()
    dispatcher.message.outer_middleware(ViewerMiddleware(bot, api, SimpleNamespace(run=AsyncMock())))

    @dispatcher.message()
    async def received(message):
        return None

    message = make_message(
        bot,
        message_id=41,
        message_thread_id=17,
        is_topic_message=True,
        text=url,
        entities=[{"type": "url", "offset": 0, "length": len(url)}],
    )
    await dispatcher.feed_update(bot, Update(update_id=1, message=message), settings=Settings(auto_video_links=False))
    messages, text = sent_text(session)
    assert len(messages) > 1 and text.endswith("я" * 10_000)
    assert messages[0].reply_parameters.message_id == 41
    assert all(method.message_thread_id == 17 for method in messages)
    assert all(not method.__api_method__.startswith("delete") for method in session.methods)
