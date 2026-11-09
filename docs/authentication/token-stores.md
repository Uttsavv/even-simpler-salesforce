# Token stores

The client keeps the current access token in memory and uses a **token store** for anything
that must outlive the process or be shared between processes. That is mainly `WebServerFlow`
refresh tokens, but you can also share client-credentials and JWT tokens to save token
requests.

| Client | Store protocol | Built-in store |
|---|---|---|
| `esf.AsyncSalesforce` | `esf.AsyncTokenStore` (async methods) | `esf.AsyncInMemoryTokenStore` (default) |
| `esf.Salesforce` | `esf.TokenStore` (same methods, sync) | `esf.InMemoryTokenStore` (default) |

```python
import even_simpler_salesforce as esf

store = esf.InMemoryTokenStore()
auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
with esf.Salesforce(auth, store=store) as sf:
    sf.limits()
    print(store.load(auth.token_key))
```

The in-memory stores are a dict plus a version counter and a per-key lock. Their contents are
lost on restart, which is fine for `ClientCredentials` and `JwtBearer` (the process just
requests a new token). For `WebServerFlow` in production, use a durable store.

## The protocol

```python
import contextlib
from collections.abc import Iterator

from even_simpler_salesforce import Token


class MyTokenStore:  # satisfies esf.TokenStore
    def load(self, key: str) -> Token | None: ...

    def save(self, key: str, token: Token, *, expected_version: int | None) -> Token:
        """Persist and return the token with its new version; raise esf.TokenConflict if
        expected_version is not None and differs from the stored version."""
        ...

    def delete(self, key: str) -> None: ...

    @contextlib.contextmanager
    def lock(self, key: str) -> Iterator[None]:  # optional
        yield
```

The async protocol is the same with `async def` methods. Its `lock()` returns an async context
manager.

| Method | Called when | Must |
|---|---|---|
| `load(key)` | the process has no token in memory, before every refresh, and after an ambiguous refresh | return the stored token with its `version`, or `None` |
| `save(key, token, expected_version=...)` | after a token was issued (initial, refreshed, or exchanged) | if `expected_version` is not `None`, compare-and-swap atomically; return the token with the new, strictly greater `version` |
| `delete(key)` | `sf.auth.revoke()` | remove the whole record, access **and** refresh token; deleting a missing key is a no-op |
| `lock(key)` *(optional)* | around each refresh | serialise refreshes for `key` **across processes**; the default does nothing |

In-process single-flight always applies, whether or not the store has a lock. A store lock is
only needed when several processes share a store. Without it, compare-and-swap still keeps the
stored data consistent, but two processes may both send a refresh request.

## Contract for durable stores

1. **One record per Salesforce identity, not per app user.** If two of your users share one
   Salesforce connection, they must share one record (one `token_key`). Copies of a refresh
   token in several records break as soon as rotation invalidates one of them.
2. **`save` with `expected_version` must be atomic**: a compare-and-swap, a row lock or a
   transaction. This is what makes refresh safe when several processes share a store. When
   `save` raises `TokenConflict`, the client re-reads the store and uses the token the other
   process saved.
3. **Encrypt refresh tokens at rest.** A refresh token is a long-lived credential.
4. **`delete` removes both tokens.**

`Token` is a frozen dataclass, so a store serialises it however it likes. This helper is used by
the examples below:

```python
import json
from datetime import datetime

from even_simpler_salesforce import Token


def token_to_json(token: Token) -> str:
    return json.dumps({
        "access_token": token.access_token,
        "instance_url": token.instance_url,
        "issued_at": token.issued_at.isoformat(),
        "refresh_token": token.refresh_token,
        "id_url": token.id_url,
        "org_id": token.org_id,
        "user_id": token.user_id,
        "scope": sorted(token.scope),
        "signature": token.signature,
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
    })


def token_from_json(data: str, version: int) -> Token:
    d = json.loads(data)
    return Token(
        access_token=d["access_token"],
        instance_url=d["instance_url"],
        issued_at=datetime.fromisoformat(d["issued_at"]),
        refresh_token=d["refresh_token"],
        id_url=d["id_url"],
        org_id=d["org_id"],
        user_id=d["user_id"],
        scope=frozenset(d["scope"]),
        signature=d["signature"],
        expires_at=datetime.fromisoformat(d["expires_at"]) if d["expires_at"] else None,
        version=version,
    )
```

## Checking your store: `TokenStoreContract`

`even_simpler_salesforce.testing` ships the contract as a pytest suite. Subclass
`TokenStoreContract` (sync) or `AsyncTokenStoreContract` (async) in a `Test*` class and
override the `make_store` fixture so that it returns a zero-argument factory for fresh, empty
stores. The suite checks:

- loading a missing key returns `None`, and a save/load round trip keeps every field;
- versions strictly increase;
- a stale `expected_version` raises `esf.TokenConflict` and leaves the stored token unchanged;
- `delete` removes both tokens and is idempotent;
- keys are isolated;
- `lock(key)`, if present, is usable as a context manager;
- N concurrent compare-and-swap saves against the same version have **exactly one winner**
  (threads for the sync suite, tasks for the async one; set `concurrency = N` on the class,
  default 16).

The async suite runs each test on its own event loop with `asyncio.run`, so it doesn't need
pytest-asyncio. `make_store` may return a sync or an async factory. If the store has `aclose()`
or `close()`, it is called after each test.

Here is a complete durable store on SQLite (standard library only), with the contract run
against it. The docs test suite runs this example:

```python
import contextlib
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import TokenStoreContract


class SqliteTokenStore:
    """Durable esf.TokenStore on one SQLite file. CAS via BEGIN IMMEDIATE + a version column.

    lock() is per process only. SQLite has no cross-process advisory lock, but the CAS
    still keeps the stored token consistent when several processes share the file.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        with contextlib.closing(self._connect()) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS esf_tokens ("
                " key TEXT PRIMARY KEY, data TEXT NOT NULL, version INTEGER NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30, isolation_level=None)

    def load(self, key: str) -> esf.Token | None:
        with contextlib.closing(self._connect()) as db:
            row = db.execute("SELECT data, version FROM esf_tokens WHERE key = ?", (key,)).fetchone()
        return None if row is None else token_from_json(row[0], row[1])

    def save(self, key: str, token: esf.Token, *, expected_version: int | None) -> esf.Token:
        with contextlib.closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")  # take the write lock before reading the version
            try:
                row = db.execute("SELECT version FROM esf_tokens WHERE key = ?", (key,)).fetchone()
                current = row[0] if row else None
                if expected_version is not None and current != expected_version:
                    raise esf.TokenConflict(f"expected version {expected_version}, found {current}")
                version = (current or 0) + 1
                db.execute(
                    "INSERT INTO esf_tokens (key, data, version) VALUES (?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET data = excluded.data, version = excluded.version",
                    (key, token_to_json(token), version),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return token.replace(version=version)

    def delete(self, key: str) -> None:
        with contextlib.closing(self._connect()) as db:
            db.execute("DELETE FROM esf_tokens WHERE key = ?", (key,))

    @contextlib.contextmanager
    def lock(self, key: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            yield


class TestSqliteTokenStore(TokenStoreContract):
    @pytest.fixture
    def make_store(self, tmp_path):
        counter = iter(range(1_000_000))
        return lambda: SqliteTokenStore(tmp_path / f"tokens-{next(counter)}.db")
```

Use it like the built-in stores: `esf.Salesforce(auth, store=SqliteTokenStore("tokens.db"))`.

## Example: SQLAlchemy (PostgreSQL)

!!! warning "Untested example, not part of the package"
    This store is an illustration for your own code. The library's test suite doesn't run it
    (it needs a PostgreSQL server). Run `TokenStoreContract` against your version before you
    rely on it.

It uses a **version column** for compare-and-swap, `SELECT … FOR UPDATE` to make the
check-and-write atomic, a PostgreSQL **advisory lock** for `lock()` (so refreshes are serialised
across processes without blocking the row that `save` updates), and
[Fernet](https://cryptography.io/en/latest/fernet/) to encrypt tokens at rest.

<!-- esf: skip -->
```python
import contextlib
from collections.abc import Iterator

from cryptography.fernet import Fernet
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

import even_simpler_salesforce as esf

metadata = MetaData()
esf_tokens = Table(
    "esf_tokens",
    metadata,
    Column("key", String(255), primary_key=True),
    Column("data", Text, nullable=False),        # Fernet-encrypted token_to_json(...)
    Column("version", Integer, nullable=False),
)


class SqlAlchemyTokenStore:
    def __init__(self, engine: Engine, fernet: Fernet) -> None:
        self.engine = engine
        self.fernet = fernet

    def load(self, key: str) -> esf.Token | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(esf_tokens.c.data, esf_tokens.c.version).where(esf_tokens.c.key == key)
            ).first()
        if row is None:
            return None
        return token_from_json(self.fernet.decrypt(row.data.encode()).decode(), row.version)

    def save(self, key: str, token: esf.Token, *, expected_version: int | None) -> esf.Token:
        data = self.fernet.encrypt(token_to_json(token).encode()).decode()
        try:
            with self.engine.begin() as conn:  # one transaction; row lock until commit
                row = conn.execute(
                    select(esf_tokens.c.version).where(esf_tokens.c.key == key).with_for_update()
                ).first()
                current = row.version if row is not None else None
                if expected_version is not None and current != expected_version:
                    raise esf.TokenConflict(f"expected version {expected_version}, found {current}")
                version = (current or 0) + 1
                if row is None:
                    conn.execute(esf_tokens.insert().values(key=key, data=data, version=version))
                else:
                    conn.execute(
                        esf_tokens.update()
                        .where(esf_tokens.c.key == key)
                        .values(data=data, version=version)
                    )
        except IntegrityError:  # another process inserted the first row concurrently
            raise esf.TokenConflict("token record was created concurrently") from None
        return token.replace(version=version)

    def delete(self, key: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(esf_tokens.delete().where(esf_tokens.c.key == key))

    @contextlib.contextmanager
    def lock(self, key: str) -> Iterator[None]:
        with self.engine.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(hashtext(:k))"), {"k": key})
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": key})
                conn.commit()
```

For `AsyncSalesforce`, write the same thing with `async def` methods on an SQLAlchemy
`AsyncEngine` (`async with engine.begin() as conn: await conn.execute(...)`) and
`contextlib.asynccontextmanager` for `lock()`, then run `AsyncTokenStoreContract` against it.

## Example: Redis

!!! warning "Untested example, not part of the package"
    The library's test suite doesn't run this store (it needs a Redis server). Run
    `TokenStoreContract` against your version before you rely on it.

It uses a **Lua script** for an atomic compare-and-swap on a hash (`data`, `version`), and a
**`SET NX PX`** lock with a random owner value, released by a second script only by its owner.
The lock has a TTL, so a crashed process can't hold it for ever. Pick a TTL longer than a token
request takes.

<!-- esf: skip -->
```python
import contextlib
import secrets
import time
from collections.abc import Iterator

import redis
from cryptography.fernet import Fernet

import even_simpler_salesforce as esf

CAS = """
local current = redis.call('HGET', KEYS[1], 'version')
if ARGV[1] ~= '' and current ~= ARGV[1] then
  return -1
end
local version = (tonumber(current) or 0) + 1
redis.call('HSET', KEYS[1], 'data', ARGV[2], 'version', version)
return version
"""

UNLOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisTokenStore:
    def __init__(self, client: redis.Redis, fernet: Fernet, *, prefix: str = "esf:token:",
                 lock_ttl: float = 30.0, lock_wait: float = 35.0) -> None:
        self.r = client
        self.fernet = fernet
        self.prefix = prefix
        self.lock_ttl = lock_ttl
        self.lock_wait = lock_wait
        self._cas = client.register_script(CAS)
        self._unlock = client.register_script(UNLOCK)

    def load(self, key: str) -> esf.Token | None:
        data, version = self.r.hmget(self.prefix + key, "data", "version")
        if data is None:
            return None
        return token_from_json(self.fernet.decrypt(data).decode(), int(version))

    def save(self, key: str, token: esf.Token, *, expected_version: int | None) -> esf.Token:
        data = self.fernet.encrypt(token_to_json(token).encode())
        expected = "" if expected_version is None else str(expected_version)
        version = int(self._cas(keys=[self.prefix + key], args=[expected, data]))
        if version == -1:
            raise esf.TokenConflict(f"token for {key!r} changed (expected version {expected_version})")
        return token.replace(version=version)

    def delete(self, key: str) -> None:
        self.r.delete(self.prefix + key)

    @contextlib.contextmanager
    def lock(self, key: str) -> Iterator[None]:
        name, owner = f"{self.prefix}{key}:lock", secrets.token_hex(16)
        deadline = time.monotonic() + self.lock_wait
        while not self.r.set(name, owner, nx=True, px=int(self.lock_ttl * 1000)):
            if time.monotonic() > deadline:
                raise TimeoutError(f"could not acquire the token lock for {key!r}")
            time.sleep(0.05)
        try:
            yield
        finally:
            self._unlock(keys=[name], args=[owner])
```

For `AsyncSalesforce`, use `redis.asyncio.Redis` with `async def` methods and an
`asynccontextmanager` lock.

## Sharing a store between clients

- Child clients (`with_auth()`, `tagged()`) use the root client's store unless you pass
  `store=` to `with_auth()`.
- An in-memory store instance can be shared between client families **of the same event loop**
  (async) or **the same process** (sync). Its lock is in-process only.
- Don't give an async store to the sync client, or a sync store to the async client.
