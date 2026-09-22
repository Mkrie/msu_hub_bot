"""Compiler stdin as a typed, topic-scoped conversation using the bot's provider."""

from contextlib import suppress

from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.utils.formatting import Bold, Pre, Text
from teleforge import CallbackContext, Feature, MessageContext, callback, command, edited_message, enter, leave, read_draft, step

from msu_hub_bot.commands.prog import ProgCallback, ProgCompiler, StdinDraft, code_submit, stdin_source
from msu_hub_bot.features.command import HubCommand, command as hub_command
from msu_hub_bot.providers.jdoodle import LANGUAGES, ManyJDoodle
from msu_hub_bot.telegram.filters import MetaInfo, SlashCommand

_ALIASES = ("py_stdin", "python_stdin", "pys", "pythons")
_FLAGS = {"handler_key": "compile.stdin_prompt.python3", "fsm_release": False}


class Compiler(Feature, key="compiler"):
    def __init__(self, provider: ManyJDoodle) -> None:
        self.provider = provider

    @command("cancel", filter=SlashCommand("cancel"), filters=(StateFilter("teleforge:compiler:stdin"),))
    async def cancel(self, ctx: MessageContext) -> str:
        await leave(ctx)
        return "🆗 Ввод отменён"

    @hub_command(*_ALIASES, flags=_FLAGS)
    @edited_message(HubCommand(*_ALIASES), StateFilter(None), flags=_FLAGS)
    async def prompt(self, ctx: MessageContext, *, meta: MetaInfo) -> Message | bool:
        return await ProgCompiler.process_stdin(ctx.message, meta, ctx.bot, "python3")

    @callback(ProgCallback, flags={"handler_key": "compile.stdin.input", "fsm_release": False})
    async def choose(self, ctx: CallbackContext, action: str, *, state: FSMContext) -> None:
        ui = ctx.message
        if not isinstance(ui, Message) or ui.from_user is None or ui.from_user.id != ctx.bot.id:
            await ctx.answer("Открой команду с кодом ещё раз.")
            return
        current = await state.get_state()
        if action == "cancel":
            if current != "teleforge:compiler:stdin":
                await ctx.answer("💁🏻‍♂️ Вы не в процессе ввода")
                return
            draft = await read_draft(ctx, StdinDraft)
            with suppress(TelegramBadRequest):
                await ctx.bot.delete_message(draft.chat_id, draft.inform_message_id)
            await leave(ctx)
            await ctx.answer("🆗 Ввод отменён")
            return
        if action != "input":
            await ctx.answer("Эта кнопка устарела.")
            return
        if current is not None:
            await ctx.answer("🔄 Бот уже ждёт твой ввод. /cancel — отменить.", show_alert=True)
            return
        source = await stdin_source(ui, ctx.bot)
        if source is None:
            await ctx.answer("Сообщение с исходным кодом недоступно. Пришли код ещё раз.", show_alert=True)
            return
        language, code = source
        await ctx.answer("⬇️ Теперь ожидаю ввод")
        prompt = await ctx.reply("Ожидаю ввод ⬇️, или /cancel", fixed=True)
        assert isinstance(prompt, Message)
        await enter(
            ctx,
            self.stdin,
            StdinDraft(
                chat_id=prompt.chat.id,
                inform_message_id=prompt.message_id,
                prog_lang=language,
                prog_code=code,
            ),
        )

    @step("stdin", draft=StdinDraft)
    async def stdin(self, ctx: MessageContext, draft: StdinDraft) -> None:
        assert isinstance(ctx.message, Message)
        with suppress(TelegramBadRequest):
            await ctx.bot.delete_message(draft.chat_id, draft.inform_message_id)
        await leave(ctx)
        await ctx.release_isolation()
        header = Text(Bold(draft.prog_lang), " | ", Bold(LANGUAGES[draft.prog_lang][1][-1][0]), "\n\n")
        progress = await ctx.reply(Text(header, "🔄 Ожидание..."), fixed=True)
        assert isinstance(progress, Message)
        result = await code_submit(
            self.provider,
            draft.prog_code,
            stdin=ctx.message.text or ctx.message.caption or "",
            lang=draft.prog_lang,
        )
        await ctx.edit(Text(header, Pre(result)) if result else "🤷🏻‍♂️ Произошла какая-то ошибка", to=progress)
