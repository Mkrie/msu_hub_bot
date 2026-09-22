import inspect
import json

import pytest
from aiogram.filters.callback_data import CallbackData

from teleforge.app import App
from teleforge.context import CallbackContext, MessageContext
from teleforge.declarations import callback, command, declarations_of, disable, event
from teleforge.feature import CompilationError, Feature
from teleforge.inputs import TextInput


class BaseTools(Feature, key="base"):
    @command("one")
    async def first(self, ctx: MessageContext, amount: int = 3) -> str:
        return str(amount)

    @command("two")
    async def second(self, ctx: MessageContext) -> str:
        return "base"


class DerivedTools(BaseTools, key="derived"):
    async def first(self, ctx: MessageContext, amount: int = 5) -> str:
        return str(amount + 1)

    @disable
    async def second(self, ctx: MessageContext) -> str:
        return "still callable"

    @command("three")
    async def third(self, ctx: MessageContext) -> str:
        return "new"


def test_override_preserves_triggers_order_signature_and_base() -> None:
    base = App(BaseTools()).iter_handlers()
    derived = App(DerivedTools()).iter_handlers()
    assert [(item.name, item.declaration.names) for item in derived] == [("first", ("one",)), ("third", ("three",))]
    assert derived[0].signature.parameters["amount"].default == 5
    assert [item.name for item in base] == ["first", "second"]
    assert declarations_of(DerivedTools().first) == declarations_of(BaseTools().first)
    assert not declarations_of(DerivedTools().second)


async def test_decorators_keep_methods_ordinary() -> None:
    feature = DerivedTools()
    assert await feature.first(None, 7) == "8"  # type: ignore[arg-type]
    assert await feature.second(None) == "still callable"  # type: ignore[arg-type]
    assert not hasattr(feature.first, "__wrapped__")
    assert inspect.iscoroutinefunction(feature.first)


def test_explicit_redeclaration_replaces_base_without_moving_route() -> None:
    class Different(BaseTools):
        @command("replacement")
        async def first(self, ctx: MessageContext, amount: int = 3) -> str:
            return "changed"

    handlers = App(Different()).iter_handlers()
    assert [(item.name, item.declaration.names) for item in handlers] == [
        ("first", ("replacement",)),
        ("second", ("two",)),
    ]


def test_errors_are_aggregate_source_located_and_do_not_open_lifespan() -> None:
    class Invalid(Feature):
        @command("broken", absent=TextInput())
        async def broken(self, text: str) -> str:
            return text

        @event("misspelled_event")
        async def invalid(self, ctx: MessageContext) -> None:
            pass

    app = App(Invalid(), Invalid())
    errors = app.check()
    assert {error.code for error in errors} == {"input-name", "event-name", "duplicate-feature"}
    assert all(error.source.line for error in errors if error.handler)
    with pytest.raises(CompilationError):
        app.build_router()


class Choice(CallbackData, prefix="choice"):
    item: int


def test_callback_payload_mismatch_is_a_compile_error() -> None:
    class Invalid(Feature):
        @callback(Choice)
        async def choose(self, ctx: CallbackContext, item: str) -> None:
            pass

    assert [error.code for error in App(Invalid()).check()] == ["payload-type"]


def test_manifest_is_structural_and_does_not_serialize_dependencies() -> None:
    class Secret:
        def __repr__(self) -> str:
            raise AssertionError("Must not inspect service values")

    app = App(DerivedTools(), data={"service": Secret()})
    manifest = app.inspect()
    assert manifest["dependencies"] == ["service"]
    assert "derived.first" in json.dumps(manifest)
    assert app._stack is None


def test_router_build_is_repeatable_for_embedding() -> None:
    app = App(BaseTools())
    first, second = app.build_router(), app.build_router()
    assert first is not second
    assert first.sub_routers[0] is not second.sub_routers[0]
    assert first.sub_routers[0].message.handlers[0].callback.__name__ == "first"


def test_multiple_entrypoints_keep_source_declaration_order() -> None:
    class Multi(Feature):
        @event("message")
        @event("edited_message")
        async def incoming(self, ctx: MessageContext) -> None:
            pass

    handlers = App(Multi()).iter_handlers()
    assert [item.declaration.event for item in handlers] == ["message", "edited_message"]
    assert len({item.key for item in handlers}) == 2
