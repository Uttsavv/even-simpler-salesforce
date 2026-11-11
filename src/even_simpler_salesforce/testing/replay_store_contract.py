"""A reusable pytest suite that checks a ``ReplayStore`` / ``AsyncReplayStore`` (§9.16.4).

Subclass the base class in a ``Test*`` class and override the ``make_store`` fixture so it
returns a zero-argument factory producing a fresh, empty store::

    import pytest
    from even_simpler_salesforce.testing import AsyncReplayStoreContract, ReplayStoreContract

    class TestRedisReplayStore(ReplayStoreContract):
        @pytest.fixture
        def make_store(self, redis_url):
            return lambda: RedisReplayStore(redis_url, namespace=uuid4().hex)

    class TestAsyncPostgresReplayStore(AsyncReplayStoreContract):
        @pytest.fixture
        def make_store(self, dsn):
            async def factory():
                return await PostgresReplayStore.connect(dsn)   # may be sync or async
            return factory

Async tests run on a private event loop via ``asyncio.run`` (no pytest-asyncio needed).

Checks: a missing key loads ``None``; what is saved is loaded byte for byte (including zero
bytes and long ids); a save overwrites; keys of different orgs and topics are independent; a
second store instance from the same factory sees saved positions only if your store is meant
to persist (override ``persistent = False`` to skip that check); concurrent saves to different
keys all land.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest

from .._async.replay_store import AsyncReplayStore
from .._sync.replay_store import ReplayStore

__all__ = ["AsyncReplayStoreContract", "ReplayStoreContract", "sample_replay_id"]

ORG_A = "00D000000000001EAA"
ORG_B = "00D000000000002EAA"
TOPIC = "/data/AccountChangeEvent"
OTHER_TOPIC = "/event/Order_Event__e"
KEY = f"{ORG_A}:{TOPIC}"


def sample_replay_id(n: int = 1) -> bytes:
    """An opaque replay id like the ones Salesforce sends (``n`` makes them distinct)."""
    return b"\x00\x00\x00\x00\x00" + n.to_bytes(3, "big")


class ReplayStoreContract:
    """Contract tests for a sync :class:`~even_simpler_salesforce.ReplayStore`.

    Override the ``make_store`` fixture to return a factory for fresh stores. Set
    ``persistent = False`` for a store that is not shared between instances (like
    :class:`~even_simpler_salesforce.InMemoryReplayStore`).
    """

    persistent: bool = True
    concurrency: int = 16

    @pytest.fixture
    def make_store(self) -> Callable[[], ReplayStore]:
        """Override: return a zero-argument callable creating a fresh, empty store."""
        pytest.skip("override the make_store fixture to run the ReplayStore contract")

    @pytest.fixture
    def store(self, make_store: Callable[[], ReplayStore]) -> ReplayStore:
        """A fresh store from ``make_store``."""
        return make_store()

    def test_load_missing_returns_none(self, store: ReplayStore) -> None:
        """An unknown key loads ``None``."""
        assert store.load(KEY) is None

    def test_round_trip(self, store: ReplayStore) -> None:
        """What is saved is loaded byte for byte, as ``bytes``."""
        for value in (sample_replay_id(), b"\x00", bytes(range(256)) * 4):
            store.save(KEY, value)
            loaded = store.load(KEY)
            assert loaded == value
            assert type(loaded) is bytes

    def test_save_overwrites(self, store: ReplayStore) -> None:
        """A later save replaces the earlier position."""
        store.save(KEY, sample_replay_id(1))
        store.save(KEY, sample_replay_id(2))
        assert store.load(KEY) == sample_replay_id(2)

    def test_keys_are_independent(self, store: ReplayStore) -> None:
        """Orgs and topics never share a position."""
        store.save(f"{ORG_A}:{TOPIC}", sample_replay_id(1))
        store.save(f"{ORG_B}:{TOPIC}", sample_replay_id(2))
        store.save(f"{ORG_A}:{OTHER_TOPIC}", sample_replay_id(3))
        assert store.load(f"{ORG_A}:{TOPIC}") == sample_replay_id(1)
        assert store.load(f"{ORG_B}:{TOPIC}") == sample_replay_id(2)
        assert store.load(f"{ORG_A}:{OTHER_TOPIC}") == sample_replay_id(3)
        assert store.load(f"{ORG_B}:{OTHER_TOPIC}") is None

    def test_positions_persist_across_instances(self, make_store: Callable[[], ReplayStore]) -> None:
        """A store built again from the same settings resumes from the saved position."""
        if not self.persistent:
            pytest.skip("store is not persistent")
        make_store().save(KEY, sample_replay_id(7))
        assert make_store().load(KEY) == sample_replay_id(7)

    def test_concurrent_saves_to_different_keys(self, store: ReplayStore) -> None:
        """Saves from several threads (one subscription each) all land."""
        keys = [f"{ORG_A}:/event/E{i}__e" for i in range(self.concurrency)]

        def save(i: int) -> None:
            store.save(keys[i], sample_replay_id(i))

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            list(pool.map(save, range(len(keys))))
        assert [store.load(k) for k in keys] == [sample_replay_id(i) for i in range(len(keys))]


class AsyncReplayStoreContract:
    """Contract tests for an :class:`~even_simpler_salesforce.AsyncReplayStore`.

    ``make_store`` may return the store or an awaitable resolving to it.
    """

    persistent: bool = True
    concurrency: int = 16

    @pytest.fixture
    def make_store(self) -> Callable[[], AsyncReplayStore | Awaitable[AsyncReplayStore]]:
        """Override: return a zero-argument callable creating a fresh, empty store."""
        pytest.skip("override the make_store fixture to run the AsyncReplayStore contract")

    @staticmethod
    async def _make(factory: Callable[[], Any]) -> AsyncReplayStore:
        made = factory()
        return cast("AsyncReplayStore", await made if inspect.isawaitable(made) else made)

    def _run(self, factory: Callable[[], Any], body: Callable[[AsyncReplayStore], Awaitable[None]]) -> None:
        async def go() -> None:
            await body(await self._make(factory))

        asyncio.run(go())

    def test_load_missing_returns_none(self, make_store: Callable[[], Any]) -> None:
        """An unknown key loads ``None``."""

        async def body(store: AsyncReplayStore) -> None:
            assert await store.load(KEY) is None

        self._run(make_store, body)

    def test_round_trip(self, make_store: Callable[[], Any]) -> None:
        """What is saved is loaded byte for byte, as ``bytes``."""

        async def body(store: AsyncReplayStore) -> None:
            for value in (sample_replay_id(), b"\x00", bytes(range(256)) * 4):
                await store.save(KEY, value)
                loaded = await store.load(KEY)
                assert loaded == value
                assert type(loaded) is bytes

        self._run(make_store, body)

    def test_save_overwrites(self, make_store: Callable[[], Any]) -> None:
        """A later save replaces the earlier position."""

        async def body(store: AsyncReplayStore) -> None:
            await store.save(KEY, sample_replay_id(1))
            await store.save(KEY, sample_replay_id(2))
            assert await store.load(KEY) == sample_replay_id(2)

        self._run(make_store, body)

    def test_keys_are_independent(self, make_store: Callable[[], Any]) -> None:
        """Orgs and topics never share a position."""

        async def body(store: AsyncReplayStore) -> None:
            await store.save(f"{ORG_A}:{TOPIC}", sample_replay_id(1))
            await store.save(f"{ORG_B}:{TOPIC}", sample_replay_id(2))
            await store.save(f"{ORG_A}:{OTHER_TOPIC}", sample_replay_id(3))
            assert await store.load(f"{ORG_A}:{TOPIC}") == sample_replay_id(1)
            assert await store.load(f"{ORG_B}:{TOPIC}") == sample_replay_id(2)
            assert await store.load(f"{ORG_A}:{OTHER_TOPIC}") == sample_replay_id(3)
            assert await store.load(f"{ORG_B}:{OTHER_TOPIC}") is None

        self._run(make_store, body)

    def test_positions_persist_across_instances(self, make_store: Callable[[], Any]) -> None:
        """A store built again from the same settings resumes from the saved position."""
        if not self.persistent:
            pytest.skip("store is not persistent")

        async def go() -> None:
            await (await self._make(make_store)).save(KEY, sample_replay_id(7))
            assert await (await self._make(make_store)).load(KEY) == sample_replay_id(7)

        asyncio.run(go())

    def test_concurrent_saves_to_different_keys(self, make_store: Callable[[], Any]) -> None:
        """Concurrent saves (one subscription each) all land."""

        async def body(store: AsyncReplayStore) -> None:
            keys = [f"{ORG_A}:/event/E{i}__e" for i in range(self.concurrency)]
            await asyncio.gather(*(store.save(k, sample_replay_id(i)) for i, k in enumerate(keys)))
            assert [await store.load(k) for k in keys] == [sample_replay_id(i) for i in range(len(keys))]

        self._run(make_store, body)
