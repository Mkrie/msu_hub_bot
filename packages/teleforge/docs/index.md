# TeleForge authoring guide

TeleForge compiles ordinary feature methods onto aiogram routers. One feature owns related entrypoints, cards and conversation steps. Its instance lives as long as the application: constructor services belong on `self`; the current update belongs in the explicitly typed `ctx` parameter.

Start with [the executable counter](../examples/quickstart.py). `testing.RecordingBot` records actual aiogram methods and uploaded bytes without opening sockets. Test through `App.feed_update` to exercise routing, preparation and delivery together. [AGENTS.md](../AGENTS.md) defines development and review rules.

## Commands and inputs

```python
class Utilities(Feature, key="utilities"):
    @command("roll", digits=Argument(clamp=(1, 100)))
    async def roll(self, digits: int = 3) -> Text:
        return Text(Code(make_roll(digits)))

    @command("meme", text=TextInput(), image=ImageInput(reply=True))
    async def meme(self, ctx: MessageContext, text: str, image: Image.Image) -> None:
        with render_meme(image, text) as encoded:
            await ctx.reply(photo=encoded)
```

Here `render_meme` returns an encoded `BytesIO`; the input is Pillow's decoded image. Install the `media` extra for decoding. Native aiogram media objects, `bytes`, `BytesIO` and `Path` are also supported input representations. Choose a native media object when an application executor must admit work before downloading, as in MSU's caption feature.

Ordinary scalar parameters consume command tokens in signature order. Missing or invalid values use the annotated default; `Argument(strict=True)` reports invalid supplied values instead. Required parameters give brief guidance. `TextInput` explicitly consumes remaining text, then optionally a replied message or UTF-8 text document. Invalid tokens rescued by defaults remain available to text input.

Declarations are named after parameters. An undeclared non-scalar parameter comes from aiogram middleware data by name. `ctx`, `event`, `bot`, message `message`, and callback `query` are invocation values. Prefer `event: InlineQuery` or another native type for non-message events. Constructor injection is ordinary Python.

Attached media takes precedence over replied media. `/meme Hello` replying to a photo selects the command's text and the replied photo: successful output replies to the photo, guidance to the command. `ctx.input_sources` records each source separately. Callback UI is never implicitly interpreted as input; select a source deliberately or load an application record.

Downloads are bounded even when Telegram omits their size. Streams, files and decoded images stay alive through returned-output delivery, then close. Never retain them on the feature, enqueue temporary paths, or pass them to detached tasks. Decode work joins on cancellation. Application executors still own their processing limits.

The default matcher is native, case-insensitive aiogram `Command`. Applications can supply `filter=...` returning `_teleforge_tail` with parsed text. MSU's `HubCommand` retains slash aliases and in-text hashtags. Explicit filters, flags, native routers and middleware remain available.

## Output and delivery

Plain strings are literal text. Use aiogram `Text`, `Bold`, `Code`, `TextLink` and related objects for formatting. TeleForge disables bot-wide parse-mode defaults for its own sends. Explicit entities use Telegram's UTF-16 offsets.

Return text from an ordinary command, or declare `output="photo"` (video, document, audio or animation) when returning encoded media. Use `await ctx.reply(...)` when combining content or needing the sent result. Native aiogram methods and handled results are supported. Native sends bypass TeleForge's delivery policies.

Text plus media prefers one Rich Message when supported by the content's entity/media constraints; otherwise delivery plans captions and complete text chunks before sending. A rejected or uncertain Rich API write is never retried as native messages. The default soft budget is three messages; larger text becomes a complete UTF-8 file. `rich=False`, `soft_messages=...` and `max_output_bytes=...` customize that policy. Hard budgets bound locally owned text/uploads before sending; referenced Telegram files and explicitly allowed remote URLs cannot be preflighted for media size. Output is never silently truncated.

Use `fixed=True` for one message that must stay editable. Oversized fixed output fails before delivery; it is not split, reposted or converted to a file. `ctx.edit` addresses the actual UI regardless of input selection; `to=` selects a native message or `DeliveryTarget`. Inline/inaccessible edits require an explicit content kind when it cannot be inferred. Replying to inaccessible UI requires an explicit target and scope. Full Rich Message edits replace the tree, without inferring partial merges. Omitted or `None` edit markup clears buttons; provide fresh markup on each redraw.

Only finite local media and Telegram file IDs are accepted by default. Remote URLs require `allow_remote_media=True`; validate provider URLs before enabling it. Arbitrary streaming `InputFile` is not a bounded upload. Caller streams stay caller-owned; TeleForge snapshots stay alive until requests finish.

`DeliveryError.progress` distinguishes confirmed sends from an uncertain request. Timeout does not prove rejection. Do not blindly retry a send or a domain action: reconcile or wait for a new invocation. Known rejections and `message is not modified` have separate handling. A primary exception must not be hidden by fallback acknowledgements or guidance.

## Callbacks and managed cards

Raw `@callback(Payload)` projects validated native `CallbackData` fields into typed parameters. Use explicit `ctx.answer`, `ctx.edit` or `ctx.reply`; a returned string is not guessed to mean an alert or an edit. `ack="manual"` transfers acknowledgement ownership to an existing native handler. Otherwise normal completion supplies an empty acknowledgement only if none was attempted.

Use a managed card when an action should reload and redraw the same UI:

```python
@card
async def board(self, ctx: Context, game: str) -> Card:
    record = await self.games.get(game)
    return Card(record.text, buttons=[[Button("Vote", self.vote, option=1)]])


@action(card="board")
async def vote(self, ctx: CallbackContext, game: str, option: int) -> None:
    await self.games.vote(game, actor=ctx.user.id, option=option)
    await ctx.answer("Saved")
```

`await show(ctx, self.board, game=id)` opens it. Renderer arguments carry into buttons; button arguments add or replace values. Pass short application IDs, not serialized state: callback payloads have a 64-byte bound. Valid typed payloads are **not authorization**. The service must check current actor, origin, ownership, expiry and revision.

Callback validators must be pure and preserve the encoded argument value. Normalize values before building a button; a transforming validator is rejected so repeated validation cannot change which record an action addresses. Renderer defaults are included in button arguments, and runtime card locks belong to each feature instance.

Renderers reload current state and have no domain side effects. Actions serialize per UI in the process; application transactions/CAS provide durable concurrency. Successful actions refresh by default. `CardRefreshError` means the action completed but presentation failed; retry only rendering. `refresh=False` supports explicit barriers such as delivering feedback's exact preview before enabling submission.

Read-only cards may choose `ack="early", coalesce=True`: acknowledge before waiting, and drop overlapping refreshes for that UI. Early acknowledgement gives up a later alert result. Do not coalesce votes, payments or other mutations that must each run.

## Conversations

```python
@step("title", draft=StickerDraft)
async def title(self, ctx: MessageContext, draft: StickerDraft) -> None:
    await self.stickers.create(draft, title=ctx.message.text)
    await leave(ctx)


# After confirming the prompt's delivery:
await enter(ctx, self.title, draft)
```

Steps persist named destinations and JSON-compatible Pydantic drafts through the host's aiogram FSM storage. There is no replay engine or second conversation database. `enter` starts from no state or advances within the same feature; cancel or clear the previous workflow explicitly before changing features. `leave` removes TeleForge's draft while preserving unrelated FSM data and refuses to clear a non-TeleForge state. Incompatible drafts fail explicitly; the application owns migrations/reset policy.

Native FSM data and state updates are separate writes, not a transaction. A failed transition may leave a mismatched envelope; the step rejects it rather than running with another step's draft. The host owns recovery and must configure storage namespaces/key builders to isolate bots and business connections. Direct-message topics require an application conversation adapter; they cannot use the default forum-topic key safely.

Standalone apps default to `USER_IN_TOPIC` and local event isolation. Embedded hosts must supply state scoped to the actual bot, chat, actor and forum topic. Inline/inaccessible callbacks cannot invent that scope. Commands default to `StateFilter(None)`. Put `/cancel` before catch-all steps, with an explicit state filter; other commands remain input during the conversation. Conversations are opt-in.

## Jobs, HTTP and persistence

`@job("name", payload=Model)` and `bind_jobs(app, adapter)` expose validated methods to the application's worker under `feature_key.name`; renaming the Python method does not change that durable identity. Enqueue with the application's transaction API. TeleForge does not provide atomic state-plus-enqueue, exactly-once execution, leases, retry policy or retention. Those belong to the durable worker and repository. MSU's worker feature owns its existing worker lifecycle without re-registering reminder/game handlers.

`@web("POST", "/path")` and `bind_web(app, adapter)` attach ordinary bound request methods to a host HTTP router. Auth, body limits, request services, transactions and native responses remain with that host. Shared services can serve Telegram, jobs and HTTP; a live request transaction must never live on shared `self`.

## Composition, inheritance and diagnostics

`App().include(feature)` preserves feature order. `build_router()` returns a fresh native router for an existing dispatcher; the host wraps its runtime in `async with app.lifespan()`. `create_dispatcher()`/`run_polling()` own standalone startup, feature lifespans, shutdown and FSM cleanup. Pass `close_bot_session=True` only when transferring session ownership to polling. Resource factories unwind in reverse order, including partial startup failures.

Standalone shutdown closes update admission, drains for `drain_timeout` (30 seconds), cancels remaining updates and joins for `cancel_timeout` (5 seconds) before closing resources. `DrainTimeout` leaves resources open rather than closing clients beneath a handler that refuses cancellation; the owner must retry shutdown or terminate the process. An active handler cannot call `app.aclose()` itself. Embedded hosts must stop admission and drain their own updates before leaving the feature lifespan.

Override `Feature.lifespan` for resources/background workers; stop and join them before returning. Imports and constructors must not perform network work. App data is injected by name; invocation-specific middleware data wins. Native aiogram middleware remains the injection and instrumentation mechanism.

Declarations compile after class creation. An undecorated override retains its inherited route and position. A new decorator replaces it; `@disable` removes it. Give reusable subclasses explicit stable feature keys. Signatures and direct calls remain ordinary Python.

```sh
PYTHONPATH=packages/teleforge/examples uv run --no-sync teleforge inspect quickstart:make_app --json
PYTHONPATH=packages/teleforge/examples uv run --no-sync teleforge check quickstart:make_app
uv run --no-sync pytest -q packages/teleforge/tests
```

Factories must be side-effect-free. The manifest shows identities, signatures, source locations, policies and dynamic filter names; dependency values are omitted. Inspection cannot prove authorization, provider availability or storage correctness. Exercise those through real service fixtures and native dispatch tests.
