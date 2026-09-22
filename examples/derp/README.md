# Derp routing examples

`features.py` contains a small text/inline answer sample and native payment
routing. Construct it with `create_app(answers, payments)`. The answer service
receives the current Telegram message, selected native media and already-selected
model messages. `/ask` accepts attached or replied media with no textual prompt;
empty requests use application guidance. This string-response sample does not
replace Derp's full chat handler, context accounting, deferred tools, hydrated
history or multiple outputs. Those remain in the existing native host routes.

`native.py` is an optional concrete paid-image routing bridge. It imports Derp's
actual `ImageOperationCoordinator`, `DeferredToolApprovalService`,
`DeliveryService`, `DatabaseManager`, models, filters and public handlers. Import
it only in a Derp environment satisfying TeleForge's declared dependencies.
The normal Hub test/import graph does not require Derp.

This bridge targets the paid-operation coordinator architecture developed in
Derp PR29, including deferred image approvals and durable delivery. Derp's legacy
main-branch image handlers use a different service interface; satisfying the
Python/aiogram requirements alone does not supply the required coordinator API.

The supported integration environment uses Python 3.14 and aiogram 3.31; the
authoritative dependency range is in `packages/teleforge/pyproject.toml`. Keep a
separate host environment when evaluating a dependency upgrade, and install
TeleForge into that environment instead of changing the host's existing lock:

```sh
uv pip install --python /path/to/derp/.venv-teleforge/bin/python -e packages/teleforge
```

Construct `NativeImages(image_operations=..., approvals=..., delivery=..., db=...)`
with the existing application services and embed `App(native_images).build_router()`
before the existing native image and tool-approval routers. **Keep both native
routers and their normal middleware installed.** Derp selects model dependencies
by the deepest matched router name. Extend its native plans for the bridge's
message and callback observers, replacing the host's default
`setup_route_dependencies` call with this single middleware installation:

```python
from derp.handlers.image import router as image_router
from derp.handlers.tool_approvals import router as tool_approval_router
from derp.middlewares.route_dependencies import (
    ROUTE_DEPENDENCY_PLANS,
    RouteDependencyKey,
    RouteDependencyMiddleware,
    RouteDependencyPlan,
    RouteEvent,
)
from teleforge import App

app = App(native_images)
plans = dict(ROUTE_DEPENDENCY_PLANS)
for event in (RouteEvent.MESSAGE, RouteEvent.CALLBACK_QUERY):
    plans[RouteDependencyKey(event, native_images.key)] = RouteDependencyPlan(models=True)
route_dependencies = RouteDependencyMiddleware(db, plans)
for event in RouteEvent:
    dispatcher.observers[event.value].middleware(route_dependencies)
dispatcher.include_routers(app.build_router(), image_router, tool_approval_router)
```

Here `image_router` and `tool_approval_router` are the existing native routers
from `derp.handlers.image` and `derp.handlers.tool_approvals`. Retain the host's
other router registrations. Do not also call `setup_route_dependencies`: this
mapping extends every existing plan and loads models in the native short read
session, without holding that session around provider execution.

The bridge owns only these selected entrypoints:

| Entry | Existing application handler |
| --- | --- |
| Image creation aliases and native hashtag grammar | `handle_imagine` |
| Image editing aliases and native hashtag grammar | `handle_edit` |
| `ImageApprovalCallback` with the `RUN` action | `approve_image_tool` |
| `DeliveryResendCallback` | `resend_image_delivery` |

Cancel, style, personal-funding decisions, malformed-token fallbacks and every
other native callback remain owned by the host routers. The host still injects
native `user_model` and `chat_model` per invocation, plus its optional
`actor_role_resolver` and concrete `commerce_policy`. Database UUIDs, Telegram
identities and the policy instance cross unchanged. Omitted commerce policy
retains Derp's closed-intake default. Image commands retain the native
`upload_photo` chat-action flag with its two-second initial delay, interpreted
by the host's existing `ChatActionMiddleware`.
The native handlers own callback acknowledgement, so both bridge callbacks use
manual acknowledgement and do not add another answer or presentation.

The bridge preserves Derp's existing input extraction, immutable quote and
approval persistence, operation identity, execution claims, saved result,
settlement, progress and delivery recovery. It introduces no second operation
state machine or transcript schema. Uncertain delivery and explicit resend use
Derp's own typed outcomes and authorization. Native i18n, funding, expiry,
known-provider-failure and refund decisions remain with those handlers.

This is a routing compatibility example, not evidence that wrapping a native
handler reduces total application code. Production adoption still requires the
chosen host dependency upgrade and real PostgreSQL settlement tests. An offline
native journey with synthetic storage/provider/Telegram edges cannot establish
financial migration or deployment readiness.

The optional `bind_jobs(app, worker)` convenience registers
`derp.commerce.recover-payment` for the simple commerce sample. The application
must commit payment acceptance and recovery enqueue together; its worker owns
claims, retries and transaction context. Duplicate payments and repeated job
delivery still reach that application boundary. Inline answers preserve their
native inline-message identity and do not invent a destination chat.

Run the normal offline sample checks with:

```sh
uv run --no-sync pytest -q tests/test_teleforge_derp.py
```

Validate the optional bridge separately with the actual Derp package and its
native tests under a supported environment. Use synthetic provider, persistence
and Telegram edges; never import research checkout runtime state into Hub tests
or initialize live provider clients for validation.
