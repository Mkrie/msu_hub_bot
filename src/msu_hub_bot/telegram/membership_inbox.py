"""Persist membership evidence before Telegram polling can acknowledge it."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import sqlite3
import stat
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

from aiogram.types import Update
from pydantic import ValidationError

from msu_hub_bot.storage.base import BotRepository
from msu_hub_bot.storage.errors import RepositoryError, RepositoryFailure
from msu_hub_bot.storage.models import MembershipBatch
from msu_hub_bot.storage.observations import membership_batch
from msu_hub_bot.telemetry import Boundary, Outcome, Telemetry

logger = logging.getLogger(__name__)
ResultT = TypeVar("ResultT")
MAX_BATCH_BYTES = 1024 * 1024
MAX_CAPTURE_BYTES = 8 * 1024 * 1024


class MembershipInboxError(RuntimeError):
    """A fixed diagnostic; SQLite paths, payloads and exception text stay private."""


class MembershipInbox:
    """One private journal and one retry consumer; remote replay is idempotent.

    All SQLite work uses one owned thread, including closing the connection.
    Cancellation can abandon a wait, but cannot race connection teardown with a
    running commit. Failed or interrupted delivery leaves the saved batch intact.
    """

    def __init__(
        self,
        path: Path,
        bot_id: int,
        repository: BotRepository,
        *,
        telemetry: Telemetry | None = None,
        max_rows: int = 10_000,
        max_bytes: int = 32 * 1024 * 1024,
        retry_seconds: float = 1,
        delivery_timeout: float = 20,
    ) -> None:
        if (
            max_rows < 1
            or max_bytes < 1
            or not 0 < retry_seconds <= 30
            or not 0 < delivery_timeout <= 60
            or not math.isfinite(delivery_timeout)
        ):
            raise ValueError("Membership inbox limits must be positive and bounded")
        self.path = path
        self.bot_id = bot_id
        self.repository = repository
        self.telemetry = telemetry or Telemetry()
        self.max_rows = max_rows
        self.max_bytes = max_bytes
        self.retry_seconds = retry_seconds
        self.delivery_timeout = delivery_timeout
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="membership-inbox")
        self._connection: sqlite3.Connection | None = None
        self._wake = asyncio.Event()
        self._stopped = asyncio.Event()
        self._stop = False
        self._opened = False
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def _io(self, operation: Callable[[], ResultT]) -> ResultT:
        if self._closed:
            raise MembershipInboxError("Membership inbox is closed")
        try:
            return await asyncio.get_running_loop().run_in_executor(self._executor, operation)
        except sqlite3.Error, OSError:
            raise MembershipInboxError("Membership inbox persistence failed") from None

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise MembershipInboxError("Membership inbox is not open")
        return self._connection

    def _open(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = self.path.parent.lstat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid() or directory.st_mode & 0o077:
            raise MembershipInboxError("Membership inbox requires a private directory")
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise MembershipInboxError("Membership inbox requires a private regular file")
        finally:
            os.close(descriptor)
        connection = sqlite3.connect(self.path, timeout=0.25, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=EXTRA")
            connection.execute("PRAGMA secure_delete=ON")
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            # Bound the database itself as well as its queued payloads. Rollback
            # journals are transient; no unbounded WAL is retained between polls.
            connection.execute(f"PRAGMA max_page_count={max(16, 2 * self.max_bytes // page_size)}")
            if connection.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise MembershipInboxError("Membership inbox schema is unsupported")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS owner (id INTEGER PRIMARY KEY CHECK (id = 1), bot_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS pending (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    digest TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL
                );
            """)
            connection.execute("INSERT OR IGNORE INTO owner VALUES (1, ?)", (self.bot_id,))
            if connection.execute("SELECT bot_id FROM owner WHERE id=1").fetchone()[0] != self.bot_id:
                raise MembershipInboxError("Membership inbox belongs to another bot")
            connection.execute("PRAGMA user_version=1")
        except BaseException:
            connection.close()
            raise
        self._connection = connection

    async def open(self) -> None:
        if self._opened:
            return
        if self._closing:
            raise MembershipInboxError("Membership inbox is closed")
        await self._io(self._open)
        self._opened = True

    async def capture(self, updates: Sequence[Update]) -> None:
        """Commit the complete minimal batch before a polling response returns."""
        if len(updates) > 100:
            raise MembershipInboxError("Membership polling batch is too large")
        batches = [batch for update in updates if (batch := membership_batch(update)) is not None]
        await self.enqueue(batches)

    async def enqueue(self, batches: Sequence[MembershipBatch]) -> None:
        if not self._opened or self._closing or self._stop:
            raise MembershipInboxError("Membership inbox is not accepting updates")
        if not batches:
            return
        if len(batches) > 100:
            raise MembershipInboxError("Membership polling batch is too large")
        records: list[tuple[str, str]] = []
        total = 0
        for batch in batches:
            # Keep this boundary minimal even when a future caller supplies
            # richer profiles than the Telegram membership extractor does.
            payload = batch.model_dump(
                mode="json", exclude_unset=True, exclude={"users": {"__all__": {"profile"}}, "chats": {"__all__": {"profile"}}}
            )
            # An absent identity field is not an explicit clear. Clocks retain
            # their original values even when supplied by model defaults.
            payload["received_at"] = batch.received_at.isoformat()
            for name, timestamps in (
                ("users", [user.observed_at for user in batch.users]),
                ("chats", [chat.observed_at for chat in batch.chats]),
                ("memberships", [member.observed_at for member in batch.memberships]),
            ):
                for value, timestamp in zip(payload.get(name, []), timestamps, strict=True):
                    value["observed_at"] = timestamp.isoformat()
            identity = json.dumps(
                {key: value for key, value in payload.items() if key != "received_at"}, sort_keys=True, separators=(",", ":")
            )
            serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            size = len(serialized)
            total += size
            if size > MAX_BATCH_BYTES or total > MAX_CAPTURE_BYTES:
                raise MembershipInboxError("Membership polling batch is too large")
            records.append((hashlib.sha256(identity.encode()).hexdigest(), serialized))
        await self._io(lambda: self._persist(records))
        self._wake.set()

    def _persist(self, records: list[tuple[str, str]]) -> None:
        connection = self._db()
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows, size = connection.execute("SELECT COUNT(*), COALESCE(SUM(length(payload)), 0) FROM pending").fetchone()
            for digest, payload in records:
                inserted = connection.execute("INSERT OR IGNORE INTO pending(digest, payload) VALUES (?, ?)", (digest, payload)).rowcount
                rows += inserted
                size += inserted * len(payload)
                if rows > self.max_rows or size > self.max_bytes:
                    raise MembershipInboxError("Membership inbox is full; polling is paused")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def _next(self) -> tuple[int, str] | None:
        row = self._db().execute("SELECT id, payload FROM pending ORDER BY id LIMIT 1").fetchone()
        return (int(row[0]), str(row[1])) if row is not None else None

    async def pending(self) -> int:
        return await self._io(lambda: int(self._db().execute("SELECT COUNT(*) FROM pending").fetchone()[0]))

    def _delete(self, identifier: int) -> None:
        self._db().execute("DELETE FROM pending WHERE id=?", (identifier,))

    def _report_failure(self, error: Exception) -> None:
        # The shared boundary exports only its fixed error classification. An
        # incident is emitted once per failed period, rather than per retry.
        try:
            with self.telemetry.operation(Boundary.JOB, "membership.ingest", trace=False) as operation:
                if isinstance(error, RepositoryError):
                    if error.code is RepositoryFailure.TIMEOUT:
                        operation.set_outcome(Outcome.TIMEOUT)
                    elif error.code is RepositoryFailure.UNAVAILABLE:
                        operation.set_outcome(Outcome.UNAVAILABLE)
                raise error
        except RepositoryError, MembershipInboxError, ValidationError, TimeoutError:
            pass

    async def run(self) -> None:
        """Retry saved facts independently from ordinary update archival."""
        failed = False
        delay = self.retry_seconds
        while not self._stop:
            self._wake.clear()
            try:
                row = await self._io(self._next)
                if row is None:
                    await self._wake.wait()
                    continue
                identifier, payload = row
                batch = MembershipBatch.model_validate_json(payload)
                async with asyncio.timeout(self.delivery_timeout):
                    await self.repository.observe_memberships(batch)
                await self._io(lambda: self._delete(identifier))
                if failed:
                    logger.info("Membership ingestion recovered")
                    with self.telemetry.operation(Boundary.JOB, "membership.ingest"):
                        pass
                failed = False
                delay = self.retry_seconds
            except (RepositoryError, MembershipInboxError, ValidationError, TimeoutError) as error:
                if not failed:
                    logger.warning("Membership ingestion delayed; saved events will be retried")
                    self._report_failure(error)
                failed = True
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(max(self.retry_seconds, delay * 2), 30)

    def stop(self) -> None:
        self._stop = True
        self._stopped.set()
        self._wake.set()

    async def close(self) -> None:
        if self._close_task is None:
            self.stop()
            self._closing = True
            self._close_task = asyncio.create_task(self._finish_close(), name="membership-inbox-close")
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _finish_close(self) -> None:
        def close_connection() -> None:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

        # This operation queues after any abandoned/cancelled write on the same
        # thread; neither the connection nor its executor outlives normal close.
        try:
            await self._io(close_connection)
        finally:
            self._closed = True
            await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)
