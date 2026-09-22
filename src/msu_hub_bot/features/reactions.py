"""The reaction scoreboard as one feature, using the existing application read model."""

from aiogram.enums import ChatType
from aiogram.filters import StateFilter
from aiogram.types import Message
from teleforge.cards import Button, Card, action, card, show
from teleforge.context import CallbackContext, Context
from teleforge.feature import Feature

from msu_hub_bot.commands.reactions import Days, ReactionCallback, Reactions, View, render_scoreboard
from msu_hub_bot.storage.base import BotRepository

from .command import command as hub_command

_GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
_GROUP_GUIDANCE = "Рейтинг живёт в групповом чате: напиши там /reactions. Для сбора реакций мне нужны права администратора."


class ReactionsFeature(Feature, key="reactions"):
    def __init__(self, repository: BotRepository) -> None:
        self.repository = repository

    @hub_command(
        "reactions",
        "реакции",
        flags={"handler_key": "Reactions.process", "fsm_release": True},
    )
    async def process(self, ctx: Context) -> Message:
        message = ctx.message
        assert isinstance(message, Message)
        if message.chat.type not in _GROUPS:
            return await ctx.bot.send_message(
                chat_id=message.chat.id,
                text=_GROUP_GUIDANCE,
                reply_parameters=message.as_reply_parameters(),
            )
        result = await show(
            ctx,
            self.scoreboard,
            view="getters",
            days=30,
            chat_id=message.chat.id,
            topic_id=message.message_thread_id if message.is_topic_message else None,
        )
        assert isinstance(result, Message)
        return result

    @card
    async def scoreboard(self, ctx: Context, view: View, days: Days, chat_id: int, topic_id: int | None) -> Card:
        message = ctx.message
        if not isinstance(message, Message) or not self._origin(message, chat_id, topic_id):
            raise ValueError("Reaction card origin changed")
        board = await self.repository.reaction_scoreboard(message.chat.id, days=days)
        content = render_scoreboard(board, message, view, administrator=await Reactions._administrator(message.as_(ctx.bot)))
        rows = []
        # Share the maintained Russian copy and layout with the native adapter.
        for row in Reactions.keyboard(view, days).inline_keyboard:
            buttons = []
            for native in row:
                assert native.callback_data is not None
                value = ReactionCallback.unpack(native.callback_data)
                buttons.append(Button(native.text, self.process_cb, view=value.view, days=value.days))
            rows.append(buttons)
        return Card(content, buttons=rows)

    @action(
        key="navigate",
        card="scoreboard",
        ack="early",
        coalesce=True,
        filters=(StateFilter(None),),
        flags={"handler_key": "Reactions.process_cb", "fsm_release": True},
    )
    async def process_cb(
        self,
        ctx: CallbackContext,
        view: View,
        days: Days,
        chat_id: int,
        topic_id: int | None,
    ) -> bool | None:
        message = ctx.message
        # This is a shared group scoreboard: any human participant may navigate it.
        # Guard the clicked origin before the renderer performs a database read.
        if ctx.user is None or ctx.user.is_bot or not isinstance(message, Message):
            return False
        if not self._origin(message, chat_id, topic_id):
            return False
        return None

    @staticmethod
    def _origin(message: Message, chat_id: int, topic_id: int | None) -> bool:
        actual_topic = message.message_thread_id if message.is_topic_message else None
        return message.chat.type in _GROUPS and message.chat.id == chat_id and actual_topic == topic_id
