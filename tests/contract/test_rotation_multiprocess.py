"""§19 acceptance criterion, mocked: 4 processes, 1,000 forced refreshes, one shared CAS store.

The real-org twin is ``tests/sandbox/test_rotation.py``. Here the token endpoint is the
``synthetic`` rotating fake in ``fake_rotation_org.py`` (behaviour from the official *OAuth 2.0
Refresh Token Flow* help page): reusing a rotated-out refresh token revokes the chain, so a
single lost update between processes turns into ``invalid_grant`` / ``ReauthorizationRequired``.

The store is ``tests/sandbox/sqlite_store.py`` (CAS + cross-process file lock). Workers are
``spawn``-ed processes; each runs the async or the sync client (both clients, mixed in one run).
A negative control removes the cross-process lock and shows the fake catches the race.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import even_simpler_salesforce as esf

from ..sandbox.rotation import RotationConfig, WorkerReport, run_rotation, summarise
from ..sandbox.sqlite_store import SqliteTokenStore
from .fake_rotation_org import CLIENT_ID, CLIENT_SECRET, ID_URL, INSTANCE, FakeOrg

KEY = "web_server:rotation-contract"
TOTAL = int(os.environ.get("ESF_ROTATION_MOCK_CYCLES", "1000"))
API_DELAY = float(os.environ.get("ESF_ROTATION_MOCK_API_DELAY", "0.002"))


def _setup(tmp_path: Path) -> tuple[FakeOrg, str]:
    org = FakeOrg(tmp_path / "org.sqlite3")
    org.create("RT-initial", "AT-initial")
    db = str(tmp_path / "tokens.sqlite3")
    SqliteTokenStore(db).save(
        KEY,
        esf.Token(
            access_token="AT-initial",
            instance_url=INSTANCE,
            issued_at=datetime.now(timezone.utc),
            refresh_token="RT-initial",
            id_url=ID_URL,
        ),
        expected_version=None,
    )
    return org, db


def _cfg(org: FakeOrg, db: str, rotations: int, install: str) -> RotationConfig:
    return RotationConfig(
        db_path=db,
        token_key=KEY,
        domain=INSTANCE,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri="https://app.example.com/cb",
        rotations=rotations,
        verify_every=10,
        install=install,
        install_arg=json.dumps({"path": org.path, "delay": 0.001, "api_delay": API_DELAY}),
    )


@pytest.mark.parametrize(
    ("kinds", "total"),
    [
        (("async", "sync", "async", "sync"), TOTAL),  # the acceptance run: both clients, 1,000
        (("async",) * 4, max(1, TOTAL // 4)),
        (("sync",) * 4, max(1, TOTAL // 4)),
    ],
    ids=["mixed", "async", "sync"],
)
def test_four_processes_never_hit_invalid_grant(tmp_path: Path, kinds: tuple[str, ...], total: int) -> None:
    org, db = _setup(tmp_path)
    reports = run_rotation(
        _cfg(org, db, total, "tests.contract.fake_rotation_org:install"), kinds, timeout=600
    )
    detail = summarise(reports)
    counters = org.counters()

    assert all(not r.errors for r in reports), detail
    assert sum(r.reauth_required for r in reports) == 0, detail
    assert counters["reuse"] == 0, counters  # nobody ever presented a rotated-out token
    assert counters["unknown"] == 0, counters
    assert counters["overlaps"] == 0, counters  # the store lock serialised every POST
    # Every refresh revokes the earlier access tokens (org-observed), so verify calls are often
    # superseded by another process's refresh; each such 401 was recovered by adopting the newer
    # stored token, never by an extra token POST (refreshes == posts below).
    assert counters["api_401"] == sum(r.rejected for r in reports), (counters, detail)
    assert counters["api_ok"] == sum(r.verified for r in reports) > 0, counters
    assert counters["refreshes"] == sum(r.posts for r in reports), (counters, detail)
    assert counters["refreshes"] >= total, detail  # the chain really rotated TOTAL times
    assert all(r.cycles_done > 0 for r in reports), detail  # every process took part

    # The final stored refresh token is the org's current one, and it still refreshes.
    final = SqliteTokenStore(db).load(KEY)
    assert final is not None
    assert final.refresh_token == org.current_refresh_token()
    check = run_rotation(_cfg(org, db, 1, "tests.contract.fake_rotation_org:install"), ("sync",), timeout=120)
    assert not check[0].errors, summarise(check)
    assert check[0].posts == 1, summarise(check)


# --- negative control: without the cross-process lock the fake must catch the race ----------


class _NoLockStore(SqliteTokenStore):
    @contextlib.contextmanager
    def lock(self, key: str) -> Iterator[None]:
        yield


@contextlib.contextmanager
def install_without_lock(arg: str | None) -> Iterator[None]:
    """Fake org + replace the worker's store lock with a no-op (CAS alone is not enough)."""
    from ..sandbox import rotation, sqlite_store
    from . import fake_rotation_org

    original_sync, original_async = rotation.SqliteTokenStore, rotation.AsyncSqliteTokenStore

    class _NoLockAsync(sqlite_store.AsyncSqliteTokenStore):
        @contextlib.asynccontextmanager
        async def lock(self, key: str) -> Any:
            yield

    rotation.SqliteTokenStore = _NoLockStore  # type: ignore[misc]
    rotation.AsyncSqliteTokenStore = _NoLockAsync  # type: ignore[misc]
    try:
        with fake_rotation_org.install(arg):
            yield
    finally:
        rotation.SqliteTokenStore, rotation.AsyncSqliteTokenStore = original_sync, original_async  # type: ignore[misc]


def test_negative_control_without_lock_the_chain_breaks(tmp_path: Path) -> None:
    org, db = _setup(tmp_path)
    reports: list[WorkerReport] = run_rotation(
        _cfg(org, db, 100, "tests.contract.test_rotation_multiprocess:install_without_lock"),
        ("sync",) * 4,
        timeout=600,
    )
    counters = org.counters()
    # Racing processes either collide in flight ("already being processed") or, worse, one
    # presents a refresh token another has just rotated out -> chain revoked -> invalid_grant.
    assert counters["overlaps"] + counters["reuse"] > 0, (counters, summarise(reports))
    if counters["reuse"]:
        assert sum(r.reauth_required for r in reports) > 0, summarise(reports)
