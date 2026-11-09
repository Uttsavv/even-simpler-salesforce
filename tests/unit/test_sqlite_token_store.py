"""The test-only SQLite token store (``tests/sandbox/sqlite_store.py``) honours the §6.3 contract.

It backs the §19 multi-process rotation acceptance test, so it runs in normal CI here (no org
needed): the reusable ``TokenStoreContract`` suites plus cross-process CAS and lock checks.
"""

from __future__ import annotations

import multiprocessing
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import AsyncTokenStoreContract, TokenStoreContract
from even_simpler_salesforce.testing.token_store_contract import sample_token

from ..sandbox.sqlite_store import AsyncSqliteTokenStore, SqliteTokenStore

KEY = "user:1"


class TestSqliteTokenStore(TokenStoreContract):
    @pytest.fixture
    def make_store(self, tmp_path: Path) -> Callable[[], SqliteTokenStore]:
        return lambda: SqliteTokenStore(tmp_path / f"{uuid4().hex}.sqlite3")


class TestAsyncSqliteTokenStore(AsyncTokenStoreContract):
    @pytest.fixture
    def make_store(self, tmp_path: Path) -> Callable[[], AsyncSqliteTokenStore]:
        return lambda: AsyncSqliteTokenStore(tmp_path / f"{uuid4().hex}.sqlite3")


def test_delete_keeps_version_monotonic(tmp_path: Path) -> None:
    store = SqliteTokenStore(tmp_path / "t.sqlite3")
    first = store.save(KEY, sample_token(1), expected_version=None)
    store.delete(KEY)
    with pytest.raises(esf.TokenConflict):  # a CAS from before the delete must not succeed
        store.save(KEY, sample_token(2), expected_version=first.version)
    again = store.save(KEY, sample_token(3), expected_version=None)
    assert again.version > first.version


def test_two_instances_share_state(tmp_path: Path) -> None:
    a, b = SqliteTokenStore(tmp_path / "t.sqlite3"), SqliteTokenStore(tmp_path / "t.sqlite3")
    saved = a.save(KEY, sample_token(1), expected_version=None)
    loaded = b.load(KEY)
    assert loaded is not None
    assert loaded.version == saved.version
    assert loaded.refresh_token == sample_token(1).refresh_token


def test_meta_round_trip(tmp_path: Path) -> None:
    store = SqliteTokenStore(tmp_path / "t.sqlite3")
    assert store.get_meta("seed") is None
    store.set_meta("seed", "abc")
    store.set_meta("seed", "def")
    assert store.get_meta("seed") == "def"


def _cas_worker(path: str, n: int, out: multiprocessing.Queue[int]) -> None:  # pragma: no cover - child
    store = SqliteTokenStore(path)
    wins = 0
    for i in range(n):
        while True:
            cur = store.load(KEY)
            assert cur is not None
            try:
                store.save(KEY, sample_token(i), expected_version=cur.version)
                wins += 1
                break
            except esf.TokenConflict:
                continue
    out.put(wins)


def test_cross_process_cas_loses_no_update(tmp_path: Path) -> None:
    """4 processes x 50 read-CAS-retry increments → exactly 200 version bumps."""
    path = str(tmp_path / "t.sqlite3")
    store = SqliteTokenStore(path)
    base = store.save(KEY, sample_token(0), expected_version=None)
    ctx = multiprocessing.get_context("spawn")
    out: multiprocessing.Queue[int] = ctx.Queue()
    procs = [ctx.Process(target=_cas_worker, args=(path, 50, out)) for _ in range(4)]
    for p in procs:
        p.start()
    wins = [out.get(timeout=120) for _ in procs]
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    assert sum(wins) == 200
    final = store.load(KEY)
    assert final is not None
    assert final.version == base.version + 200


def _hold_lock(path: str, ready: multiprocessing.Event, hold: float) -> None:  # pragma: no cover - child
    store = SqliteTokenStore(path)
    with store.lock(KEY):
        ready.set()
        time.sleep(hold)


def test_lock_excludes_other_processes(tmp_path: Path) -> None:
    path = str(tmp_path / "t.sqlite3")
    store = SqliteTokenStore(path)
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    p = ctx.Process(target=_hold_lock, args=(path, ready, 0.5))
    p.start()
    try:
        assert ready.wait(60)
        started = time.monotonic()
        with store.lock(KEY):
            waited = time.monotonic() - started
        assert waited >= 0.2, f"lock acquired after {waited:.3f}s while another process held it"
    finally:
        p.join(timeout=60)
    assert p.exitcode == 0
    with store.lock("other-key"):  # different keys don't block each other
        pass
