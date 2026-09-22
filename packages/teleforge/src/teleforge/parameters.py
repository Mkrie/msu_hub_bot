"""One static source per parameter, shared by inspection and invocation."""

import inspect
import io
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import UnionType
from typing import (
    Annotated,
    Any,
    Literal,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
    get_protocol_members,
    is_protocol,
)

from aiogram import Bot
from aiogram.types import (
    Animation,
    Audio,
    CallbackQuery,
    Document,
    Message,
    PhotoSize,
    Sticker,
    Update,
    Video,
    VideoNote,
    Voice,
)
from pydantic import BaseModel

from .context import CallbackContext, Context, MessageContext
from .declarations import Declaration
from .inputs import (
    Argument,
    DocumentInput,
    ImageInput,
    MediaInput,
    TextInput,
    VideoInput,
    annotations_for,
    ordinary,
    representation,
)
from .issues import ConfigurationError

type ParameterSource = Literal[
    "context",
    "event",
    "bot",
    "dependency",
    "callback_payload",
    "draft",
    "argument",
    "text",
    "media",
    "native",
    "job_payload",
]
_RESERVED = {"ctx", "context", "event", "message", "query", "bot", "state", "callback_data"}
_MEDIA = (ImageInput, VideoInput, DocumentInput, MediaInput)


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    annotation: Any
    default: Any
    source: ParameterSource
    declaration: object = None


@dataclass(frozen=True, slots=True)
class ParameterPlan:
    parameters: tuple[Parameter, ...]
    command: bool = False


@dataclass(frozen=True, slots=True)
class ParameterIssue:
    code: str
    message: str


def _media_annotation(annotation: Any, rule: ImageInput | VideoInput | DocumentInput | MediaInput) -> bool:
    value = representation(annotation)
    if value in (bytes, io.BytesIO, Path):
        return True
    if getattr(value, "__module__", "") == "PIL.Image" and getattr(value, "__name__", "") == "Image":
        return isinstance(rule, ImageInput) or isinstance(rule, MediaInput) and rule.kinds == ("image",)
    kinds = (
        rule.kinds
        if isinstance(rule, MediaInput)
        else ("image",)
        if isinstance(rule, ImageInput)
        else ("video",)
        if isinstance(rule, VideoInput)
        else ("document",)
    )
    allowed: set[type[Any]] = set()
    for kind in kinds:
        allowed.update(
            {
                "image": (PhotoSize, Sticker, Document),
                "video": (Video, Animation, VideoNote, Sticker, Document),
                "document": (Document,),
                "audio": (Audio, Voice),
            }[kind]
        )
    choices = get_args(value) if get_origin(value) in (Union, UnionType) else (value,)
    # A broad native-media union is useful with a narrower acquisition rule;
    # every member must be native, and at least one must be selectable.
    native = {PhotoSize, Sticker, Document, Video, Animation, VideoNote, Audio, Voice}
    meaningful = tuple(choice for choice in choices if choice is not type(None))
    return (
        bool(meaningful)
        and all(choice in native for choice in meaningful)
        and any(choice in allowed for choice in meaningful)
    )


def _accepts_type(annotation: Any, actual: type[Any]) -> bool:
    annotation = representation(annotation)
    if annotation is Any:
        return True
    if get_origin(annotation) in (Union, UnionType):
        return any(_accepts_type(choice, actual) for choice in get_args(annotation))
    return isinstance(annotation, type) and not is_protocol(annotation) and issubclass(actual, annotation)


def compile_parameters(
    handler: Callable[..., Any],
    declaration: Declaration,
    *,
    annotations: Mapping[str, Any] | None = None,
    payload_fields: Mapping[str, Any] | None = None,
) -> tuple[ParameterPlan, tuple[ParameterIssue, ...]]:
    """Compile without evaluating middleware, factories or dependency values."""
    signature = inspect.signature(handler)
    hints = annotations if annotations is not None else annotations_for(handler)
    issues: list[ParameterIssue] = []
    parameters: list[Parameter] = []

    def error(code: str, message: str) -> None:
        issues.append(ParameterIssue(code, message))

    fields = (
        {name: field.annotation for name, field in declaration.payload.model_fields.items()}
        if declaration.payload is not None
        else dict(payload_fields or {})
    )
    for name in fields:
        if name in _RESERVED:
            error("payload-reserved", f"Callback field '{name}' is reserved for invocation context")
        if name in declaration.inputs:
            error("payload-input", f"Callback field '{name}' cannot also acquire a message input")
        if name in hints and hints[name] != fields[name]:
            error("payload-type", f"Parameter '{name}' must match its CallbackData field type")
    for name in declaration.inputs:
        if name not in signature.parameters:
            error("input-name", f"Input '{name}' is not a method parameter")
    draft = declaration.metadata.get("draft")
    if "step" in declaration.metadata:
        if not isinstance(draft, type) or not issubclass(draft, BaseModel):
            error("step-draft", "A step must declare a Pydantic draft model")
        if "draft" not in signature.parameters:
            error("step-draft", "A step method needs its declared 'draft' parameter")
        elif hints.get("draft") != draft:
            error("step-draft", "The 'draft' parameter must match its declared draft model")
    job_argument = declaration.metadata.get("argument") if declaration.kind == "job" else None
    if declaration.kind == "job":
        model = declaration.metadata.get("payload")
        if not isinstance(model, type) or not issubclass(model, BaseModel):
            error("job-payload", "A job must declare a Pydantic payload model")
        if not isinstance(job_argument, str) or job_argument not in signature.parameters:
            error("job-payload", "A job method needs its declared payload parameter")
        elif hints.get(job_argument) != model:
            error("job-payload", f"Parameter '{job_argument}' must match its declared job payload model")

    event_field = Update.model_fields.get(declaration.event or "")
    event_type = representation(event_field.annotation) if event_field is not None else None
    for name in ("ctx", "context"):
        if name not in hints or declaration.kind in {"job", "web"}:
            continue
        context_type = representation(hints[name])
        context_types = get_args(context_type) if get_origin(context_type) in (Union, UnionType) else (context_type,)
        if any(not isinstance(item, type) or not issubclass(item, Context) for item in context_types):
            error("context-type", f"Parameter '{name}' must use Context or an event-specific Context")
        elif event_type is not None:
            actual_context = (
                MessageContext if event_type is Message else CallbackContext if event_type is CallbackQuery else Context
            )
            if not _accepts_type(context_type, actual_context):
                error("context-type", f"Parameter '{name}' cannot receive the context for '{declaration.event}'")

    for name, parameter in signature.parameters.items():
        if name in {"self", "cls"}:
            continue
        annotation = hints.get(name, parameter.annotation)
        rule = declaration.inputs.get(name)
        if parameter.kind not in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY):
            error("parameter-kind", f"Parameter '{name}' must be an ordinary named parameter")
        if name not in hints:
            error("parameter-type", f"Parameter '{name}' needs a type annotation")
        if rule is not None:
            if name in _RESERVED or name == "draft" and "step" in declaration.metadata:
                error("input-reserved", f"Input '{name}' is reserved for invocation context")
            if not isinstance(rule, Argument | TextInput | ImageInput | VideoInput | DocumentInput | MediaInput):
                error("input-rule", f"Input '{name}' needs an Argument, TextInput or media declaration")
            elif isinstance(rule, TextInput) and representation(annotation) is not str:
                error("input-type", f"Text input '{name}' requires a str annotation")
            elif isinstance(rule, Argument):
                if declaration.kind != "command":
                    error("argument-source", f"Argument '{name}' requires a command entrypoint")
                if not ordinary(annotation):
                    error("input-type", f"Argument '{name}' requires an ordinary scalar annotation")
            elif isinstance(rule, _MEDIA) and not _media_annotation(annotation, rule):
                error(
                    "input-type",
                    f"Media input '{name}' requires compatible native media, bytes, BytesIO, Path or Image",
                )

        source: ParameterSource
        if name in {"ctx", "context"} and declaration.kind not in {"job", "web"}:
            source = "context"
        elif name == job_argument:
            source = "job_payload"
        elif declaration.kind in {"job", "web"}:
            source = "native"
        elif name == "draft" and "step" in declaration.metadata:
            source = "draft"
        elif name in fields:
            source = "callback_payload"
        elif isinstance(rule, TextInput):
            source = "text"
        elif isinstance(rule, _MEDIA):
            source = "media"
        elif declaration.kind == "card":
            source = "dependency" if parameter.kind == parameter.KEYWORD_ONLY else "native"
        elif name in {"event", "message", "query"}:
            source = "event"
        elif name == "bot":
            source = "bot"
        elif name in {"state", "callback_data"}:
            source = "dependency"
        elif isinstance(rule, Argument):
            source = "argument"
        elif declaration.kind == "command" and parameter.kind == parameter.POSITIONAL_OR_KEYWORD:
            source = "argument"
            if not ordinary(annotation):
                error("parameter-source", f"Command dependency '{name}' must be keyword-only, or declare its input")
        elif "card_action" in declaration.metadata and parameter.kind == parameter.POSITIONAL_OR_KEYWORD:
            source = "callback_payload"
        else:
            source = "dependency"
        if source == "event" and event_type is not None and not _accepts_type(annotation, event_type):
            error("event-type", f"Parameter '{name}' cannot receive native event '{declaration.event}'")
        if source == "bot" and not _accepts_type(annotation, Bot):
            error("bot-type", f"Parameter '{name}' must accept an aiogram Bot")
        if (
            name == "callback_data"
            and declaration.payload is not None
            and not _accepts_type(annotation, declaration.payload)
        ):
            error("payload-type", "Parameter 'callback_data' must accept the declared CallbackData model")
        parameters.append(Parameter(name, annotation, parameter.default, source, rule))
    return ParameterPlan(tuple(parameters), command=declaration.kind == "command"), tuple(issues)


def dependency_matches(annotation: Any, value: Any) -> bool:
    """Check compatibility without validators, coercion, copies or service construction."""
    if isinstance(annotation, TypeAliasType):
        return dependency_matches(annotation.__value__, value)
    if annotation is Any or annotation is inspect.Parameter.empty:
        return True
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Annotated:
        return dependency_matches(args[0], value)
    if origin in (Union, UnionType):
        return any(dependency_matches(choice, value) for choice in args)
    if annotation is None or annotation is type(None):
        return value is None
    if origin is Literal:
        return any(type(value) is type(choice) and value == choice for choice in args)
    if is_protocol(annotation):
        return all(
            inspect.getattr_static(value, name, inspect.Parameter.empty) is not inspect.Parameter.empty
            for name in get_protocol_members(annotation)
        )
    target = origin or annotation
    if target in (int, float, bool, str, bytes):
        return type(value) is target
    if not isinstance(target, type):
        raise ConfigurationError("Dependency annotation must support a native runtime compatibility check")
    if not isinstance(value, target):
        return False
    if origin in (list, set, frozenset) and args and isinstance(value, list | set | frozenset):
        return all(dependency_matches(args[0], item) for item in value)
    if origin in (dict, Mapping) and args and isinstance(value, Mapping):
        return all(
            dependency_matches(args[0], key) and dependency_matches(args[1], item) for key, item in value.items()
        )
    if origin is Sequence and args and isinstance(value, Sequence):
        return all(dependency_matches(args[0], item) for item in value)
    if origin is tuple and args and isinstance(value, tuple):
        if len(args) == 2 and args[1] is Ellipsis:
            return all(dependency_matches(args[0], item) for item in value)
        return len(value) == len(args) and all(
            dependency_matches(kind, item) for kind, item in zip(args, value, strict=True)
        )
    return True


def checked_dependency(annotation: Any, value: Any, name: str) -> Any:
    if not dependency_matches(annotation, value):
        raise ConfigurationError(f"Injected dependency '{name}' does not match its declared type")
    return value
