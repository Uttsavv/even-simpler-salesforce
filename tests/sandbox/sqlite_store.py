"""A SQLite-backed ``TokenStore`` for the sandbox and multi-process rotation tests (§6.3).

Test-only: it lives under ``tests/`` and is not part of the package. It shows what a durable
store has to do and is shared by several processes in the §19 rotation acceptance test:

- ``save(..., expected_version=v)`` is an atomic compare-and-swap. ``BEGIN IMMEDIATE`` takes
  SQLite's write lock before the version is read, so the read-compare-write cannot interleave
  with another writer in any thread or process.
- ``lock(key)`` is a cross-process exclusive lock: an OS file lock (``fcntl.flock`` on POSIX,
  ``msvcrt.locking`` on Windows) on a per-key lock file next to the database. Each call opens
  its own file descriptor, so it also excludes other threads of the same process.
- ``delete`` keeps a tombstone row (no tokens, version kept), so versions never go backwards
  and a CAS against a version from before the delete fails instead of succeeding by accident.

The refresh token is stored in clear text (contract rule 3 says it SHOULD be encrypted at
rest); the database file is created with mode 0600 and is meant for a sandbox org only.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from even_simpler_salesforce import Token, TokenConflict

__all__ = ["AsyncSqliteTokenStore", "SqliteTokenStore", "token_from_json", "token_to_json"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    key     TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    token   TEXT                -- JSON; NULL = deleted (tombstone keeps the version)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_dt(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def token_to_json(token: Token) -> str:
    """Serialise every field except ``version`` (the row owns it)."""
    return json.dumps(
        {
            "access_token": token.access_token,
            "instance_url": token.instance_url,
            "issued_at": _dt(token.issued_at),
            "refresh_token": token.refresh_token,
            "id_url": token.id_url,
            "org_id": token.org_id,
            "user_id": token.user_id,
            "scope": sorted(token.scope),
            "signature": token.signature,
            "expires_at": _dt(token.expires_at),
        },
        sort_keys=True,
    )


def token_from_json(raw: str, version: int) -> Token:
    d: dict[str, Any] = json.loads(raw)
    issued = _parse_dt(d.get("issued_at"))
    if issued is None:
        raise ValueError("stored token has no issued_at")
    return Token(
        access_token=d["access_token"],
        instance_url=d["instance_url"],
        issued_at=issued,
        refresh_token=d.get("refresh_token"),
        id_url=d.get("id_url"),
        org_id=d.get("org_id"),
        user_id=d.get("user_id"),
        scope=frozenset(d.get("scope") or ()),
        signature=d.get("signature"),
        expires_at=_parse_dt(d.get("expires_at")),
        version=version,
    )


class _FileLock:
    """Exclusive OS-level lock on ``path`` (blocks until acquired)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI only
                import msvcrt

                while True:
                    try:
                        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # retries for ~10 s, then raises
                        break
                    except OSError:
                        time.sleep(0.05)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if sys.platform == "win32":  # pragma: no cover
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class SqliteTokenStore:
    """Sync :class:`~even_simpler_salesforce.TokenStore` on a SQLite file.

    Safe to share between threads and processes: every operation opens its own connection.

    Args:
        path: Database file (created if missing, mode 0600).
        timeout: Seconds to wait for SQLite's write lock before failing.
    """

    def __init__(self, path: str | os.PathLike[str], *, timeout: float = 60.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600))
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # isolation_level=None: we issue BEGIN/COMMIT ourselves.
        conn = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        try:
            conn.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
            yield conn
        finally:
            conn.close()

    # --- TokenStore ------------------------------------------------------------------

    def load(self, key: str) -> Token | None:
        with self._connect() as conn:
            row = conn.execute("SELECT version, token FROM tokens WHERE key = ?", (key,)).fetchone()
        if row is None or row[1] is None:
            return None
        return token_from_json(row[1], int(row[0]))

    def save(self, key: str, token: Token, *, expected_version: int | None) -> Token:
        payload = token_to_json(token)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")  # take the write lock before reading the version
            try:
                row = conn.execute("SELECT version, token FROM tokens WHERE key = ?", (key,)).fetchone()
                old_version = int(row[0]) if row is not None else 0
                live_version = old_version if row is not None and row[1] is not None else None
                if expected_version is not None and live_version != expected_version:
                    raise TokenConflict(
                        f"token changed (expected version {expected_version}, found {live_version})",
                        error_code="TOKEN_CONFLICT",
                    )
                new_version = old_version + 1
                if row is None:
                    conn.execute(
                        "INSERT INTO tokens (key, version, token) VALUES (?, ?, ?)",
                        (key, new_version, payload),
                    )
                else:
                    cur = conn.execute(
                        "UPDATE tokens SET version = ?, token = ? WHERE key = ? AND version = ?",
                        (new_version, payload, key, old_version),
                    )
                    if cur.rowcount != 1:  # cannot happen under BEGIN IMMEDIATE; belt and braces
                        raise TokenConflict("concurrent token update", error_code="TOKEN_CONFLICT")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return token.replace(version=new_version)

    def delete(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("UPDATE tokens SET version = version + 1, token = NULL WHERE key = ?", (key,))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def lock_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()[:16]
        return self.path.with_name(f"{self.path.name}.{digest}.lock")

    @contextlib.contextmanager
    def lock(self, key: str) -> Iterator[None]:
        """Cross-process (and cross-thread) exclusive lock for ``key``."""
        fl = _FileLock(self.lock_path(key))
        fl.acquire()
        try:
            yield
        finally:
            fl.release()

    # --- extras used by the sandbox fixtures -------------------------------------------

    def get_meta(self, name: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (name,)).fetchone()
        return str(row[0]) if row is not None else None

    def set_meta(self, name: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (name, value))


class AsyncSqliteTokenStore:
    """Async twin of :class:`SqliteTokenStore` over the same file format.

    SQLite calls are short and run inline; waiting for the cross-process lock runs in a thread
    so it never blocks the event loop.
    """

    def __init__(self, path: str | os.PathLike[str], *, timeout: float = 60.0) -> None:
        self.sync = SqliteTokenStore(path, timeout=timeout)
        self.path = self.sync.path

    async def load(self, key: str) -> Token | None:
        return self.sync.load(key)

    async def save(self, key: str, token: Token, *, expected_version: int | None) -> Token:
        return self.sync.save(key, token, expected_version=expected_version)

    async def delete(self, key: str) -> None:
        self.sync.delete(key)

    @contextlib.asynccontextmanager
    async def lock(self, key: str) -> AsyncIterator[None]:
        fl = _FileLock(self.sync.lock_path(key))
        acquiring = asyncio.ensure_future(asyncio.to_thread(fl.acquire))
        try:
            await asyncio.shield(acquiring)
        except asyncio.CancelledError:
            # The thread may still get the lock after we stop waiting: release it then.
            acquiring.add_done_callback(lambda f: None if f.cancelled() or f.exception() else fl.release())
            raise
        try:
            yield
        finally:
            fl.release()
