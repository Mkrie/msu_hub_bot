import asyncio
from concurrent.futures.process import ProcessPoolExecutor, BrokenProcessPool
from concurrent.futures.thread import ThreadPoolExecutor, BrokenThreadPool
from typing import Any, Callable, Tuple, Optional

from throttler import ThrottlerSimultaneous

from common.utils import do_nothing


class _BaseExecutor:
    ExecutorClass = None
    ExecutorException = None

    def __init__(self, max_workers: int):
        self.max_workers = max_workers
        self.throttler = ThrottlerSimultaneous(count=max_workers)

        # Lazy initialization
        self._executor = None

    @property
    def executor(self):
        if self._executor:
            return self._executor
        self._executor = self.ExecutorClass(max_workers=self.max_workers)
        return self._executor

    async def run(self, func: Callable, *args, timeout: Optional[float] = 180) -> Tuple[Any, bool]:
        async with self.throttler:
            try:
                future = asyncio.get_running_loop().run_in_executor(self.executor, func, *args)
                try:
                    result = await asyncio.wait_for(future, timeout=timeout)
                except asyncio.exceptions.TimeoutError:
                    return None, True
                return result, False
            except self.ExecutorException:
                self._executor.shutdown(wait=False, cancel_futures=True)
                self._executor = None
                return await self.run(func, *args, timeout)

    async def run_here(self, func: Callable, *args, timeout: float = None) -> Tuple[Any, bool]:
        """
        Useful analog for debugging
        """
        do_nothing(self, timeout)
        result = func(*args)
        return result, False

    def shutdown(self, wait: bool):
        return self.executor.shutdown(wait=wait, cancel_futures=True)


class TPExecutor(_BaseExecutor):
    ExecutorClass = ThreadPoolExecutor
    ExecutorException = BrokenThreadPool


class PPExecutor(_BaseExecutor):
    ExecutorClass = ProcessPoolExecutor
    ExecutorException = BrokenProcessPool


PPExecutor = TPExecutor  # noqa: F811 — preserve the deployed thread executor


class FakePPExecutor(PPExecutor):
    def __init__(self, *args, **kwargs):
        pass

    async def run(self, func: Callable, *args, timeout: float = None) -> Tuple[Any, bool]:
        """
        Useful analog for debugging
        """
        do_nothing(self, timeout)
        result = func(*args)
        return result, False
