"""Bound submitted thread work while keeping caller deadlines independent.

Running threads retain their admission slots after cancellation or timeout.
Waiting callers have a finite backlog; asynchronous preparation starts only
after admission. Jobs must bound their own I/O, subprocesses and decoded media.
Shutdown cannot terminate running threads.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import Executor, Future
from concurrent.futures.thread import ThreadPoolExecutor, BrokenThreadPool
from contextlib import suppress
from contextvars import Context
from functools import partial
from typing import Any, TypeVar

from msu_hub_bot.telemetry import Backend, Boundary, GaugeName, Outcome, Telemetry, failure_outcome

ResultT = TypeVar("ResultT")


class ExecutorBusy(RuntimeError):
    """All worker slots and pending admissions are occupied."""


class TPExecutor:
    ExecutorClass: Callable[..., Executor] = ThreadPoolExecutor
    ExecutorException: type[RuntimeError] = BrokenThreadPool

    def __init__(self, max_workers: int, telemetry: Telemetry | None = None, *, max_pending: int = 3) -> None:
        if not isinstance(max_workers, int) or isinstance(max_workers, bool) or max_workers <= 0:
            raise ValueError("max_workers must be a positive integer")
        if not isinstance(max_pending, int) or isinstance(max_pending, bool) or max_pending < 0:
            raise ValueError("max_pending must be a nonnegative integer")
        self.max_workers = max_workers
        self.max_pending = max_pending
        self.telemetry = telemetry or Telemetry()
        self._slots = asyncio.BoundedSemaphore(max_workers)
        self._closed = False
        self._waiting = 0
        self._running = 0
        self._executor: Executor | None = None

    @property
    def executor(self) -> Executor:
        if self._closed:
            raise RuntimeError("cannot schedule new futures after shutdown")
        if self._executor is None:
            self._executor = self.ExecutorClass(max_workers=self.max_workers)
        return self._executor

    def _complete(self, loop: asyncio.AbstractEventLoop, started: float, future: Future[Any]) -> None:
        outcome = Outcome.CANCELLED if future.cancelled() else Outcome.SUCCESS
        if not future.cancelled() and (error := future.exception()) is not None:
            outcome = failure_outcome(error)
        # Only aggregate measurements cross back; no request/FSM context or raw exception.
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(self._finish, time.monotonic() - started, outcome, context=Context())

    def _finish(self, duration: float, outcome: Outcome) -> None:
        self._slots.release()
        self._running -= 1
        self.telemetry.gauge(GaugeName.WORKERS_ACTIVE, self._running)
        self.telemetry.record_media_completion(duration, outcome)

    async def _observe(self, operation: str, awaitable: Awaitable[ResultT], deadline: asyncio.Timeout) -> ResultT:
        with self.telemetry.operation(Boundary.MEDIA, operation, backend=Backend.NATIVE) as observation:
            try:
                return await awaitable
            except asyncio.CancelledError:
                if deadline.expired():
                    observation.set_outcome(Outcome.TIMEOUT)
                raise

    async def run(self, func: Callable[..., Any], *args: Any, timeout: float | None = 180) -> tuple[Any, bool]:
        async def prepare() -> tuple[Any, ...]:
            return args

        return await self.run_prepared(prepare, func, timeout=timeout)

    async def run_prepared(
        self, prepare: Callable[[], Awaitable[tuple[Any, ...]]], func: Callable[..., Any], *, timeout: float | None = 180
    ) -> tuple[Any, bool]:
        """Apply a caller deadline to queueing, execution, and one pool recovery.

        Reserve a worker slot before downloading or retaining expensive inputs.
        ``prepare`` returns arguments for ``func`` and runs at most once. Keep
        preparation cancellable and release its own temporary resources on error.
        Timeout/cancellation cannot stop a running thread. Its slot stays occupied
        until the concurrent future finishes; jobs need their own I/O/process limits.
        Use this executor from a single application event loop.
        """
        if self._closed:
            raise RuntimeError("cannot schedule new futures after shutdown")
        if timeout is not None and timeout <= 0:
            return None, True
        loop = asyncio.get_running_loop()
        limit = asyncio.timeout(timeout)
        args: tuple[Any, ...] | None = None
        try:
            async with limit:
                for attempt in range(2):
                    pool = None
                    try:
                        if self._slots.locked() and self._waiting >= self.max_pending:
                            with self.telemetry.operation(Boundary.MEDIA, "worker.queue", backend=Backend.NATIVE):
                                raise ExecutorBusy("Media worker queue is full")
                        self._waiting += 1
                        self.telemetry.gauge(GaugeName.WORKERS_QUEUED, self._waiting)
                        try:
                            await self._observe("worker.queue", self._slots.acquire(), limit)
                        finally:
                            self._waiting -= 1
                            self.telemetry.gauge(GaugeName.WORKERS_QUEUED, self._waiting)
                        try:
                            if self._closed:
                                raise RuntimeError("cannot schedule new futures after shutdown")
                            if args is None:
                                args = await self._observe("worker.prepare", prepare(), limit)
                            pool = self.executor
                            started = time.monotonic()
                            future = pool.submit(func, *args)
                        except BaseException:
                            self._slots.release()
                            raise
                        # Track the real work, not the cancellable asyncio wrapper.
                        self._running += 1
                        self.telemetry.gauge(GaugeName.WORKERS_ACTIVE, self._running)
                        future.add_done_callback(partial(self._complete, loop, started))
                        return await self._observe("worker.run", asyncio.wrap_future(future), limit), False
                    except self.ExecutorException:
                        if pool is not None and pool is self._executor:
                            self._executor = None
                            pool.shutdown(wait=False, cancel_futures=True)
                        if attempt:
                            raise
        except TimeoutError:
            if limit.expired():
                return None, True
            raise
        raise RuntimeError("Worker recovery exhausted")

    def shutdown(self, wait: bool) -> None:
        """Reject new jobs and cancel queued futures; running threads are not killed."""
        self._closed = True
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=True)
