"""Token store protocols (§6.3). Implementations: ``AsyncInMemoryTokenStore`` /
``InMemoryTokenStore`` (in ``_async``/``_sync``) or your own durable store."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from typing import Protocol, runtime_checkable

from .token import Token

__all__ = ["AsyncTokenStore", "TokenStore"]


@runtime_checkable
class AsyncTokenStore(Protocol):
    """Async token store.

    Contract for durable stores:

    1. One record per Salesforce identity, not per app user.
    2. ``save`` with ``expected_version`` MUST be atomic (compare-and-swap or row lock).
    3. Refresh tokens SHOULD be encrypted at rest.
    4. ``delete`` MUST remove both tokens.

    Use ``esf.testing.token_store_contract`` to check your implementation.
    """

    async def load(self, key: str) -> Token | None:
        """Return the stored token (with its ``version``) for ``key``, or ``None``."""
        ...

    async def save(self, key: str, token: Token, *, expected_version: int | None) -> Token:
        """Persist ``token``. If ``expected_version`` is not ``None`` and the stored version
        differs, raise :class:`TokenConflict`. Returns the stored token with its new version."""
        ...

    async def delete(self, key: str) -> None:
        """Remove the whole record for ``key`` (access and refresh token); no-op if missing."""
        ...

    def lock(self, key: str) -> AbstractAsyncContextManager[None]:
        """Optional cross-process lock. The default is a no-op; in-process single-flight is
        always applied regardless."""
        return _async_noop()


@runtime_checkable
class TokenStore(Protocol):
    """Sync twin of :class:`AsyncTokenStore` with the same contract."""

    def load(self, key: str) -> Token | None:
        """Return the stored token (with its ``version``) for ``key``, or ``None``."""
        ...

    def save(self, key: str, token: Token, *, expected_version: int | None) -> Token:
        """Persist ``token``; compare-and-swap when ``expected_version`` is not ``None``.

        Returns the stored token with its new version; raises :class:`TokenConflict` on a
        version mismatch.
        """
        ...

    def delete(self, key: str) -> None:
        """Remove the whole record for ``key`` (access and refresh token); no-op if missing."""
        ...

    def lock(self, key: str) -> AbstractContextManager[None]:
        """Optional cross-process lock around refreshes. The default is a no-op."""
        return _sync_noop()


@contextlib.asynccontextmanager
async def _async_noop() -> AsyncIterator[None]:
    yield


@contextlib.contextmanager
def _sync_noop() -> Iterator[None]:
    yield


def store_lock_async(store: object, key: str) -> AbstractAsyncContextManager[None]:
    fn = getattr(store, "lock", None)
    return fn(key) if callable(fn) else _async_noop()  # pyright: ignore[reportReturnType]


def store_lock_sync(store: object, key: str) -> AbstractContextManager[None]:
    fn = getattr(store, "lock", None)
    return fn(key) if callable(fn) else _sync_noop()  # pyright: ignore[reportReturnType]
