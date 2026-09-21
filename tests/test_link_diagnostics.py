"""Extraction diagnostics distinguish failures without carrying provider content."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict
import json
from pathlib import Path
import signal
import socket
import subprocess
from threading import Barrier
import time
from types import SimpleNamespace

import pytest
import requests
from yt_dlp.networking.exceptions import HTTPError as YdlHTTPError, TransportError
from yt_dlp.utils import DownloadError, ExtractorError

from msu_hub_bot.providers import instagram, link_download, tiktok, youtube
from msu_hub_bot.providers.link_diagnostics import (
    LinkDiagnostic,
    LinkExtraction,
    LinkReason,
    LinkStage,
    collect_link_diagnostics,
    record_link_diagnostic,
)

URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
SECRET = "private response https://cdn.example.test/signed?token=secret"


def test_collector_returns_unchanged_value_and_immutable_fixed_diagnostics():
    value = object()

    def work():
        record_link_diagnostic(LinkStage.EXTRACT, LinkReason.HTTP_ERROR, duration_ms=123, http_status=403)
        return value

    result = collect_link_diagnostics(work)
    assert result.value is value
    assert result.diagnostics == (LinkDiagnostic(LinkStage.EXTRACT, LinkReason.HTTP_ERROR, 123, 403),)
    with pytest.raises(FrozenInstanceError):
        result.diagnostics[0].http_status = 200
    with pytest.raises(FrozenInstanceError):
        result.value = None


@pytest.mark.parametrize(
    "fields",
    [
        {"stage": SECRET},
        {"reason": SECRET},
        {"http_status": "403"},
        {"http_status": True},
        {"http_status": 700},
        {"duration_ms": SECRET},
        {"duration_ms": -1},
        {"duration_ms": 300001},
        {"duration_ms": 1.5},
    ],
)
def test_diagnostics_cannot_hold_arbitrary_provider_strings_or_unbounded_values(fields):
    with pytest.raises(ValueError):
        LinkDiagnostic(**{"stage": LinkStage.EXTRACT, "reason": LinkReason.OK, **fields})


def test_collector_is_bounded_and_does_not_leave_events_for_the_next_call():
    def work():
        for _ in range(200):
            record_link_diagnostic(LinkStage.IMAGE, LinkReason.OK)
        return "done"

    assert len(collect_link_diagnostics(work).diagnostics) == 128
    record_link_diagnostic(LinkStage.EXTRACT, LinkReason.HTTP_ERROR)
    assert collect_link_diagnostics(lambda: None) == LinkExtraction(None, ())


def test_nested_collectors_and_exception_cleanup_preserve_context():
    def inner():
        record_link_diagnostic(LinkStage.REQUEST, LinkReason.OK)

    def outer():
        record_link_diagnostic(LinkStage.EXTRACT, LinkReason.OK)
        result = collect_link_diagnostics(inner)
        assert result.diagnostics == (LinkDiagnostic(LinkStage.REQUEST, LinkReason.OK),)
        return result.value

    assert collect_link_diagnostics(outer).diagnostics == (LinkDiagnostic(LinkStage.EXTRACT, LinkReason.OK),)

    def failure():
        record_link_diagnostic(LinkStage.VIDEO, LinkReason.TIMEOUT)
        raise RuntimeError(SECRET)

    with pytest.raises(RuntimeError):
        collect_link_diagnostics(failure)
    assert collect_link_diagnostics(lambda: 42) == LinkExtraction(42, ())


def test_concurrent_worker_threads_do_not_share_request_diagnostics():
    barrier = Barrier(2)

    def work(stage):
        barrier.wait(timeout=3)
        record_link_diagnostic(stage, LinkReason.OK)
        barrier.wait(timeout=3)
        return stage

    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(collect_link_diagnostics, work, stage) for stage in (LinkStage.IMAGE, LinkStage.VIDEO)]
        results = [call.result(timeout=5) for call in calls]
    assert results == [LinkExtraction(stage, (LinkDiagnostic(stage, LinkReason.OK),)) for stage in (LinkStage.IMAGE, LinkStage.VIDEO)]


def test_nested_yt_dlp_failure_retains_http_status_without_exception_text():
    http_error = YdlHTTPError(SimpleNamespace(status=403, reason=SECRET))
    extractor_error = ExtractorError(SECRET, cause=http_error)
    error = DownloadError(SECRET, exc_info=(ExtractorError, extractor_error, None))
    assert link_download.classify_link_error(error) == (LinkReason.HTTP_ERROR, 403)


def test_nested_transport_timeout_wins_over_generic_network_failure():
    transport = TransportError(msg=SECRET, cause=socket.timeout(SECRET))
    error = DownloadError(SECRET, exc_info=(TransportError, transport, None))
    assert link_download.classify_link_error(error) == (LinkReason.TIMEOUT, None)


def test_error_classification_is_bounded_and_never_stringifies_unknown_errors():
    class SensitiveError(Exception):
        def __str__(self):
            pytest.fail("Exception text must not be read")

    first, second = SensitiveError(), SensitiveError()
    first.__cause__ = second
    second.__cause__ = first
    assert link_download.classify_link_error(first) == (LinkReason.PROCESS_ERROR, None)


def run_result(monkeypatch, envelope, *, body=None):
    def run(command, **kwargs):
        if body is not None:
            (Path(command[-1]).parent / "result.bin").write_bytes(body)
        return json.dumps(envelope).encode()

    monkeypatch.setattr(link_download, "run_process", run)


def test_safe_child_failure_is_one_event_and_ignores_untrusted_extra_fields(monkeypatch):
    run_result(monkeypatch, {"ok": False, "reason": "http_error", "http_status": 429, "message": SECRET, "url": SECRET})
    result = collect_link_diagnostics(link_download.extract_info, URL, deadline=time.monotonic() + 10)
    assert result.value is None
    assert len(result.diagnostics) == 1
    event = result.diagnostics[0]
    assert (event.stage, event.reason, event.http_status) == (LinkStage.EXTRACT, LinkReason.HTTP_ERROR, 429)
    assert event.duration_ms is not None and event.duration_ms >= 0
    assert SECRET not in json.dumps(asdict(event))


@pytest.mark.parametrize(
    "envelope",
    [
        {"ok": False, "reason": SECRET},
        {"ok": False, "reason": "ok"},
        {"ok": False, "reason": "http_error", "http_status": SECRET},
        {"ok": True, "metadata": SECRET},
        {"ok": "true", "metadata": {}},
        [],
    ],
)
def test_malformed_worker_envelopes_produce_fixed_invalid_response(monkeypatch, envelope):
    run_result(monkeypatch, envelope)
    result = collect_link_diagnostics(link_download.extract_info, URL, deadline=time.monotonic() + 10)
    assert result.value is None
    assert [event.reason for event in result.diagnostics] == [LinkReason.INVALID_RESPONSE]
    assert SECRET not in repr(result.diagnostics)


def test_json_success_emits_one_request_outcome_after_validation(monkeypatch):
    def run(command, **kwargs):
        request = json.loads(Path(command[-1]).read_text())
        assert request["operation"] == "json"
        return b'{"ok":true,"metadata":{"title":"Useful title"}}'

    monkeypatch.setattr(link_download, "run_process", run)
    result = collect_link_diagnostics(
        link_download.request_json,
        "https://www.youtube.com/oembed",
        deadline=time.monotonic() + 10,
        allowed_hosts=("www.youtube.com",),
    )
    assert result.value == {"title": "Useful title"}
    assert [(event.stage, event.reason) for event in result.diagnostics] == [(LinkStage.REQUEST, LinkReason.OK)]


@pytest.mark.parametrize(
    "failure,reason",
    [
        (subprocess.TimeoutExpired(SECRET, 10), LinkReason.TIMEOUT),
        (subprocess.CalledProcessError(1, SECRET, stderr=SECRET), LinkReason.PROCESS_ERROR),
        (subprocess.CalledProcessError(-signal.SIGXFSZ, SECRET), LinkReason.TOO_LARGE),
        (link_download.ProcessOutputTooLarge(SECRET), LinkReason.TOO_LARGE),
        (FileNotFoundError(SECRET), LinkReason.PROCESS_ERROR),
    ],
)
def test_parent_process_failures_keep_safe_reason_and_clean_files(monkeypatch, failure, reason):
    directories = []

    def run(command, **kwargs):
        directory = Path(command[-1]).parent
        directories.append(directory)
        (directory / "partial.mp4").write_bytes(b"partial")
        raise failure

    monkeypatch.setattr(link_download, "run_process", run)
    result = collect_link_diagnostics(link_download.download_video, URL, deadline=time.monotonic() + 10)
    assert result.value is None
    assert [event.reason for event in result.diagnostics] == [reason]
    assert SECRET not in repr(result.diagnostics)
    assert not directories[0].exists()


def test_oversized_finished_binary_is_not_reported_as_success(monkeypatch):
    run_result(monkeypatch, {"ok": True, "metadata": {"width": 1, "height": 1}}, body=b"12345")
    result = collect_link_diagnostics(
        link_download.download_image,
        "https://cdn.test/image",
        deadline=time.monotonic() + 10,
        allowed_hosts=("cdn.test",),
        max_bytes=4,
    )
    assert result.value is None
    assert [event.reason for event in result.diagnostics] == [LinkReason.TOO_LARGE]


def test_child_failure_envelope_does_not_write_response_body_or_traceback(monkeypatch, tmp_path, capsys):
    import resource

    path = tmp_path / "request.json"
    path.write_text(json.dumps({"operation": "extract", "url": URL, "provider": "youtube", "flat": False, "max_bytes": 1024}))
    response = requests.Response()
    response.status_code = 403
    response._content = SECRET.encode()

    def extract(*args):
        raise requests.HTTPError(SECRET, response=response)

    monkeypatch.setattr(link_download.os, "environ", {"SECRET_TOKEN": SECRET, "PATH": "/usr/bin"})
    monkeypatch.setattr(link_download.sys, "argv", ["link_download", str(path)])
    monkeypatch.setattr(resource, "setrlimit", lambda *args: None)
    monkeypatch.setattr(link_download, "_extract", extract)
    link_download._main()
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": False, "reason": "http_error", "http_status": 403}
    assert captured.err == "" and SECRET not in captured.out
    assert "SECRET_TOKEN" not in link_download.os.environ


@pytest.mark.parametrize("fetch", [youtube.fetch_youtube, instagram.fetch_instagram, tiktok.fetch_tiktok])
def test_provider_unsupported_routes_have_a_scoped_decline(fetch):
    result = collect_link_diagnostics(fetch, "https://unsupported.test/private")
    assert result.value is None
    assert result.diagnostics == (LinkDiagnostic(LinkStage.ADAPTER, LinkReason.UNSUPPORTED),)


def test_instagram_malformed_post_metadata_has_an_explicit_decline(monkeypatch):
    monkeypatch.setattr(instagram, "extract_info", lambda *args, **kwargs: {"id": "wrong", "description": SECRET})
    result = collect_link_diagnostics(instagram.fetch_instagram, "https://www.instagram.com/p/ABCdefghi/")
    assert result.value is None
    assert result.diagnostics == (LinkDiagnostic(LinkStage.ADAPTER, LinkReason.INVALID_RESPONSE),)
    assert SECRET not in repr(result.diagnostics)


def test_youtube_unavailable_video_has_no_invented_success(monkeypatch):
    monkeypatch.setattr(youtube, "extract_info", lambda *args, **kwargs: None)
    monkeypatch.setattr(youtube, "request_json", lambda *args, **kwargs: None)
    result = collect_link_diagnostics(youtube.fetch_youtube, "https://www.youtube.com/shorts/jNQXAC9IVRw")
    assert result.value is None
    assert result.diagnostics == (LinkDiagnostic(LinkStage.ADAPTER, LinkReason.UNAVAILABLE),)
