"""Metric batching preserves totals and does not postpone failure diagnostics."""

import asyncio

import pytest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.metrics.v1.metrics_pb2 import AGGREGATION_TEMPORALITY_CUMULATIVE

from msu_hub_bot.telemetry import Boundary, Telemetry, TelemetryConfig
from telemetry_helpers import Capture, config

CANARY = "SYNTHETIC_PRIVATE_CADENCE_CANARY"


@pytest.fixture(autouse=True)
def local_sdk_capture(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)


def metric_batches(sink):
    return [
        {metric.name: metric for resource in message.resource_metrics for scope in resource.scope_metrics for metric in scope.metrics}
        for message in sink.messages()
        if isinstance(message, ExportMetricsServiceRequest)
    ]


def test_metrics_default_to_one_minute():
    assert TelemetryConfig().interval == 60
    assert TelemetryConfig.from_env({}).interval == 60


@pytest.mark.parametrize("seconds", ["10", "60", "300"])
def test_explicit_metric_interval_is_validated(seconds):
    configured = TelemetryConfig.from_env(
        {"HUB_TELEMETRY_ENABLED": "true", "LOGFIRE_TOKEN": CANARY, "HUB_TELEMETRY_METRICS_INTERVAL_SECONDS": seconds}
    )
    assert configured.export and configured.valid()
    assert configured.interval == float(seconds)


@pytest.mark.parametrize("seconds", ["", "9.9", "300.1", "nan", "inf", "-inf", CANARY])
def test_invalid_metric_interval_disables_export_without_logging_value(seconds, caplog):
    configured = TelemetryConfig.from_env(
        {"HUB_TELEMETRY_ENABLED": "true", "LOGFIRE_TOKEN": CANARY, "HUB_TELEMETRY_METRICS_INTERVAL_SECONDS": seconds}
    )
    assert not configured.export
    assert CANARY not in caplog.text


async def test_unsampled_failure_is_exported_before_metric_deadline_and_shutdown_keeps_totals():
    class Notified(Capture):
        logs_sent = asyncio.Event()

        async def send(self, signal, payload):
            result = await super().send(signal, payload)
            if signal == "logs":
                self.logs_sent.set()
            return result

    sink = Notified()
    telemetry = Telemetry(config(sample_rate=0), {"test.handler"}, transport=sink)
    await telemetry.start()
    try:
        with telemetry.context(user_id=123456, chat_id=-654321):
            with telemetry.operation(Boundary.HANDLER, "test.handler"):
                pass
            with pytest.raises(RuntimeError), telemetry.operation(Boundary.HANDLER, "test.handler"):
                raise RuntimeError(CANARY)
        await asyncio.wait_for(sink.logs_sent.wait(), timeout=1)
        assert sink.spans() == []
        assert [log.body.string_value for log in sink.logs()] == ["bot.operation.failed"]
        assert metric_batches(sink) == []
    finally:
        await telemetry.close()

    metrics = metric_batches(sink)[0]
    counts = metrics["bot.operations"].sum
    durations = metrics["bot.operation.duration"].histogram
    assert counts.aggregation_temporality == durations.aggregation_temporality == AGGREGATION_TEMPORALITY_CUMULATIVE
    assert sum(point.as_int for point in counts.data_points) == sum(point.count for point in durations.data_points) == 2
    assert {
        attribute.value.string_value for point in counts.data_points for attribute in point.attributes if attribute.key == "outcome"
    } == {
        "success",
        "unexpected",
    }
    assert all(not attribute.key.startswith("telegram.") for point in counts.data_points for attribute in point.attributes)
    assert CANARY not in sink.serialized()


async def test_periodic_metrics_do_not_require_logs_or_spans():
    class Notified(Capture):
        metrics_sent = asyncio.Event()

        async def send(self, signal, payload):
            result = await super().send(signal, payload)
            if signal == "metrics":
                self.metrics_sent.set()
            return result

    sink = Notified()
    telemetry = Telemetry(config(sample_rate=0, interval=0.01), {"test.handler"}, transport=sink)
    await telemetry.start()
    try:
        with telemetry.operation(Boundary.HANDLER, "test.handler"):
            pass
        await asyncio.wait_for(sink.metrics_sent.wait(), timeout=1)
        assert not sink.logs() and not sink.spans()
        assert metric_batches(sink)[0]["bot.operations"].sum.data_points[0].as_int == 1
    finally:
        await telemetry.close()


async def test_later_cumulative_export_retains_operations_after_a_dropped_batch():
    class DroppedFirst(Capture):
        async def send(self, signal, payload):
            await super().send(signal, payload)
            return len(self.payloads) != 1

    sink = DroppedFirst()
    telemetry = Telemetry(config(sample_rate=0), {"test.handler"}, transport=sink)
    await telemetry.start()
    try:
        for _ in range(2):
            with telemetry.operation(Boundary.HANDLER, "test.handler"):
                pass
        await telemetry._metrics()
        with telemetry.operation(Boundary.HANDLER, "test.handler"):
            pass
    finally:
        await telemetry.close()

    batches = metric_batches(sink)
    assert [batch["bot.operations"].sum.data_points[0].as_int for batch in batches] == [2, 3]
    assert [batch["bot.operation.duration"].histogram.data_points[0].count for batch in batches] == [2, 3]
    assert all(batch["bot.operations"].sum.aggregation_temporality == AGGREGATION_TEMPORALITY_CUMULATIVE for batch in batches)
