"""Application composition on native aiogram routers and dispatcher lifecycle."""

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import Any, Self, cast

from aiogram import Bot, Dispatcher, Router
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.dispatcher.middlewares.user_context import EVENT_CONTEXT_KEY, EventContext
from aiogram.filters import Command, StateFilter
from aiogram.fsm.middleware import FSMContextMiddleware
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.fsm.strategy import FSMStrategy
from aiogram.types import InaccessibleMessage, TelegramObject, Update

from .binding import adapter_for
from .declarations import NativeFilter
from .feature import CompilationError, CompiledHandler, Diagnostic, Feature, compile_feature

type ResourceFactory = Callable[[], AbstractAsyncContextManager[object]]
type NextHandler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class AdmissionClosed(RuntimeError):
    """The standalone application has stopped accepting Telegram updates."""


class DrainTimeout(RuntimeError):
    """Updates did not release application resources after cancellation."""

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining
        super().__init__(f"{remaining} update task(s) still own application resources")


class _Dispatcher(Dispatcher):
    def __init__(self, app: App, **kwargs: Any) -> None:
        self._app = app
        super().__init__(**kwargs)

    async def feed_update(self, bot: Bot, update: Update, **kwargs: Any) -> Any:
        # Enclose the native dispatcher, including its outer error middleware.
        async with self._app._admit_update():
            return await super().feed_update(bot, update, **kwargs)


class _Workflow(BaseMiddleware):
    def __init__(self, app: App) -> None:
        self.app = app

    async def __call__(self, handler: NextHandler, event: TelegramObject, data: dict[str, Any]) -> Any:
        # Native filter/middleware values take precedence over application defaults.
        for name, value in self.app.data.items():
            data.setdefault(name, value)
        data["_teleforge_media_slots"] = self.app._media_slots
        return await handler(event, data)


class _ScopedFSM(FSMContextMiddleware):
    async def __call__(self, handler: NextHandler, event: TelegramObject, data: dict[str, Any]) -> Any:
        context = data.get(EVENT_CONTEXT_KEY)
        inaccessible = (
            isinstance(event, Update)
            and event.callback_query is not None
            and isinstance(event.callback_query.message, InaccessibleMessage)
        )
        if inaccessible or not isinstance(context, EventContext) or context.chat is None:
            # Native FSM otherwise invents a private chat for chatless events; an
            # inaccessible callback also cannot establish its real topic identity.
            data["fsm_storage"] = self.storage
            return await handler(event, data)
        return await super().__call__(handler, event, data)


class App:
    def __init__(
        self,
        *features: Feature,
        bot: Bot | None = None,
        data: Mapping[str, Any] | None = None,
        media_concurrency: int = 2,
        drain_timeout: float = 30,
        cancel_timeout: float = 5,
    ) -> None:
        if type(media_concurrency) is not int or media_concurrency < 1:
            raise ValueError("media_concurrency must be a positive integer")
        if not all(math.isfinite(value) and value >= 0 for value in (drain_timeout, cancel_timeout)):
            raise ValueError("Update drain timeouts must be finite and nonnegative")
        self.bot = bot
        self.data = dict(data or {})
        self._features: list[Feature] = list(features)
        self._media_slots = asyncio.Semaphore(media_concurrency)
        self._resources: list[ResourceFactory] = []
        self._stack: AsyncExitStack | None = None
        self._configuration_closed = False
        self._lifecycle_lock = asyncio.Lock()
        self._dispatcher: Dispatcher | None = None
        self._fsm_closed = False
        self._accepting = True
        self._updates: dict[asyncio.Task[Any], int] = {}
        self._updates_done = asyncio.Event()
        self._updates_done.set()
        self._drain_timeout, self._cancel_timeout = drain_timeout, cancel_timeout

    @asynccontextmanager
    async def _admit_update(self) -> AsyncIterator[None]:
        if not self._accepting:
            raise AdmissionClosed("The application is shutting down")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Update handling requires an asyncio task")
        self._updates[task] = self._updates.get(task, 0) + 1
        self._updates_done.clear()
        try:
            yield
        finally:
            depth = self._updates[task] - 1
            if depth:
                self._updates[task] = depth
            else:
                del self._updates[task]
            if not self._updates:
                self._updates_done.set()

    async def _drain_updates(self) -> None:
        if not self._updates:
            return
        try:
            async with asyncio.timeout(self._drain_timeout):
                await self._updates_done.wait()
        except TimeoutError:
            for task in tuple(self._updates):
                task.cancel()
            try:
                async with asyncio.timeout(self._cancel_timeout):
                    await self._updates_done.wait()
            except TimeoutError:
                raise DrainTimeout(len(self._updates)) from None

    @property
    def features(self) -> tuple[Feature, ...]:
        return tuple(self._features)

    def include(self, feature: Feature) -> App:
        if self._configuration_closed or self._dispatcher is not None:
            raise RuntimeError("Include features before building the standalone dispatcher or starting the application")
        self._features.append(feature)
        return self

    def resource(self, factory: ResourceFactory) -> App:
        """Own a context-manager factory; ordinary closures wire its dependencies."""
        if self._configuration_closed:
            raise RuntimeError("Register resources before application startup")
        self._resources.append(factory)
        return self

    def _compile(self) -> tuple[tuple[CompiledHandler, ...], tuple[Diagnostic, ...]]:
        handlers: list[CompiledHandler] = []
        errors: list[Diagnostic] = []
        seen: set[str] = set()
        for feature in self.features:
            if feature.key in seen:
                errors.append(
                    Diagnostic("duplicate-feature", "Feature keys must be unique within an application", feature.key)
                )
            seen.add(feature.key)
            compiled, diagnostics = compile_feature(feature)
            handlers.extend(compiled)
            errors.extend(diagnostics)
        return tuple(handlers), tuple(errors)

    def check(self) -> tuple[Diagnostic, ...]:
        return self._compile()[1]

    def inspect(self) -> dict[str, object]:
        handlers, errors = self._compile()
        return {
            "schema_version": 1,
            "features": [{"key": feature.key, "type": type(feature).__qualname__} for feature in self.features],
            "handlers": [handler.as_dict() for handler in handlers],
            "diagnostics": [error.as_dict() for error in errors],
            "dependencies": sorted(self.data),
            "resources": len(self._resources),
        }

    def iter_handlers(self, kind: str | None = None) -> tuple[CompiledHandler, ...]:
        handlers, errors = self._compile()
        if errors:
            raise CompilationError(errors)
        return tuple(item for item in handlers if kind is None or item.declaration.kind == kind)

    def build_router(self) -> Router:
        """Build a fresh native router for embedding; the host owns its lifecycle/FSM."""
        handlers = self.iter_handlers()
        root = Router(name="teleforge")
        for observer in root.observers.values():
            observer.outer_middleware(_Workflow(self))
        for feature in self.features:
            router = Router(name=feature.key)
            for compiled in handlers:
                if compiled.feature is not feature or compiled.declaration.event is None:
                    continue
                declaration = compiled.declaration
                assert declaration.event is not None
                filters = list(declaration.filters)
                if declaration.filter_factory is not None:
                    filters.extend(declaration.filter_factory(feature))
                if declaration.kind == "command":
                    if not any(isinstance(item, StateFilter) for item in filters):
                        filters.insert(0, StateFilter(None))
                    custom_filter = declaration.metadata.get("_command_filter")
                    filters.append(
                        cast(NativeFilter, custom_filter)
                        if custom_filter is not None
                        else Command(*declaration.names, ignore_case=True)
                    )
                flags = dict(declaration.flags)
                flags.setdefault("handler_key", compiled.key)
                flags.setdefault("feature_key", feature.key)
                router.observers[declaration.event].register(adapter_for(compiled), *filters, flags=flags)
            root.include_router(router)
        return root

    def create_dispatcher(
        self,
        *,
        storage: BaseStorage | None = None,
        events_isolation: BaseEventIsolation | None = None,
        fsm_strategy: FSMStrategy = FSMStrategy.USER_IN_TOPIC,
    ) -> Dispatcher:
        if not self._accepting:
            raise AdmissionClosed("A closed application cannot create a dispatcher")
        if self._dispatcher is not None:
            raise RuntimeError("This application already has a standalone dispatcher; use build_router for embedding")
        dispatcher = _Dispatcher(
            self,
            storage=storage if storage is not None else MemoryStorage(),
            events_isolation=events_isolation if events_isolation is not None else SimpleEventIsolation(),
            fsm_strategy=fsm_strategy,
            disable_fsm=True,
            **self.data,
        )
        dispatcher.fsm = _ScopedFSM(dispatcher.fsm.storage, dispatcher.fsm.events_isolation, fsm_strategy)
        dispatcher.update.outer_middleware(dispatcher.fsm)
        # A fresh Dispatcher has only fsm.close here. Workers/resources must stop
        # before storage closes; this app owns that single shutdown boundary.
        dispatcher.shutdown.handlers.clear()
        dispatcher.startup.register(self.start)
        dispatcher.shutdown.register(self.aclose)
        dispatcher.include_router(self.build_router())
        self._dispatcher = dispatcher
        self._fsm_closed = False
        return dispatcher

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if not self._accepting:
                raise AdmissionClosed("A closed application cannot restart")
            if self._stack is not None:
                return
            if self._fsm_closed:
                raise RuntimeError("A closed standalone application cannot restart its storage")
            errors = self.check()
            if errors:
                raise CompilationError(errors)
            self._configuration_closed = True
            stack = AsyncExitStack()
            try:
                for factory in self._resources:
                    await stack.enter_async_context(factory())
                for feature in self.features:
                    await stack.enter_async_context(feature.lifespan(self))
            except BaseException:
                await stack.aclose()
                raise
            self._stack = stack

    async def aclose(self) -> None:
        if asyncio.current_task() in self._updates:
            raise RuntimeError("An update cannot close its own application; request shutdown from the owner")
        async with self._lifecycle_lock:
            self._configuration_closed = True
            self._accepting = False
            # Leave clients open if a running update cannot be joined. The
            # application owner can terminate the process or retry shutdown.
            await self._drain_updates()
            stack, self._stack = self._stack, None
            try:
                if stack is not None:
                    await stack.aclose()
            finally:
                if self._dispatcher is not None and not self._fsm_closed:
                    self._fsm_closed = True
                    await self._dispatcher.fsm.close()

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[App]:
        await self.start()
        try:
            yield self
        finally:
            await self.aclose()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def feed_update(self, bot: Bot, update: Update, **data: Any) -> object:
        dispatcher = self._dispatcher or self.create_dispatcher()
        await self.start()
        return await dispatcher.feed_update(bot, update, **data)

    async def run_polling(self, bot: Bot | None = None, *, close_bot_session: bool = False, **options: Any) -> None:
        selected = bot if bot is not None else self.bot
        if selected is None:
            raise TypeError("Supply a Bot to App or run_polling")
        dispatcher = self._dispatcher or self.create_dispatcher()
        try:
            await dispatcher.start_polling(selected, close_bot_session=False, **options)
        finally:
            await self.aclose()
            if close_bot_session:
                await selected.session.close()
