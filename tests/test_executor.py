"""Bounded, offline regression tests for the shared application worker pool."""

import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures.thread import BrokenThreadPool

import pytest

from msu_hub_bot.execution.executor import ExecutorBusy, TPExecutor


class CountingPool(ThreadPoolExecutor):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.submitted = 0

    def submit(self, function, *args, **kwargs):
        self.submitted += 1
        return super().submit(function, *args, **kwargs)


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("workers", [1, 3])
@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_abandoned_work_keeps_its_slot(workers, stop):
    executor = TPExecutor(workers)
    executor.ExecutorClass = CountingPool
    release = threading.Event()
    started = [threading.Event() for _ in range(workers)]

    def block(index):
        started[index].set()
        assert release.wait(3), "test worker was not released"

    jobs = [asyncio.create_task(executor.run(block, index, timeout=0.1 if stop == "timeout" else None)) for index in range(workers)]
    try:
        await wait_until(lambda: all(event.is_set() for event in started))
        if stop == "cancel":
            for job in jobs:
                job.cancel()
            results = await asyncio.gather(*jobs, return_exceptions=True)
            assert all(isinstance(result, asyncio.CancelledError) for result in results)
        else:
            assert await asyncio.gather(*jobs) == [(None, True)] * workers

        assert await asyncio.wait_for(executor.run(lambda: "late", timeout=0.02), 0.5) == (None, True)
        # A timed-out caller must not admit another job to the pool's unbounded queue.
        assert executor.executor.submitted == workers
        release.set()
        assert await executor.run(lambda: "recovered", timeout=1) == ("recovered", False)
    finally:
        release.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        executor.shutdown(wait=True)


async def test_timeout_includes_waiting_for_a_worker():
    executor = TPExecutor(1)
    release = threading.Event()
    started = threading.Event()

    def block():
        started.set()
        assert release.wait(3), "test worker was not released"

    first = asyncio.create_task(executor.run(block, timeout=None))
    try:
        await wait_until(started.is_set)
        assert await asyncio.wait_for(executor.run(lambda: "queued", timeout=0.02), 0.5) == (None, True)
    finally:
        release.set()
        await first
        executor.shutdown(wait=True)


class BrokenPool(ThreadPoolExecutor):
    def submit(self, *args, **kwargs):
        raise BrokenThreadPool("synthetic pool failure")


@pytest.mark.parametrize("workers", [1, 3])
async def test_broken_pool_retries_without_deadlock_or_extra_arguments(workers):
    executor = TPExecutor(workers)
    pools = []

    def factory(**kwargs):
        pool = (BrokenPool if not pools else CountingPool)(**kwargs)
        pools.append(pool)
        return pool

    executor.ExecutorClass = factory
    try:
        assert await asyncio.wait_for(executor.run(lambda value: value, "ok", timeout=1), 0.5) == ("ok", False)
        assert len(pools) == 2
    finally:
        executor.shutdown(wait=True)


async def test_broken_pool_recovery_has_a_retry_limit():
    executor = TPExecutor(1)
    pools = []

    def factory(**kwargs):
        pool = BrokenPool(**kwargs)
        pools.append(pool)
        return pool

    executor.ExecutorClass = factory
    try:
        with pytest.raises(BrokenThreadPool, match="synthetic"):
            await asyncio.wait_for(executor.run(lambda: None, timeout=1), 0.5)
        assert len(pools) == 2
    finally:
        executor.shutdown(wait=True)


async def test_worker_timeout_error_is_not_a_caller_timeout():
    executor = TPExecutor(1)

    def fail():
        raise TimeoutError("provider deadline")

    try:
        with pytest.raises(TimeoutError, match="provider deadline"):
            await executor.run(fail)
        assert await executor.run(lambda: "ok") == ("ok", False)
    finally:
        executor.shutdown(wait=True)


async def test_shutdown_does_not_create_a_pool_and_rejects_new_work():
    executor = TPExecutor(1)
    executor.shutdown(wait=False)
    assert executor._executor is None
    with pytest.raises(RuntimeError, match="shutdown"):
        await executor.run(lambda: None)
    executor.shutdown(wait=False)


@pytest.mark.parametrize("timeout", [0, -1])
async def test_expired_deadline_does_not_start_work(timeout):
    executor = TPExecutor(1)
    started = threading.Event()
    try:
        assert await executor.run(started.set, timeout=timeout) == (None, True)
        assert not started.is_set()
        assert executor._executor is None
    finally:
        executor.shutdown(wait=True)


@pytest.mark.parametrize("workers", [0, -1, 0.5, True])
def test_invalid_worker_limit_rejected(workers):
    with pytest.raises(ValueError):
        TPExecutor(workers)


async def test_cancelled_queue_waiter_never_runs_or_loses_capacity():
    executor = TPExecutor(1)
    release = threading.Event()
    started = threading.Event()
    queued_started = threading.Event()

    def block():
        started.set()
        assert release.wait(3), "test worker was not released"

    first = asyncio.create_task(executor.run(block))
    queued = None
    try:
        await wait_until(started.is_set)
        queued = asyncio.create_task(executor.run(queued_started.set))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        release.set()
        await first
        assert await executor.run(lambda: "ok", timeout=1) == ("ok", False)
        assert not queued_started.is_set()
    finally:
        release.set()
        await asyncio.gather(first, *([queued] if queued is not None else []), return_exceptions=True)
        executor.shutdown(wait=True)


async def test_concurrent_broken_futures_share_one_replacement_pool():
    executor = TPExecutor(3)
    futures = []
    pools = []

    class BrokenFuturePool(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            future = Future()
            futures.append(future)
            return future

    def factory(**kwargs):
        pool = (BrokenFuturePool if not pools else CountingPool)(**kwargs)
        pools.append(pool)
        return pool

    executor.ExecutorClass = factory
    jobs = [asyncio.create_task(executor.run(lambda value: value, index, timeout=1)) for index in range(3)]
    try:
        await wait_until(lambda: len(futures) == 3)
        for future in futures:
            future.set_exception(BrokenThreadPool("synthetic future failure"))
        assert await asyncio.wait_for(asyncio.gather(*jobs), 1) == [(index, False) for index in range(3)]
        assert len(pools) == 2
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        executor.shutdown(wait=True)


async def test_owned_thread_executor_closes_after_work():
    executor = TPExecutor(max_workers=3)
    worker, timed_out = await executor.run(threading.get_ident)
    assert worker != threading.get_ident() and not timed_out
    await asyncio.to_thread(executor.shutdown, wait=True)
    with pytest.raises(RuntimeError, match="shutdown"):
        await executor.run(lambda: None)


@pytest.mark.parametrize("pending", [0, 1, 3])
async def test_saturation_rejects_before_preparation_and_recovers(pending):
    executor = TPExecutor(1, max_pending=pending)
    release = threading.Event()
    started = threading.Event()
    prepared = []

    def block():
        started.set()
        assert release.wait(3)

    async def prepare():
        prepared.append(True)
        return ("ok",)

    active = asyncio.create_task(executor.run(block))
    waiting = []
    try:
        await wait_until(started.is_set)
        waiting = [asyncio.create_task(executor.run_prepared(prepare, str)) for _ in range(pending)]
        await wait_until(lambda: executor._waiting == pending)
        with pytest.raises(ExecutorBusy):
            await executor.run_prepared(prepare, str)
        assert not prepared
        release.set()
        await active
        assert await asyncio.gather(*waiting) == [("ok", False)] * pending
        assert await executor.run_prepared(prepare, str) == ("ok", False)
        assert len(prepared) == pending + 1
    finally:
        release.set()
        await asyncio.gather(active, *waiting, return_exceptions=True)
        executor.shutdown(wait=True)


@pytest.mark.parametrize("stop", ["cancel", "timeout", "error"])
async def test_preparation_holds_slot_and_releases_it_on_failure(stop):
    executor = TPExecutor(1, max_pending=0)
    entered = asyncio.Event()
    finish = asyncio.Event()
    started = threading.Event()

    async def prepare():
        entered.set()
        await finish.wait()
        raise ValueError("synthetic preparation failure")

    task = asyncio.create_task(executor.run_prepared(prepare, started.set, timeout=0.1 if stop == "timeout" else None))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(ExecutorBusy):
            await executor.run(lambda: None)
        if stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif stop == "timeout":
            assert await task == (None, True)
        else:
            finish.set()
            with pytest.raises(ValueError, match="preparation"):
                await task
        assert not started.is_set()
        assert await executor.run(lambda: "recovered") == ("recovered", False)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        executor.shutdown(wait=True)


async def test_cancelled_waiter_frees_backlog_without_preparing_input():
    executor = TPExecutor(1, max_pending=1)
    release = threading.Event()
    started = threading.Event()
    prepared = []

    def block():
        started.set()
        assert release.wait(3)

    async def prepare():
        prepared.append(True)
        return ()

    active = asyncio.create_task(executor.run(block))
    pending = None
    try:
        await wait_until(started.is_set)
        pending = asyncio.create_task(executor.run_prepared(prepare, lambda: None))
        await wait_until(lambda: executor._waiting == 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert executor._waiting == 0
        assert await executor.run_prepared(prepare, lambda: None, timeout=0.01) == (None, True)
        assert not prepared
        release.set()
        await active
        assert await executor.run_prepared(prepare, lambda: "ok") == ("ok", False)
    finally:
        release.set()
        await asyncio.gather(active, *([pending] if pending else []), return_exceptions=True)
        executor.shutdown(wait=True)


async def test_broken_pool_does_not_repeat_preparation():
    executor = TPExecutor(1)
    pools, prepared = [], []

    def factory(**kwargs):
        pool = (BrokenPool if not pools else CountingPool)(**kwargs)
        pools.append(pool)
        return pool

    async def prepare():
        prepared.append(True)
        return ("downloaded once",)

    executor.ExecutorClass = factory
    try:
        assert await executor.run_prepared(prepare, str) == ("downloaded once", False)
        assert len(prepared) == 1
    finally:
        executor.shutdown(wait=True)


@pytest.mark.parametrize("pending", [-1, 1.5, True])
def test_invalid_pending_limit_rejected(pending):
    with pytest.raises(ValueError):
        TPExecutor(1, max_pending=pending)
