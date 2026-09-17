"""Downloads start after admission; only immutable input crosses into workers."""

import asyncio
import io
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.telegram import media_jobs


@pytest.mark.parametrize("fail", [False, True])
async def test_download_and_worker_streams_close_on_success_and_failure(monkeypatch, fail):
    download = io.BytesIO(b"synthetic media")
    monkeypatch.setattr(media_jobs, "download", AsyncMock(return_value=download))
    inputs = []

    def convert(stream, suffix):
        assert download.closed
        inputs.append(stream)
        if fail:
            raise ValueError("synthetic conversion failure")
        return stream.read() + suffix

    worker = TPExecutor(1)
    try:
        if fail:
            with pytest.raises(ValueError, match="synthetic"):
                await media_jobs.run_downloaded(worker, object(), convert, b"!", timeout=2)
        else:
            assert await media_jobs.run_downloaded(worker, object(), convert, b"!", timeout=2) == (b"synthetic media!", False)
        assert inputs[0].closed
        assert media_jobs.download.call_args.kwargs["max_bytes"] == media_jobs.MAX_DOWNLOAD_BYTES
    finally:
        worker.shutdown(wait=True)


async def test_no_start_failure_only_retains_immutable_bytes(monkeypatch):
    download = io.BytesIO(b"media")
    monkeypatch.setattr(media_jobs, "download", AsyncMock(return_value=download))

    async def cannot_submit(prepare, func, **kwargs):
        assert await prepare() == (b"media",)
        assert download.closed
        raise RuntimeError("no worker available")

    worker = SimpleNamespace(run_prepared=cannot_submit)
    with pytest.raises(RuntimeError, match="no worker"):
        await media_jobs.run_downloaded(worker, object(), lambda stream: None)


async def test_missing_download_never_submits_native_work(monkeypatch):
    monkeypatch.setattr(media_jobs, "download", AsyncMock(return_value=None))
    worker = TPExecutor(1)
    try:
        with pytest.raises(media_jobs.DownloadUnavailable):
            await media_jobs.run_downloaded(worker, object(), lambda stream: pytest.fail("missing media submitted"))
        assert worker._executor is None
        assert await worker.run(lambda: "recovered") == ("recovered", False)
    finally:
        worker.shutdown(wait=True)


async def test_waiting_cancelled_request_never_downloads(monkeypatch):
    download = AsyncMock(return_value=io.BytesIO(b"media"))
    monkeypatch.setattr(media_jobs, "download", download)
    started = threading.Event()
    release = threading.Event()
    worker = TPExecutor(1)

    def busy():
        started.set()
        assert release.wait(3)

    first = asyncio.create_task(worker.run(busy))
    request = None
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.001)
        request = asyncio.create_task(media_jobs.run_downloaded(worker, object(), lambda stream: None))
        await asyncio.sleep(0)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        download.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(first, *([request] if request is not None else []), return_exceptions=True)
        worker.shutdown(wait=True)


async def test_running_cancelled_request_keeps_its_input_until_worker_finishes(monkeypatch):
    download = io.BytesIO(b"media")
    monkeypatch.setattr(media_jobs, "download", AsyncMock(return_value=download))
    started = threading.Event()
    release = threading.Event()
    inputs = []
    worker = TPExecutor(1)

    def busy(stream):
        inputs.append(stream)
        started.set()
        assert release.wait(3)
        assert stream.read() == b"media"

    request = asyncio.create_task(media_jobs.run_downloaded(worker, object(), busy))
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.001)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert download.closed and not inputs[0].closed
        release.set()
        assert await worker.run(lambda: "recovered") == ("recovered", False)
        assert inputs[0].closed
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        worker.shutdown(wait=True)
