"""Asynchronous durable storage boundary for backend and controller clients."""

import asyncio
import concurrent.futures
import queue
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

from .backend import BackendLedger
from .controller import ControllerLedger


class Storage(Protocol):
    """Implementations must atomically commit application writes and receipts.

    The callback passed to ingest runs once per new fact within that transaction.
    A failure must roll back the fact, application writes and receipt together.
    Receipt confirmation must occur only after successful broker PUBACK.
    """

    async def initialize(self, node_id, bridge_id, consumer_id):
        """Bind persistent storage to the configured chair identities."""

    async def ingest(self, topic, payload, *, qos, retain, on_fact):
        """Deduplicate and commit a publication and receipt with its callback."""

    async def reject(self, topic, payload, reason):
        """Persist a bounded diagnosis of an invalid delivery."""

    async def pending_receipts(self):
        """Return pending (publication_id, topic, payload) tuples."""

    async def confirm_receipt(self, publication_id):
        """Remove a receipt only after the broker acknowledges its publication."""

    async def enqueue_command(self, request, now_ms):
        """Persist an immutable ConfigRequest and return whether it is new."""

    async def due_commands(self, now_ms):
        """Return retry-ready (id, topic, payload, expiry_ms) tuples and expire old work."""

    async def command_submitted(self, command_id, now_ms):
        """Schedule the next admission retry; return whether the row was updated."""

    async def observe_report(self, topic, payload, *, qos, retain):
        """Validate and deduplicate a command report; return whether state advanced."""

    async def get_command(self, command_id):
        """Return stored admission, outcome, report sequence and report, or None."""

    async def close(self):
        """Release storage resources after in-progress operations finish."""


class SQLiteTransaction:
    """Execute application statements on the same SQLite transaction as a fact."""

    def __init__(self, connection):
        self._connection = connection
        self._active = True
        self._operations = queue.Queue()

    async def execute(self, statement, parameters=()):
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

        result = concurrent.futures.Future()
        self._operations.put((statement, parameters, result))
        return await asyncio.wrap_future(result)


class SQLiteStorage:
    """Durable SQLite connector at ``path``; database work runs off the event loop.

    Use a file path, not an in-memory database. The parent directory must exist.
    The client initializes and closes the connector; do not reuse it afterwards.
    """

    def __init__(self, path):
        self.path = Path(path)
        if str(self.path) in {".", ":memory:"}:
            raise ValueError("a durable SQLite path is required")
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="chair-db"
        )
        self._db = self._backend = self._controller = None
        self._loop = None

    async def _run(self, fn, *args, **kwargs):
        import functools

        return await asyncio.get_running_loop().run_in_executor(
            self._executor, functools.partial(fn, *args, **kwargs)
        )

    async def initialize(self, node_id, bridge_id, consumer_id):
        """Open the database and bind it to the configured identities."""
        if self._loop is not None:
            raise RuntimeError("storage already initialized")
        self._loop = asyncio.get_running_loop()

        def setup():
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

    async def ingest(self, topic, payload, *, qos, retain, on_fact):
        """Commit one validated delivery, application callback and receipt atomically."""

        def ingest():
            def callback(db, route, value):
                tx = SQLiteTransaction(db)

                async def invoke():
                    try:
                        await on_fact(route, value, tx)
                    finally:
                        tx._active = False

                future = asyncio.run_coroutine_threadsafe(invoke(), self._loop)
                while not future.done():
                    try:
                        statement, parameters, result = tx._operations.get(timeout=0.05)
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

            self._backend.on_fact = callback
            try:
                return self._backend.ingest(topic, payload, qos=qos, retain=retain)
            finally:
                self._backend.on_fact = None

        return await self._run(ingest)

    async def reject(self, topic, payload, reason):
        """Record a bounded diagnosis for invalid delivery bytes."""
        return await self._run(self._backend.reject, topic, payload, reason)

    async def pending_receipts(self):
        """Return pending (publication_id, topic, payload) tuples."""
        return await self._run(self._backend.pending_receipts)

    async def confirm_receipt(self, publication_id):
        """Remove a receipt following successful MQTT PUBACK."""
        return await self._run(self._backend.confirmed, publication_id)

    async def enqueue_command(self, request, now_ms):
        """Durably store a ConfigRequest and return whether it was newly queued."""
        return await self._run(self._controller.enqueue, request, now_ms=now_ms)

    async def due_commands(self, now_ms):
        """Return commands due for publication and record unconfirmed expiry."""
        return await self._run(self._controller.due, now_ms=now_ms)

    async def command_submitted(self, command_id, now_ms):
        """Record a publish attempt and schedule the next admission retry."""
        return await self._run(self._controller.submitted, command_id, now_ms=now_ms)

    async def observe_report(self, topic, payload, *, qos, retain):
        """Persist and deduplicate a report; return whether it advanced the state."""
        return await self._run(
            self._controller.observe, topic, payload, qos=qos, retain=retain
        )

    async def get_command(self, command_id):
        """Return a stored command dictionary, or None when the ID is unknown."""
        return await self._run(self._controller.get, command_id)

    async def close(self):
        """Close the database and its worker; this connector cannot be reused."""
        if self._db is not None:
            await self._run(self._db.close)
            self._db = None
        self._executor.shutdown(wait=True)
