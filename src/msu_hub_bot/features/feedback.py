"""Feature entrypoints over the existing private-feedback consent workflow."""

from aiogram import F
from aiogram.enums import ContentType
from aiogram.filters import StateFilter
from aiogram.types import Message
from teleforge.context import CallbackContext, MessageContext
from teleforge.declarations import callback
from teleforge.feature import Feature

from msu_hub_bot.commands.feedback import Feedback
from msu_hub_bot.feedback import FeedbackService
from msu_hub_bot.feedback.context import DiagnosticBuffer
from msu_hub_bot.feedback.presentation import FeedbackCallback
from msu_hub_bot.storage.base import BotRepository
from msu_hub_bot.telegram.filters import MetaInfo

from .command import command as hub_command


class FeedbackFeature(Feature, key="feedback"):
    """The service and native workflow remain the authority for exact-preview consent."""

    def __init__(
        self,
        service: FeedbackService,
        repository: BotRepository,
        *,
        diagnostics: DiagnosticBuffer | None = None,
    ) -> None:
        self.service = service
        self.repository = repository
        self.diagnostics = diagnostics

    @hub_command(
        "feedback",
        filters=(F.content_type == ContentType.TEXT,),
        flags={"handler_key": "Feedback.process", "fsm_release": True, "automatic_previews": False},
    )
    async def process(self, ctx: MessageContext, *, meta: MetaInfo) -> Message | None:
        message = ctx.message
        assert isinstance(message, Message)
        return await Feedback.process(message, meta, self.service, self.repository, self.diagnostics)

    @callback(
        FeedbackCallback,
        StateFilter(None),
        ack="manual",
        flags={"handler_key": "Feedback.process_cb", "fsm_release": True},
    )
    async def process_cb(self, ctx: CallbackContext, callback_data: FeedbackCallback) -> None:
        # Automatic card refresh would violate the preview-before-submit barrier.
        # This native workflow owns its acknowledgements and all UI transitions.
        await Feedback.process_cb(ctx.query, callback_data, self.service)
