"""In-memory token store (§6.3)."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import AsyncIterator

from ..auth.token import Token
from ..errors import TokenConflict

__all__ = ["AsyncInMemoryTokenStore"]


class AsyncInMemoryTokenStore:
    """Dict + version counter. Lost on restart, which is fine for ``ClientCredentials`` and
    ``JwtBearer``. Use a durable store for ``WebServerFlow`` refresh tokens."""

    def __init__(self) -> None:
        self._data: dict[str, Token] = {}
        self._guard = threading.Lock()

    async def load(self, key: str) -> Token | None:
        with self._guard:
            return self._data.get(key)

    async def save(self, key: str, token: Token, *, expected_version: int | None) -> Token:
        with self._guard:
            current = self._data.get(key)
            if expected_version is not None:
                actual = current.version if current is not None else None
                if actual != expected_version:
                    raise TokenConflict(
                        f"token for key changed (expected version {expected_version}, found {actual})",
                        error_code="TOKEN_CONFLICT",
                    )
            stored = token.replace(version=(current.version if current is not None else 0) + 1)
            self._data[key] = stored
            return stored

    async def delete(self, key: str) -> None:
        with self._guard:
            self._data.pop(key, None)

    @contextlib.asynccontextmanager
    async def lock(self, key: str) -> AsyncIterator[None]:
        yield
