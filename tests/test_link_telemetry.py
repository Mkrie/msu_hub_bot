"""Source opt-in, metric cardinality and task isolation use real OTLP payloads."""

import asyncio

import pytest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest

from msu_hub_bot.providers.link_diagnostics import LinkDiagnostic, LinkReason, LinkStage
from msu_hub_bot.providers.link_source import source_metadata
from msu_hub_bot.telemetry import Boundary, MediaKind, MediaReason, Provider, Telemetry
from telemetry_helpers import Capture, config


def attributes(items):
    return {item.key: getattr(item.value, item.value.WhichOneof("value")) for item in items}


def metrics(capture, name):
    return [
        (attributes(point.attributes), point.as_int)
        for message in capture.messages()
        if isinstance(message, ExportMetricsServiceRequest)
        for resource in message.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.sum.data_points
    ]


@pytest.mark.parametrize(
    "url,expected,scope",
    [
        ("https://fixupx.com/example/status/123/video/1?secret=CANARY#CANARY", "https://x.com/i/web/status/123/video/1", "post"),
        ("https://youtu.be/abcdefghijk?t=20&token=CANARY", "https://www.youtube.com/watch?v=abcdefghijk&t=20", "post"),
        ("https://youtube.com/shorts/abcdefghijk?si=CANARY", "https://www.youtube.com/watch?v=abcdefghijk", "post"),
        ("https://instagram.com/example/reels/ABCDE/?igsh=CANARY", "https://www.instagram.com/reel/ABCDE/", "post"),
        ("https://vt.tiktok.com/EXAMPLE/?token=CANARY", "https://vt.tiktok.com/EXAMPLE", "post"),
        ("https://tiktok.com/@example/video/123?token=CANARY", "https://www.tiktok.com/@example/video/123", "post"),
        ("https://vk.ru/example?w=wall-123_456&access_key=CANARY", "https://vk.com/wall-123_456", "post"),
        ("https://vimeo.com/123?password=CANARY", "https://vimeo.com/123", "post"),
        ("https://reddit.com/r/example/comments/abc123/CANARY?token=CANARY", "https://reddit.com/r/example/comments/abc123", "post"),
        ("https://example.org/private/CANARY?api_key=CANARY", "https://example.org/", "origin"),
        ("https://youtube.com/watch?v=CANARY", "https://youtube.com/", "origin"),
        ("https://youtube.com/watch?v=abcdefghijk&v=zyxwvutsrqp", "https://youtube.com/", "origin"),
        ("https://youtu.be/extra/abcdefghijk", "https://youtu.be/", "origin"),
        ("https://instagram.com/CANARY", "https://instagram.com/", "origin"),
    ],
)
def test_source_identity_is_reconstructed_without_tracking_or_arbitrary_paths(url, expected, scope):
    result = source_metadata(url)
    assert result == {"link.source_url": expected, "link.url_scope": scope}
    assert "CANARY" not in str(result) and len(expected) <= 160


@pytest.mark.parametrize(
    "url",
    [
        "https://username:CANARY@youtube.com/watch?v=abcdefghijk",
        "https://localhost/private/CANARY",
        "https://example.internal/CANARY",
        "https://127.0.0.1/CANARY",
        "https://[::1]/CANARY",
        "https://example.org:8443/CANARY",
        "https://example.org:invalid/CANARY",
        "https://video.twimg.com/CANARY.mp4?signature=CANARY",
        "https://something.googlevideo.com/CANARY",
        "https://example.ibytedtos.com/CANARY",
        "https://instagram.com/\nCANARY",
        "https://youtube.com/?" + "&".join(f"x={number}" for number in range(101)),
        "https://" + "a" * 200 + ".example.com/CANARY",
        "not a url",
    ],
)
def test_private_or_malformed_sources_are_omitted(url):
    assert source_metadata(url) == {}


async def test_unsampled_link_records_keep_sources_and_recovery_without_metric_identifiers(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    capture = Capture()
    telemetry = Telemetry(config(sample_rate=0), transport=capture)
    await telemetry.start()
    try:
        with telemetry.context(user_id=42, chat_id=-10042, message_id=20):
            with telemetry.link_preview("https://youtu.be/abcdefghijk?token=CANARY", Provider.YOUTUBE) as attempt:
                attempt.link_diagnostics(
                    [
                        LinkDiagnostic(LinkStage.VIDEO, LinkReason.HTTP_ERROR, 50, 403),
                        LinkDiagnostic(LinkStage.IMAGE, LinkReason.OK, 25),
                    ]
                )
                attempt.link_result(LinkStage.DELIVERY, LinkReason.READY)
            with telemetry.link_preview("https://vt.tiktok.com/EXAMPLE?secret=CANARY", Provider.TIKTOK) as attempt:
                attempt.link_result(LinkStage.EXTRACT, LinkReason.UNAVAILABLE)
    finally:
        await telemetry.close()
    assert not capture.spans()
    logs = [(record.body.string_value, attributes(record.attributes)) for record in capture.logs()]
    terminals = [attrs for name, attrs in logs if name == "bot.link.completed"]
    assert len(terminals) == 2
    assert {(item["provider"], item["outcome"]) for item in terminals} == {("youtube", "success"), ("tiktok", "unavailable")}
    failure = next(attrs for name, attrs in logs if name == "bot.link.step.failed")
    assert failure["http.response.status_code"] == 403 and failure["link.stage"] == "video"
    assert failure["link.source_url"] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert all(item["telegram.user_id"] == 42 and item["telegram.chat_id"] == -10042 for _, item in logs)
    assert sum(count for _, count in metrics(capture, "bot.links.attempts")) == 2
    assert sum(count for _, count in metrics(capture, "bot.links.steps")) == 2
    for metric_name in ("bot.links.attempts", "bot.links.steps"):
        assert all(set(attrs) == {"provider", "link.stage", "link.reason", "outcome"} for attrs, _ in metrics(capture, metric_name))
    for signal, payload in capture.payloads:
        if signal == "metrics":
            assert b"https://" not in payload and b"telegram.user_id" not in payload
    assert "CANARY" not in capture.serialized()


async def test_concurrent_sources_do_not_escape_to_other_previews_or_jobs(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    capture = Capture()
    telemetry = Telemetry(config(), transport=capture)
    arrived = 0
    barrier = asyncio.Event()

    async def child():
        with telemetry.operation(Boundary.JOB, "background"):
            pass

    async def preview(identifier):
        nonlocal arrived
        with telemetry.context(user_id=identifier):
            with telemetry.link_preview(f"https://vimeo.com/{identifier}", Provider.YTDLP) as attempt:
                arrived += 1
                if arrived == 2:
                    barrier.set()
                await barrier.wait()
                with telemetry.operation(Boundary.PROVIDER, "links.extract", provider=Provider.YTDLP):
                    await asyncio.sleep(0)

                # The late-task owner guard must also reject accidental task inheritance.
                async def borrowed():
                    attempt.link_result(LinkStage.DELIVERY, LinkReason.UNEXPECTED)
                    attempt.link_diagnostics([LinkDiagnostic(LinkStage.VIDEO, LinkReason.HTTP_ERROR, 1, 403)])
                    await child()

                await asyncio.create_task(borrowed())
                await asyncio.create_task(child(), context=telemetry.job_context())
                await child()
                attempt.link_result(LinkStage.DELIVERY, LinkReason.READY)
            attempt.link_result(LinkStage.DELIVERY, LinkReason.UNEXPECTED)
            attempt.link_diagnostics([LinkDiagnostic(LinkStage.VIDEO, LinkReason.HTTP_ERROR, 1, 403)])
            await child()

    await telemetry.start()
    try:
        await asyncio.gather(preview(41), preview(42))
    finally:
        await telemetry.close()
    for span in capture.spans():
        attrs = attributes(span.attributes)
        if attrs["operation"] == "background":
            assert "link.source_url" not in attrs
        else:
            assert attrs["link.source_url"] == f"https://vimeo.com/{attrs['telegram.user_id']}"
    assert len(metrics(capture, "bot.links.steps")) == 0
    assert metrics(capture, "bot.links.attempts") == [
        (
            {
                "provider": "ytdlp",
                "link.stage": "delivery",
                "link.reason": "ready",
                "outcome": "success",
            },
            2,
        )
    ]


@pytest.mark.parametrize("error,reason", [(TimeoutError("CANARY"), "timeout"), (asyncio.CancelledError("CANARY"), "cancelled")])
async def test_error_or_cancellation_closes_source_scope(monkeypatch, error, reason):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    capture = Capture()
    telemetry = Telemetry(config(sample_rate=0), transport=capture)
    await telemetry.start()
    try:
        with pytest.raises(type(error)):
            with telemetry.link_preview("https://vimeo.com/123?token=CANARY", Provider.YTDLP) as attempt:
                attempt.link_result(LinkStage.EXTRACT, LinkReason.READY)
                raise error
        with telemetry.operation(Boundary.JOB, "background"):
            raise RuntimeError("CANARY")
    except RuntimeError:
        pass
    finally:
        await telemetry.close()
    logs = [(record.body.string_value, attributes(record.attributes)) for record in capture.logs()]
    terminal = next(attrs for name, attrs in logs if name == "bot.link.completed")
    assert terminal["link.reason"] == reason
    assert terminal["link.source_url"] == "https://vimeo.com/123"
    job = next(attrs for name, attrs in logs if name == "bot.operation.failed")
    assert "link.source_url" not in job
    assert "CANARY" not in capture.serialized()


async def test_x_media_omissions_have_source_step_diagnostics_without_another_terminal(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    capture = Capture()
    telemetry = Telemetry(config(sample_rate=0), transport=capture)
    await telemetry.start()
    try:
        with telemetry.link_preview("https://x.com/example/status/123?tracking=CANARY", Provider.FXEMBED) as preview:
            with telemetry.operation(Boundary.MEDIA, "x.media.prepare", provider=Provider.FXEMBED) as media:
                media.media_asset(MediaKind.VIDEO, MediaReason.HTTP_ERROR, attempts=1, duration=0.05)
                media.media_asset(MediaKind.PHOTO, MediaReason.READY, attempts=1, duration=0.05)
            preview.link_result(LinkStage.DELIVERY, LinkReason.READY)
    finally:
        await telemetry.close()
    steps = metrics(capture, "bot.links.steps")
    assert sum(count for _, count in steps) == 2
    assert sum(count for _, count in metrics(capture, "bot.links.attempts")) == 1
    failure = next(attributes(log.attributes) for log in capture.logs() if log.body.string_value == "bot.link.step.failed")
    assert failure["link.source_url"] == "https://x.com/i/web/status/123"
    assert failure["link.reason"] == "http_error" and failure["link.stage"] == "video"
    assert "CANARY" not in capture.serialized()
