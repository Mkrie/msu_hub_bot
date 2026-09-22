"""A small feature: typed arguments, literal formatting and native dice."""

from typing import Annotated

from aiogram.types import Message
from aiogram.utils.formatting import Code, Text
from pydantic import BeforeValidator
from teleforge import Feature
from teleforge.context import MessageContext
from teleforge.inputs import Argument

from msu_hub_bot.commands.rolls import get_roll
from msu_hub_bot.features.command import command as hub_command


def _unsigned(value: object) -> object:
    if isinstance(value, str) and not value.isdigit():
        raise ValueError("Use unsigned digits")
    return value


RollDigits = Annotated[int, BeforeValidator(_unsigned)]


class Roll(Feature, key="roll"):
    @hub_command(
        "roll",
        "ролл",
        digits=Argument(clamp=(1, 100)),
        flags={"handler_key": "process_roll", "fsm_release": True},
    )
    async def roll(self, digits: RollDigits = 3) -> Text:
        roll, name = get_roll(digits)
        return Text(Code(roll), f" — {name}" if name else "")

    @hub_command("dice")
    async def dice(self, ctx: MessageContext) -> Message:
        assert isinstance(ctx.message, Message)
        return await ctx.message.reply_dice()
