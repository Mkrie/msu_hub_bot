"""Chat-local reaction rankings in one navigable message."""

from typing import Literal

from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.formatting import Bold, Text, TextLink
from cachetools import TTLCache
from pydantic import field_validator

from msu_hub_bot.commands.quiz_view import compact
from msu_hub_bot.storage.base import BotRepository
from msu_hub_bot.storage.reactions import ReactionRank, ReactionScoreboard
from msu_hub_bot.telegram.callbacks import CallbackCommandBase

View = Literal["getters", "givers", "posts", "pulse"]
Days = Literal[1, 7, 30]
TITLES: dict[View, str] = {
    "getters": "🧲 Магниты реакций",
    "givers": "💛 Щедрые на реакции",
    "posts": "🔥 Хиты чата",
    "pulse": "📊 Пульс реакций",
}
PERIODS: dict[int, str] = {1: "24 часа", 7: "7 дней", 30: "30 дней"}


class ReactionCallback(CallbackData, prefix="react"):
    view: View
    days: Days

    @field_validator("days", mode="before")
    @classmethod
    def parse_days(cls, value: object) -> object:
        # CallbackData unpacks string tokens before Pydantic validates literals.
        return int(value) if isinstance(value, str) and value in {"1", "7", "30"} else value


def _number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _person(row: ReactionRank) -> Text:
    name = " ".join(part for part in (row.first_name, row.last_name) if part)
    return TextLink(compact(name, 48) or "Участник", url=f"tg://user?id={row.user_id}")


def _ranking(rows: list[ReactionRank], view: View) -> Text:
    if not rows:
        return Text("Пока тихо. За этот период ещё нет баллов.\n")
    lines: list[Text] = []
    for rank, row in enumerate(rows[:10], 1):
        medal = ("🥇", "🥈", "🥉")[rank - 1] if rank <= 3 else f"{rank}."
        lines.append(
            Text(
                medal,
                " ",
                _person(row),
                " — ",
                Bold(_number(row.score)),
                "\n     👥 ",
                _number(row.people),
                " · 💬 ",
                _number(row.messages),
                "\n",
            )
        )
    legend = "разные авторы" if view == "givers" else "разные участники"
    return Text(*lines, "\n👥 ", legend, " · 💬 сообщения\n")


def render_scoreboard(board: ReactionScoreboard, message: Message, view: View, *, administrator: bool | None = True) -> Text:
    parts: list[Text | str] = [Bold(TITLES[view]), " · ", PERIODS[board.days], "\n", compact(message.chat.title or "Этот чат", 64), "\n\n"]
    if view in {"getters", "givers"}:
        parts.append(_ranking(board.getters if view == "getters" else board.givers, view))
    elif view == "posts":
        if not board.posts:
            parts.append("Хиты ещё впереди. Здесь появятся сообщения, собравшие больше всего людей.\n")
        for rank, post in enumerate(board.posts[:10], 1):
            target = message.model_copy(
                update={"message_id": post.message_id, "message_thread_id": post.thread_id, "is_topic_message": post.thread_id is not None}
            )
            url = target.get_url(force_private=True, include_thread_id=True)
            label = f"Сообщение №{post.message_id}"
            parts.append(Text(f"{rank}. ", TextLink(label, url=url) if url else label, " — ", Bold(_number(post.score)), " 💛\n"))
    else:
        summary = board.summary
        parts.extend(
            [
                Text("💛 Баллы: ", Bold(_number(summary.points)), " · эмодзи: ", _number(summary.reactions), "\n"),
                Text("👥 Дарят: ", _number(summary.givers), " · получают: ", _number(summary.getters), "\n"),
                Text("💬 Сообщений с реакциями: ", _number(summary.messages), "\n"),
            ]
        )
        emoji: dict[str, int] = {}
        for item in board.emoji:
            label = item.key[2:] if item.key.startswith("e:") else "▫️ Свои эмодзи"
            emoji[label] = emoji.get(label, 0) + item.count
        if emoji:
            parts.append(Text("\n", Bold("Чем реагирует чат"), "\n"))
            ceiling = max(emoji.values())
            for label, count in sorted(emoji.items(), key=lambda entry: (-entry[1], entry[0]))[:6]:
                bars = "▰" * max(1, round(6 * count / max(1, ceiling)))
                parts.append(Text(compact(label, 32), " ", bars, " ", _number(count), "\n"))
        if summary.anonymous:
            parts.append(Text("\n🕶 Анонимные реакции: ", _number(summary.anonymous), "\n"))
        if summary.paid:
            parts.append(Text("⭐️ Платные реакции: ", _number(summary.paid), "\n"))
        if summary.channel_reactions:
            parts.append(Text("📣 От имени чатов: ", _number(summary.channel_reactions), "\n"))
        if summary.anonymous or summary.paid or summary.channel_reactions:
            parts.append("Текущие итоги с изменениями за период; отдельно от личного рейтинга.\n")
    if board.summary.unattributed:
        parts.append(
            Text("\nБез личного получателя: ", _number(board.summary.unattributed), " баллов. Автор не виден, либо это бот или чат.\n")
        )
    parts.append(
        "\n1 человек → 1 сообщение = 1 балл. Считаю реакции, добавленные при мне в этот период и ещё не снятые. "
        "Смена эмодзи не добавляет балл. Когда автор известен, свои реакции не считаются."
    )
    if administrator is False:
        parts.append("\n\n⚙️ Для новых реакций нужны права администратора. Сейчас показываю сохранённые данные.")
    elif administrator is None:
        parts.append("\n\n⚙️ Не удалось проверить права бота. Для сбора реакций нужны права администратора.")
    return Text(*parts)


class Reactions(CallbackCommandBase):
    callback_data = ReactionCallback
    permissions: TTLCache[tuple[int, int], bool] = TTLCache(maxsize=512, ttl=60)

    @staticmethod
    def keyboard(view: View, days: Days) -> InlineKeyboardMarkup:
        views: list[tuple[View, str]] = [("getters", "🧲 Получают"), ("givers", "💛 Дарят"), ("posts", "🔥 Посты"), ("pulse", "📊 Пульс")]
        buttons = [
            InlineKeyboardButton(
                text=("• " if selected == view else "") + label, callback_data=ReactionCallback(view=selected, days=days).pack()
            )
            for selected, label in views
        ]
        periods: tuple[Days, ...] = (1, 7, 30)
        return InlineKeyboardMarkup(
            inline_keyboard=[
                buttons[:2],
                buttons[2:],
                [
                    InlineKeyboardButton(
                        text=("• " if period == days else "") + PERIODS[period],
                        callback_data=ReactionCallback(view=view, days=period).pack(),
                    )
                    for period in periods
                ],
                [InlineKeyboardButton(text="↻ Обновить", callback_data=ReactionCallback(view=view, days=days).pack())],
            ]
        )

    @classmethod
    async def _administrator(cls, message: Message) -> bool | None:
        assert message.bot is not None
        key = (message.bot.id, message.chat.id)
        if key in cls.permissions:
            return cls.permissions[key]
        try:
            member = await message.bot.get_chat_member(message.chat.id, message.bot.id)
        except TelegramAPIError:
            return None
        result = member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
        cls.permissions[key] = result
        return result

    @classmethod
    async def process(cls, message: Message, db: BotRepository) -> Message:
        if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return await message.reply(
                "Рейтинг живёт в групповом чате: напиши там /reactions. Для сбора реакций мне нужны права администратора."
            )
        board = await db.reaction_scoreboard(message.chat.id)
        content = render_scoreboard(board, message, "getters", administrator=await cls._administrator(message))
        return await message.reply(**content.as_kwargs(), reply_markup=cls.keyboard("getters", 30), disable_web_page_preview=True)

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: ReactionCallback, db: BotRepository) -> Message | bool:
        message = query.message
        if not isinstance(message, Message) or message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return await query.answer("Открой /reactions в групповом чате.")
        await query.answer()
        lock = cls.lock(message)
        if lock.locked():
            return True
        async with lock:
            board = await db.reaction_scoreboard(message.chat.id, days=callback_data.days)
            content = render_scoreboard(board, message, callback_data.view, administrator=await cls._administrator(message))
            try:
                return await message.edit_text(
                    **content.as_kwargs(), reply_markup=cls.keyboard(callback_data.view, callback_data.days), disable_web_page_preview=True
                )
            except TelegramBadRequest as error:
                if not error.message.removeprefix("Bad Request: ").casefold().startswith("message is not modified"):
                    raise
        return True
