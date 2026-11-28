"""Asynchronous durable storage boundary for backend and controller clients."""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import os
import queue
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ParamSpec, Protocol, TypeAlias, TypeVar, cast

from .backend import BackendLedger
from .contract import Route
from .controller import ControllerLedger
from .models import ConfigRequest, JsonObject

_P = ParamSpec("_P")
_T = TypeVar("_T")

ReceiptRow: TypeAlias = tuple[str, str, bytes]
DueCommandRow: TypeAlias = tuple[str, str, bytes, int]
FactCallback: TypeAlias = Callable[[Route, JsonObject, Any], Awaitable[object]]
SQLParameters: TypeAlias = Sequence[Any] | Mapping[str, Any]


class Storage(Protocol):
    """Implementations must atomically commit application writes and receipts.

    The callback passed to ingest runs once per new fact within that transaction.
    A failure must roll back the fact, application writes and receipt together.
    Receipt confirmation must occur only after successful broker PUBACK.
    """

    async def initialize(self, node_id: str, bridge_id: str, consumer_id: str) -> None:
        """Bind persistent storage to the configured chair identities."""
        ...

    async def ingest(
        self,
        topic: str,
        payload: bytes,
        *,
        qos: int,
        retain: bool,
        on_fact: FactCallback,
    ) -> bool:
        """Deduplicate and commit a publication and receipt with its callback."""
        ...

    async def reject(self, topic: str, payload: bytes, reason: str) -> None:
        """Persist a bounded diagnosis of an invalid delivery."""
        ...

    async def pending_receipts(self) -> list[ReceiptRow]:
        """Return pending (publication_id, topic, payload) tuples."""
        ...

    async def confirm_receipt(self, publication_id: str) -> None:
        """Remove a receipt only after the broker acknowledges its publication."""
        ...

    async def enqueue_command(self, request: ConfigRequest, now_ms: int) -> bool:
        """Persist an immutable ConfigRequest and return whether it is new."""
        ...

    async def due_commands(self, now_ms: int) -> list[DueCommandRow]:
        """Return retry-ready (id, topic, payload, expiry_ms) tuples and expire old work."""
        ...

    async def command_submitted(self, command_id: str, now_ms: int) -> bool:
        """Schedule the next admission retry; return whether the row was updated."""
        ...

    async def observe_report(
        self, topic: str, payload: bytes, *, qos: int, retain: bool
    ) -> bool:
        """Validate and deduplicate a command report; return whether state advanced."""
        ...

    async def get_command(self, command_id: str) -> JsonObject | None:
        """Return stored admission, outcome, report sequence and report, or None."""
        ...

    async def close(self) -> None:
        """Release storage resources after in-progress operations finish."""
        ...


class SQLiteTransaction:
    """Execute application statements on the same SQLite transaction as a fact."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection: sqlite3.Connection = connection
        self._active: bool = True
        self._operations: queue.Queue[
            tuple[
                str,
                SQLParameters,
                concurrent.futures.Future[list[Any] | int],
            ]
        ] = queue.Queue()

    async def execute(
        self, statement: str, parameters: SQLParameters = ()
    ) -> list[Any] | int:
        """Execute one SQL statement; return selected rows or affected-row count.

        Use only inside the event callback. Commit and rollback are controlled
        by the connector so the application write and receipt stay atomic.
        """
        if not self._active:
            raise RuntimeError("transaction is no longer active")
        verb = statement.lstrip().split(None, 1)[0].upper()
        if verb in {
            "BEGIN",
            "COMMIT",
            "END",
            "ROLLBACK",
            "SAVEPOINT",
            "RELEASE",
            "PRAGMA",
            "VACUUM",
            "ATTACH",
            "DETACH",
        }:
            raise ValueError("transaction control is managed by the library")

        result: concurrent.futures.Future[list[Any] | int] = concurrent.futures.Future()
        self._operations.put((statement, parameters, result))
        return await asyncio.wrap_future(result)


class SQLiteStorage:
    """Durable SQLite connector at ``path``; database work runs off the event loop.

    Use a file path, not an in-memory database. The parent directory must exist.
    The client initializes and closes the connector; do not reuse it afterwards.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path: Path = Path(path)
        if str(self.path) in {".", ":memory:"}:
            raise ValueError("a durable SQLite path is required")
        self._executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="chair-db"
        )
        self._db: sqlite3.Connection | None = None
        self._backend: BackendLedger | None = None
        self._controller: ControllerLedger | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._callback_task: asyncio.Task[Any] | None = None

    async def _run(
        self, fn: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs
    ) -> _T:
        if (
            self._callback_task is not None
            and asyncio.current_task() is self._callback_task
        ):
            # The worker thread is waiting for this callback, so the call would
            # queue behind it forever.
            raise RuntimeError(
                "storage cannot be used from inside its own transaction callback; "
                "use the supplied transaction, or start a task that runs after it"
            )
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, functools.partial(fn, *args, **kwargs)
        )

    async def initialize(self, node_id: str, bridge_id: str, consumer_id: str) -> None:
        """Open the database and bind it to the configured identities."""
        if self._loop is not None:
            raise RuntimeError("storage already initialized")
        self._loop = asyncio.get_running_loop()

        def setup() -> None:
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._backend = BackendLedger(
                self._db, node_id=node_id, bridge_id=bridge_id, consumer_id=consumer_id
            )
            self._controller = ControllerLedger(self._db, node_id=node_id)

        try:
            await self._run(setup)
        except BaseException:
            if self._db is not None:
                await self._run(self._db.close)
                self._db = None
            self._executor.shutdown(wait=True)
            raise

    async def ingest(
        self,
        topic: str,
        payload: bytes,
        *,
        qos: int,
        retain: bool,
        on_fact: FactCallback,
    ) -> bool:
        """Commit one validated delivery, application callback and receipt atomically."""
        backend = self._backend
        loop = self._loop
        assert backend is not None  # set by initialize()
        assert loop is not None  # set by initialize()

        def run_ingest() -> bool:
            def callback(
                db: sqlite3.Connection, route: Route, value: JsonObject
            ) -> None:
                tx = SQLiteTransaction(db)

                async def invoke() -> None:
                    self._callback_task = asyncio.current_task()
                    try:
                        await on_fact(route, value, tx)
                    finally:
                        self._callback_task = None
                        tx._active = False  # pyright: ignore[reportPrivateUsage]  # internal transaction state driven by SQLiteStorage

                future = asyncio.run_coroutine_threadsafe(invoke(), loop)
                while not future.done():
                    try:
                        statement, parameters, result = tx._operations.get(  # pyright: ignore[reportPrivateUsage]  # internal transaction queue driven by SQLiteStorage
                            timeout=0.05
                        )
                    except queue.Empty:
                        continue
                    if result.set_running_or_notify_cancel():
                        try:
                            cursor = db.execute(statement, parameters)
                            result.set_result(
                                cursor.fetchall()
                                if cursor.description
                                else cursor.rowcount
                            )
                        except BaseException as error:
                            result.set_exception(error)
                future.result()

            backend.on_fact = callback
            try:
                return backend.ingest(topic, payload, qos=qos, retain=retain)
            finally:
                backend.on_fact = None

        return await self._run(run_ingest)

    async def reject(self, topic: str, payload: bytes, reason: str) -> None:
        """Record a bounded diagnosis for invalid delivery bytes."""
        assert self._backend is not None  # set by initialize()
        await self._run(self._backend.reject, topic, payload, reason)

    async def pending_receipts(self) -> list[ReceiptRow]:
        """Return pending (publication_id, topic, payload) tuples."""
        assert self._backend is not None  # set by initialize()
        return cast(list[ReceiptRow], await self._run(self._backend.pending_receipts))

    async def confirm_receipt(self, publication_id: str) -> None:
        """Remove a receipt following successful MQTT PUBACK."""
        assert self._backend is not None  # set by initialize()
        await self._run(self._backend.confirmed, publication_id)

    async def enqueue_command(self, request: ConfigRequest, now_ms: int) -> bool:
        """Durably store a ConfigRequest and return whether it was newly queued."""
        assert self._controller is not None  # set by initialize()
        return await self._run(self._controller.enqueue, request, now_ms=now_ms)

    async def due_commands(self, now_ms: int) -> list[DueCommandRow]:
        """Return commands due for publication and record unconfirmed expiry."""
        assert self._controller is not None  # set by initialize()
        return cast(
            list[DueCommandRow],
            await self._run(self._controller.due, now_ms=now_ms),
        )

    async def command_submitted(self, command_id: str, now_ms: int) -> bool:
        """Record a publish attempt and schedule the next admission retry."""
        assert self._controller is not None  # set by initialize()
        return await self._run(self._controller.submitted, command_id, now_ms=now_ms)

    async def observe_report(
        self, topic: str, payload: bytes, *, qos: int, retain: bool
    ) -> bool:
        """Persist and deduplicate a report; return whether it advanced the state."""
        assert self._controller is not None  # set by initialize()
        return await self._run(
            self._controller.observe, topic, payload, qos=qos, retain=retain
        )

    async def get_command(self, command_id: str) -> JsonObject | None:
        """Return a stored command dictionary, or None when the ID is unknown."""
        assert self._controller is not None  # set by initialize()
        return await self._run(self._controller.get, command_id)

    async def close(self) -> None:
        """Close the database and its worker; this connector cannot be reused."""
        if self._db is not None:
            await self._run(self._db.close)
            self._db = None
        self._executor.shutdown(wait=True)
