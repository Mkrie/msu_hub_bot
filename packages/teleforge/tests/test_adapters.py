from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from teleforge.app import App
from teleforge.feature import Feature
from teleforge.jobs import JobHandler, bind_jobs, job
from teleforge.web import bind_web, web


class Delivery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: int


class Reminders(Feature, key="reminders"):
    def __init__(self) -> None:
        self.records: list[tuple[int, str]] = []

    @job("deliver", payload=Delivery)
    async def deliver(self, payload: Delivery, lease: str) -> None:
        self.records.append((payload.record_id, lease))

    @web("GET", "/reminders")
    async def list_reminders(self, request: object) -> object:
        return request


class Worker:
    def __init__(self) -> None:
        self.handlers: dict[str, JobHandler] = {}
        self.enqueued: list[object] = []

    def register(self, name: str, handler: JobHandler) -> None:
        if name in self.handlers:
            raise ValueError("duplicate job")
        self.handlers[name] = handler


class Router:
    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Callable[..., Any]]] = []

    def add_route(self, method: str, path: str, handler: Callable[..., Any]) -> None:
        self.routes.append((method, path, handler))


async def test_job_binding_has_no_enqueue_and_passes_typed_payload_and_native_lease() -> None:
    feature, worker = Reminders(), Worker()
    keys = bind_jobs(App(feature), worker)
    assert keys == ("reminders.deliver",)
    assert worker.enqueued == [] and feature.records == []
    await worker.handlers[keys[0]]({"record_id": 12}, lease="application-owned")
    assert feature.records == [(12, "application-owned")]
    assert worker.enqueued == []


@pytest.mark.parametrize("payload", [{"record_id": "12"}, {"record_id": True}, {"record_id": 1, "unknown": 1}])
async def test_worker_rejects_invalid_payload_before_application_side_effect(payload: Mapping[str, object]) -> None:
    feature, worker = Reminders(), Worker()
    bind_jobs(App(feature), worker)
    with pytest.raises(ValidationError):
        await worker.handlers["reminders.deliver"](payload, lease="native")
    assert feature.records == []


async def test_http_adapter_keeps_native_request_and_response_identity() -> None:
    feature, router = Reminders(), Router()
    app = App(feature)
    bind_web(app, router)
    assert len(router.routes) == 1
    method, path, handler = router.routes[0]
    assert (method, path) == ("GET", "/reminders")
    request = object()
    assert await handler(request) is request
    assert app.build_router().resolve_used_update_types() == []


async def test_job_failure_is_observed_by_application_worker_without_replaying() -> None:
    class Failing(Reminders):
        async def deliver(self, payload: Delivery, lease: str) -> None:
            self.records.append((payload.record_id, lease))
            raise RuntimeError("application outcome")

    feature, worker = Failing(), Worker()
    key = bind_jobs(App(feature), worker)[0]
    with pytest.raises(RuntimeError, match="application outcome"):
        await worker.handlers[key]({"record_id": 1}, lease="one")
    assert feature.records == [(1, "one")]
