"""Token stores (§6.3): in-memory stores, CAS, protocol defaults, custom stores in the client."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import respx

import even_simpler_salesforce as esf
from even_simpler_salesforce.auth.store import store_lock_async, store_lock_sync

from ..conftest import DATA, TOKEN_URL, Harness, make_token, token_body


def test_in_memory_store_cas(h: Harness) -> None:
    store = h.new_store()
    assert isinstance(store, esf.AsyncTokenStore if h.is_async else esf.TokenStore)
    assert h.load(store, "k") is None
    s1 = h.run(store.save("k", make_token("a"), expected_version=None))
    assert s1.version == 1
    s2 = h.run(store.save("k", make_token("b"), expected_version=1))
    assert (s2.version, s2.access_token) == (2, "b")
    with pytest.raises(esf.TokenConflict) as ei:
        h.run(store.save("k", make_token("c"), expected_version=1))
    assert ei.value.error_code == "TOKEN_CONFLICT"
    with pytest.raises(esf.TokenConflict):
        h.run(store.save("new", make_token("c"), expected_version=0))
    h.run(store.delete("k"))
    h.run(store.delete("missing"))
    assert h.load(store, "k") is None


def test_in_memory_store_lock_is_a_context_manager(h: Harness) -> None:
    store = h.new_store()
    if h.is_async:

        async def use() -> None:
            async with store.lock("k"):
                pass

        h.run(use())
    else:
        with store.lock("k"):
            pass


class DictStoreAsync:
    """Minimal async store without a ``lock`` method."""

    def __init__(self) -> None:
        self.data: dict[str, esf.Token] = {}

    async def load(self, key: str) -> esf.Token | None:
        return self.data.get(key)

    async def save(self, key: str, token: esf.Token, *, expected_version: int | None) -> esf.Token:
        cur = self.data.get(key)
        if expected_version is not None and (cur.version if cur else None) != expected_version:
            raise esf.TokenConflict("conflict")
        stored = token.replace(version=(cur.version if cur else 0) + 1)
        self.data[key] = stored
        return stored

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


class DictStoreSync:
    def __init__(self) -> None:
        self.data: dict[str, esf.Token] = {}

    def load(self, key: str) -> esf.Token | None:
        return self.data.get(key)

    def save(self, key: str, token: esf.Token, *, expected_version: int | None) -> esf.Token:
        cur = self.data.get(key)
        if expected_version is not None and (cur.version if cur else None) != expected_version:
            raise esf.TokenConflict("conflict")
        stored = token.replace(version=(cur.version if cur else 0) + 1)
        self.data[key] = stored
        return stored

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


def test_custom_store_without_lock(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(TOKEN_URL).respond(json=token_body("AT-custom"))
    route = mock.get(f"{DATA}/limits").respond(json={})
    store: Any = DictStoreAsync() if h.is_async else DictStoreSync()
    sf = h.client(store=store, token=False)
    h.run(sf.limits())
    assert route.calls.last.request.headers["Authorization"] == "Bearer AT-custom"
    (tok,) = store.data.values()
    assert tok.version == 1


class LockingAsync(DictStoreAsync, esf.AsyncTokenStore):
    """Subclasses the protocol and inherits its default no-op ``lock``."""


class LockingSync(DictStoreSync, esf.TokenStore):
    pass


class CountingLockAsync(DictStoreAsync):
    def __init__(self) -> None:
        super().__init__()
        self.locked = 0

    @contextlib.asynccontextmanager
    async def lock(self, key: str) -> AsyncIterator[None]:
        self.locked += 1
        yield


class CountingLockSync(DictStoreSync):
    def __init__(self) -> None:
        super().__init__()
        self.locked = 0

    @contextlib.contextmanager
    def lock(self, key: str) -> Iterator[None]:
        self.locked += 1
        yield


def test_protocol_default_lock_and_custom_lock(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(TOKEN_URL).respond(json=token_body("AT-x"))
    mock.get(f"{DATA}/limits").respond(json={})
    default: Any = LockingAsync() if h.is_async else LockingSync()
    h.run(h.client(store=default, token=False).limits())
    counting: Any = CountingLockAsync() if h.is_async else CountingLockSync()
    h.run(h.client(store=counting, token=False).limits())
    assert counting.locked == 1  # the cross-process lock is taken around the token request


def test_store_lock_helpers_fallback(arun: Any) -> None:
    class NoLock:
        pass

    async def use() -> None:
        async with store_lock_async(NoLock(), "k"):
            pass
        async with esf.AsyncTokenStore.lock(NoLock(), "k"):  # type: ignore[arg-type]
            pass

    arun(use())
    with store_lock_sync(NoLock(), "k"):
        pass
    with esf.TokenStore.lock(NoLock(), "k"):  # type: ignore[arg-type]
        pass
