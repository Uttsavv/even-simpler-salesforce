"""Run the reusable token-store contract against the library's in-memory stores."""

from __future__ import annotations

from collections.abc import Callable

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.auth.token import Token
from even_simpler_salesforce.testing import AsyncTokenStoreContract, TokenStoreContract

pytest_plugins = ["pytester"]


class TestInMemoryTokenStore(TokenStoreContract):
    @pytest.fixture
    def make_store(self) -> Callable[[], esf.InMemoryTokenStore]:
        return esf.InMemoryTokenStore


class TestAsyncInMemoryTokenStore(AsyncTokenStoreContract):
    @pytest.fixture
    def make_store(self) -> Callable[[], esf.AsyncInMemoryTokenStore]:
        return esf.AsyncInMemoryTokenStore


class TestAsyncFactoryIsAwaited(AsyncTokenStoreContract):
    concurrency = 4

    @pytest.fixture
    def make_store(self) -> Callable[[], object]:
        async def factory() -> esf.AsyncInMemoryTokenStore:
            return esf.AsyncInMemoryTokenStore()

        return factory


class _BrokenStore:
    """Ignores expected_version: must fail the CAS tests."""

    def __init__(self) -> None:
        self.data: dict[str, Token] = {}

    def load(self, key: str) -> Token | None:
        return self.data.get(key)

    def save(self, key: str, token: Token, *, expected_version: int | None) -> Token:
        cur = self.data.get(key)
        stored = token.replace(version=(cur.version if cur else 0) + 1)
        self.data[key] = stored
        return stored

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


def test_contract_catches_broken_store(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_broken=f"""
import pytest
from even_simpler_salesforce.testing import TokenStoreContract
from {__name__} import _BrokenStore


class TestBroken(TokenStoreContract):
    @pytest.fixture
    def make_store(self):
        return _BrokenStore
"""
    )
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=5, failed=2, skipped=1)
    result.stdout.fnmatch_lines(["*test_cas_conflict*", "*test_concurrent_cas_has_one_winner*"])


def test_base_class_without_override_skips(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_plain="""
from even_simpler_salesforce.testing import AsyncTokenStoreContract, TokenStoreContract

class TestA(TokenStoreContract):
    pass

class TestB(AsyncTokenStoreContract):
    pass
"""
    )
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(skipped=16)
