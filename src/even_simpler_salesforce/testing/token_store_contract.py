"""A reusable pytest suite that checks a ``TokenStore`` / ``AsyncTokenStore`` honours §6.3.

Subclass the base class in a ``Test*`` class and override the ``make_store`` fixture so it
returns a zero-argument factory producing a fresh, empty store::

    import pytest
    from even_simpler_salesforce.testing import AsyncTokenStoreContract, TokenStoreContract

    class TestRedisStore(TokenStoreContract):
        @pytest.fixture
        def make_store(self, redis_url):
            return lambda: RedisTokenStore(redis_url, namespace=uuid4().hex)

    class TestAsyncPostgresStore(AsyncTokenStoreContract):
        @pytest.fixture
        def make_store(self, dsn):
            async def factory():
                return await PostgresTokenStore.connect(dsn)   # may be sync or async
            return factory

Async tests run on a private event loop via ``asyncio.run`` (no pytest-asyncio needed), so an
async factory is awaited inside that loop. Set ``concurrency`` on the subclass to change the
number of concurrent writers in the race tests (default 16).

Checks: round-trip, versions increase, compare-and-swap conflicts raise
``esf.TokenConflict`` and leave the stored token untouched, ``delete`` removes both tokens
(and is idempotent), keys are isolated, and concurrent CAS saves have exactly one winner.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest

from ..auth.store import AsyncTokenStore, TokenStore
from ..auth.token import Token
from ..errors import TokenConflict

__all__ = ["AsyncTokenStoreContract", "TokenStoreContract", "sample_token"]

_KEY = "esf-contract:user:1"


def sample_token(n: int = 1, *, refresh: bool = True) -> Token:
    """A realistic token for contract tests (``n`` makes the values distinct)."""
    issued = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=n)
    return Token(
        access_token=f"00Dfake!AQ.access-{n}",
        instance_url="https://example.my.salesforce.com",
        issued_at=issued,
        refresh_token=f"5Aep861.refresh-{n}" if refresh else None,
        id_url="https://login.salesforce.com/id/00D000000000001EAA/005000000000001AAA",
        org_id="00D000000000001EAA",
        user_id="005000000000001AAA",
        scope=frozenset({"api", "refresh_token"}),
        signature=f"sig-{n}",
        expires_at=issued + timedelta(hours=2),
    )


def _same(a: Token, b: Token) -> bool:
    return a.replace(version=0) == b.replace(version=0)


class TokenStoreContract:
    """Contract tests for a sync :class:`~even_simpler_salesforce.TokenStore`.

    Override the ``make_store`` fixture to return a factory for fresh stores.
    """

    concurrency: int = 16

    @pytest.fixture
    def make_store(self) -> Callable[[], TokenStore]:
        """Override: return a zero-argument callable creating a fresh, empty store."""
        pytest.skip("override the make_store fixture to run the TokenStore contract")

    @pytest.fixture
    def store(self, make_store: Callable[[], TokenStore]) -> TokenStore:
        """A fresh store from ``make_store``."""
        return make_store()

    def test_load_missing_returns_none(self, store: TokenStore) -> None:
        """Loading an unknown key returns ``None``."""
        assert store.load(_KEY) is None

    def test_round_trip(self, store: TokenStore) -> None:
        """What is saved is what is loaded (all fields)."""
        tok = sample_token()
        saved = store.save(_KEY, tok, expected_version=None)
        loaded = store.load(_KEY)
        assert loaded is not None
        assert _same(loaded, tok)
        assert loaded.version == saved.version

    def test_versions_increase(self, store: TokenStore) -> None:
        """Each save returns a strictly greater version."""
        v1 = store.save(_KEY, sample_token(1), expected_version=None).version
        v2 = store.save(_KEY, sample_token(2), expected_version=v1).version
        v3 = store.save(_KEY, sample_token(3), expected_version=None).version
        assert v1 < v2 < v3
        loaded = store.load(_KEY)
        assert loaded is not None
        assert loaded.version == v3

    def test_cas_conflict(self, store: TokenStore) -> None:
        """A stale ``expected_version`` raises ``TokenConflict`` and changes nothing."""
        first = store.save(_KEY, sample_token(1), expected_version=None)
        store.save(_KEY, sample_token(2), expected_version=first.version)
        with pytest.raises(TokenConflict):
            store.save(_KEY, sample_token(3), expected_version=first.version)
        loaded = store.load(_KEY)
        assert loaded is not None
        assert _same(loaded, sample_token(2))

    def test_cas_on_missing_key_conflicts(self, store: TokenStore) -> None:
        """Expecting a version for a key that has none raises ``TokenConflict``."""
        with pytest.raises(TokenConflict):
            store.save(_KEY, sample_token(1), expected_version=1)
        assert store.load(_KEY) is None

    def test_delete_removes_both_tokens(self, store: TokenStore) -> None:
        """``delete`` removes access and refresh token; deleting again is a no-op."""
        store.save(_KEY, sample_token(), expected_version=None)
        store.delete(_KEY)
        assert store.load(_KEY) is None
        store.delete(_KEY)
        fresh = store.save(_KEY, sample_token(9, refresh=False), expected_version=None)
        loaded = store.load(_KEY)
        assert loaded is not None
        assert loaded.refresh_token is None
        assert fresh.version >= 1

    def test_keys_are_isolated(self, store: TokenStore) -> None:
        """Writes to one key never affect another."""
        store.save(_KEY, sample_token(1), expected_version=None)
        store.save(_KEY + ":other", sample_token(2), expected_version=None)
        store.delete(_KEY + ":other")
        loaded = store.load(_KEY)
        assert loaded is not None
        assert _same(loaded, sample_token(1))

    def test_lock_is_usable(self, store: TokenStore) -> None:
        """``lock(key)``, when implemented, is a context manager."""
        lock = getattr(store, "lock", None)
        if lock is None:
            pytest.skip("store has no lock()")
        with lock(_KEY):
            pass

    def test_lock_excludes_a_second_holder(self, store: TokenStore) -> None:
        """While one thread holds ``lock(key)``, another cannot enter it."""
        lock = getattr(store, "lock", None)
        if lock is None:
            pytest.skip("store has no lock()")
        held, release, entered = threading.Event(), threading.Event(), threading.Event()

        def first() -> None:
            with lock(_KEY):
                held.set()
                release.wait(5)

        def second() -> None:
            held.wait(5)
            with lock(_KEY):
                entered.set()

        a, b = threading.Thread(target=first), threading.Thread(target=second)
        a.start()
        b.start()
        try:
            assert held.wait(5)
            assert not entered.wait(0.2), "a second holder entered lock() while it was held"
        finally:
            release.set()
            a.join(5)
            b.join(5)
        assert entered.is_set()

    def test_concurrent_cas_has_one_winner(self, store: TokenStore) -> None:
        """N threads CAS-saving against the same version: exactly one wins."""
        base = store.save(_KEY, sample_token(0), expected_version=None)
        n = self.concurrency
        barrier = threading.Barrier(n)

        def attempt(i: int) -> Token | None:
            barrier.wait()
            try:
                return store.save(_KEY, sample_token(i + 1), expected_version=base.version)
            except TokenConflict:
                return None

        with ThreadPoolExecutor(max_workers=n) as pool:
            results = list(pool.map(attempt, range(n)))
        winners = [r for r in results if r is not None]
        assert len(winners) == 1, f"{len(winners)} concurrent CAS saves succeeded"
        loaded = store.load(_KEY)
        assert loaded is not None
        assert loaded.version == winners[0].version
        assert _same(loaded, winners[0])


class AsyncTokenStoreContract:
    """Contract tests for an :class:`~even_simpler_salesforce.AsyncTokenStore`.

    Override the ``make_store`` fixture to return a factory (sync or async) for fresh stores.
    """

    concurrency: int = 16

    @pytest.fixture
    def make_store(self) -> Callable[[], AsyncTokenStore | Awaitable[AsyncTokenStore]]:
        """Override: return a zero-argument callable creating a fresh, empty store."""
        pytest.skip("override the make_store fixture to run the AsyncTokenStore contract")

    @staticmethod
    def _run(
        make_store: Callable[[], Any],
        body: Callable[[AsyncTokenStore], Awaitable[None]],
    ) -> None:
        async def main() -> None:
            made = make_store()
            store = cast("AsyncTokenStore", await made if inspect.isawaitable(made) else made)
            try:
                await body(store)
            finally:
                closer = getattr(store, "aclose", None) or getattr(store, "close", None)
                if callable(closer):
                    res = closer()
                    if inspect.isawaitable(res):
                        await res

        asyncio.run(main())

    def test_load_missing_returns_none(self, make_store: Callable[[], Any]) -> None:
        """Loading an unknown key returns ``None``."""

        async def body(store: AsyncTokenStore) -> None:
            assert await store.load(_KEY) is None

        self._run(make_store, body)

    def test_round_trip(self, make_store: Callable[[], Any]) -> None:
        """What is saved is what is loaded (all fields)."""

        async def body(store: AsyncTokenStore) -> None:
            tok = sample_token()
            saved = await store.save(_KEY, tok, expected_version=None)
            loaded = await store.load(_KEY)
            assert loaded is not None
            assert _same(loaded, tok)
            assert loaded.version == saved.version

        self._run(make_store, body)

    def test_versions_increase(self, make_store: Callable[[], Any]) -> None:
        """Each save returns a strictly greater version."""

        async def body(store: AsyncTokenStore) -> None:
            v1 = (await store.save(_KEY, sample_token(1), expected_version=None)).version
            v2 = (await store.save(_KEY, sample_token(2), expected_version=v1)).version
            v3 = (await store.save(_KEY, sample_token(3), expected_version=None)).version
            assert v1 < v2 < v3

        self._run(make_store, body)

    def test_cas_conflict(self, make_store: Callable[[], Any]) -> None:
        """A stale ``expected_version`` raises ``TokenConflict`` and changes nothing."""

        async def body(store: AsyncTokenStore) -> None:
            first = await store.save(_KEY, sample_token(1), expected_version=None)
            await store.save(_KEY, sample_token(2), expected_version=first.version)
            with pytest.raises(TokenConflict):
                await store.save(_KEY, sample_token(3), expected_version=first.version)
            loaded = await store.load(_KEY)
            assert loaded is not None
            assert _same(loaded, sample_token(2))

        self._run(make_store, body)

    def test_cas_on_missing_key_conflicts(self, make_store: Callable[[], Any]) -> None:
        """Expecting a version for a key that has none raises ``TokenConflict``."""

        async def body(store: AsyncTokenStore) -> None:
            with pytest.raises(TokenConflict):
                await store.save(_KEY, sample_token(1), expected_version=1)
            assert await store.load(_KEY) is None

        self._run(make_store, body)

    def test_delete_removes_both_tokens(self, make_store: Callable[[], Any]) -> None:
        """``delete`` removes access and refresh token; deleting again is a no-op."""

        async def body(store: AsyncTokenStore) -> None:
            await store.save(_KEY, sample_token(), expected_version=None)
            await store.delete(_KEY)
            assert await store.load(_KEY) is None
            await store.delete(_KEY)
            await store.save(_KEY, sample_token(9, refresh=False), expected_version=None)
            loaded = await store.load(_KEY)
            assert loaded is not None
            assert loaded.refresh_token is None

        self._run(make_store, body)

    def test_keys_are_isolated(self, make_store: Callable[[], Any]) -> None:
        """Writes to one key never affect another."""

        async def body(store: AsyncTokenStore) -> None:
            await store.save(_KEY, sample_token(1), expected_version=None)
            await store.save(_KEY + ":other", sample_token(2), expected_version=None)
            await store.delete(_KEY + ":other")
            loaded = await store.load(_KEY)
            assert loaded is not None
            assert _same(loaded, sample_token(1))

        self._run(make_store, body)

    def test_lock_is_usable(self, make_store: Callable[[], Any]) -> None:
        """``lock(key)``, when implemented, is an async context manager."""

        async def body(store: AsyncTokenStore) -> None:
            lock = getattr(store, "lock", None)
            if lock is None:
                return
            async with lock(_KEY):
                pass

        self._run(make_store, body)

    def test_lock_excludes_a_second_holder(self, make_store: Callable[[], Any]) -> None:
        """While one task holds ``lock(key)``, another cannot enter it."""

        async def body(store: AsyncTokenStore) -> None:
            lock = getattr(store, "lock", None)
            if lock is None:
                return
            entered = asyncio.Event()

            async def second() -> None:
                async with lock(_KEY):
                    entered.set()

            async with lock(_KEY):
                task = asyncio.ensure_future(second())
                await asyncio.sleep(0.05)
                assert not entered.is_set(), "a second holder entered lock() while it was held"
            await asyncio.wait_for(task, 5)
            assert entered.is_set()

        self._run(make_store, body)

    def test_concurrent_cas_has_one_winner(self, make_store: Callable[[], Any]) -> None:
        """N tasks CAS-saving against the same version: exactly one wins."""
        n = self.concurrency

        async def body(store: AsyncTokenStore) -> None:
            base = await store.save(_KEY, sample_token(0), expected_version=None)
            start = asyncio.Event()

            async def attempt(i: int) -> Token | None:
                await start.wait()
                try:
                    return await store.save(_KEY, sample_token(i + 1), expected_version=base.version)
                except TokenConflict:
                    return None

            tasks = [asyncio.ensure_future(attempt(i)) for i in range(n)]
            await asyncio.sleep(0)
            start.set()
            results = await asyncio.gather(*tasks)
            winners = [r for r in results if r is not None]
            assert len(winners) == 1, f"{len(winners)} concurrent CAS saves succeeded"
            loaded = await store.load(_KEY)
            assert loaded is not None
            assert loaded.version == winners[0].version

        self._run(make_store, body)
