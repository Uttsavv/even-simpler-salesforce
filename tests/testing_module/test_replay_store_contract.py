"""Run the reusable replay-store contract against the library's in-memory stores."""

from __future__ import annotations

from collections.abc import Callable

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import AsyncReplayStoreContract, ReplayStoreContract

pytest_plugins = ["pytester"]


class TestInMemoryReplayStore(ReplayStoreContract):
    persistent = False

    @pytest.fixture
    def make_store(self) -> Callable[[], esf.InMemoryReplayStore]:
        return esf.InMemoryReplayStore


class TestAsyncInMemoryReplayStore(AsyncReplayStoreContract):
    persistent = False

    @pytest.fixture
    def make_store(self) -> Callable[[], esf.AsyncInMemoryReplayStore]:
        return esf.AsyncInMemoryReplayStore


_SHARED = esf.InMemoryReplayStore()
_ASHARED = esf.AsyncInMemoryReplayStore()


class TestPersistentCheckRuns(ReplayStoreContract):
    """One store behind every factory call behaves like a persistent store."""

    @pytest.fixture
    def make_store(self) -> Callable[[], esf.InMemoryReplayStore]:
        return lambda: _SHARED


class TestAsyncFactoryIsAwaited(AsyncReplayStoreContract):
    concurrency = 4

    @pytest.fixture
    def make_store(self) -> Callable[[], object]:
        async def factory() -> esf.AsyncInMemoryReplayStore:
            return _ASHARED

        return factory


def test_in_memory_store_rejects_empty_ids() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        esf.InMemoryReplayStore().save("k", b"")
    assert "keys=0" in repr(esf.InMemoryReplayStore())
    assert isinstance(esf.InMemoryReplayStore(), esf.ReplayStore)
    assert isinstance(esf.AsyncInMemoryReplayStore(), esf.AsyncReplayStore)


def test_a_broken_store_fails_the_contract(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import pytest
        from even_simpler_salesforce.testing import ReplayStoreContract

        class Lossy:
            def __init__(self):
                self.data = {}
            def load(self, key):
                return self.data.get(key.split(":")[1])   # ignores the org
            def save(self, key, replay_id):
                self.data[key.split(":")[1]] = replay_id

        class TestLossy(ReplayStoreContract):
            persistent = False

            @pytest.fixture
            def make_store(self):
                return Lossy
        """
    )
    result = pytester.runpytest_inprocess("-q", "-p", "no:randomly")
    result.assert_outcomes(passed=4, failed=1, skipped=1)
    assert "test_keys_are_independent" in result.stdout.str()


def test_the_base_class_skips_without_a_factory(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        from even_simpler_salesforce.testing import AsyncReplayStoreContract, ReplayStoreContract

        class TestSync(ReplayStoreContract):
            pass

        class TestAsync(AsyncReplayStoreContract):
            pass
        """
    )
    result = pytester.runpytest_inprocess("-q", "-p", "no:randomly")
    result.assert_outcomes(skipped=12)
