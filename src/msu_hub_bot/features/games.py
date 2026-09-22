"""Native quiz contracts with shared bounded card delivery, including worker edits."""

from typing import cast

from aiogram import Bot
from aiogram.methods import EditMessageCaption, EditMessageMedia, SendPhoto, TelegramMethod
from aiogram.types import InlineKeyboardMarkup, InputFile, InputMediaPhoto, Message
from teleforge import CallbackContext, Feature, MessageContext, callback
from teleforge.cards import Card, prepare_card
from teleforge.delivery import DeliveryError, DeliveryTarget, ResponsePolicy, edit_response, send_response

from msu_hub_bot.commands.chess import ChessCallback
from msu_hub_bot.commands.geoguess import GeoguessCallback
from msu_hub_bot.features.command import command
from msu_hub_bot.games.quiz import QuizService


class QuizDelivery:
    """Adapt only the quiz's photo UI; its service keeps native errors and recovery."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def __call__[T](self, method: TelegramMethod[T], request_timeout: int | None = None) -> T:
        if not isinstance(method, (SendPhoto, EditMessageCaption, EditMessageMedia)):
            return await self.bot(method, request_timeout=request_timeout)
        if not isinstance(method.chat_id, int):
            raise TypeError("Quiz messages require a numeric chat identity")
        if method.reply_markup is not None and not isinstance(method.reply_markup, InlineKeyboardMarkup):
            raise TypeError("Quiz cards require inline buttons")
        markup = method.reply_markup
        photo: str | InputFile | None
        if isinstance(method, EditMessageMedia):
            if not isinstance(method.media, InputMediaPhoto):
                raise TypeError("Quiz cards contain photos")
            text, entities, photo = method.media.caption, method.media.caption_entities, method.media.media
        else:
            text, entities = method.caption, method.caption_entities
            photo = method.photo if isinstance(method, SendPhoto) else None
        view = Card(
            text=text,
            entities=entities,
            photo=photo,
            buttons=markup.inline_keyboard if markup is not None else (),
        )
        content = await prepare_card(view)
        policy = ResponsePolicy(rich=False, timeout=request_timeout or 15)
        try:
            if isinstance(method, SendPhoto):
                target = DeliveryTarget(
                    chat_id=method.chat_id,
                    message_id=method.reply_parameters.message_id if method.reply_parameters else None,
                    thread_id=method.message_thread_id,
                    business_connection_id=method.business_connection_id,
                )
                result: object = await send_response(
                    self.bot,
                    target,
                    **content,
                    fixed=True,
                    policy=policy,
                    allow_remote_media=True,
                    request_timeout=request_timeout,
                )
            else:
                target = DeliveryTarget(
                    chat_id=method.chat_id,
                    message_id=method.message_id,
                    business_connection_id=method.business_connection_id,
                    kind="photo",
                )
                result = await edit_response(
                    self.bot,
                    target,
                    **content,
                    policy=policy,
                    allow_remote_media=True,
                    request_timeout=request_timeout,
                )
        except DeliveryError as error:
            # The quiz already distinguishes rejected edits from uncertain publication.
            # Preserve that service contract rather than introducing a second retry loop.
            raise error.cause from error
        return cast(T, result)


class Games(Feature, key="games"):
    def __init__(self, quiz: QuizService) -> None:
        self.quiz = quiz

    @command("chess", flags={"fsm_release": True})
    async def chess(self, ctx: MessageContext) -> Message | None:
        return await self.quiz.start("chess", ctx.message)

    @command("geoguess", flags={"fsm_release": True})
    async def geoguess(self, ctx: MessageContext) -> Message | None:
        return await self.quiz.start("geoguess", ctx.message)

    @callback(ChessCallback, ack="manual", flags={"fsm_release": True})
    async def chess_vote(self, ctx: CallbackContext, round: str, choice: str) -> bool | None:
        return await self.quiz.callback("chess", ctx.query, round, choice)

    @callback(GeoguessCallback, ack="manual", flags={"fsm_release": True})
    async def geoguess_vote(self, ctx: CallbackContext, round: str, choice: str) -> bool | None:
        return await self.quiz.callback("geoguess", ctx.query, round, choice)
