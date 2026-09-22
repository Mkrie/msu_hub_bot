"""Own the application's existing leased worker through a feature lifespan."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from teleforge.app import App
from teleforge.feature import Feature

from msu_hub_bot.storage.features import FeatureWorker


class DurableWorkers(Feature, key="durable_workers"):
    """Reminder, game and feedback services keep their registrations and transactions.

    Include this once after constructing those services with the same worker.
    Persistence and bot clients must outlive this feature's draining shutdown.
    """

    def __init__(self, worker: FeatureWorker) -> None:
        self.worker = worker
        self._task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def lifespan(self, app: App) -> AsyncIterator[None]:
        if self._task is not None:
            raise RuntimeError("The durable worker has one application lifetime")
        self._task = asyncio.create_task(self.worker.run(), name="msu-feature-worker")
        try:
            yield
        finally:
            self.worker.stop()
            try:
                await self._task
            except asyncio.CancelledError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
                raise
