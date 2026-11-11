# Token stores

A token store keeps tokens that must outlive the process or be shared between processes. The
client keeps the current access token in memory and saves every new token to its token store.

```python
import even_simpler_salesforce as esf

store = esf.AsyncInMemoryTokenStore()
auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
async with esf.AsyncSalesforce(auth, store=store) as sf:
    await sf.limits()
    print(await store.load(auth.token_key))
```

Each client takes the store type that matches it:

| Client | Store protocol | Built-in store |
|---|---|---|
| `esf.AsyncSalesforce` | `esf.AsyncTokenStore`, with async methods | `esf.AsyncInMemoryTokenStore` (default) |
| `esf.Salesforce` | `esf.TokenStore`, with the same methods as sync | `esf.InMemoryTokenStore` (default) |

Don't give an async store to the sync client, or a sync store to the async client.

## Choose a store

The in-memory stores lose their contents when the process restarts. For `ClientCredentials`
and `JwtBearer` that is fine, because the process requests a new token.

For `WebServerFlow` in production, use a durable token store. It keeps each user's refresh
token across restarts and shares it between processes. You can also share
`ClientCredentials` and `JwtBearer` tokens through a durable store to save token requests.

## Write your own store

A token store is any class with these methods:

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

The async protocol has the same methods as `async def`. Its `lock()` returns an async context
manager.

| Method | Called when | Must |
|---|---|---|
| `load(key)` | the process has no token in memory, before every refresh, and after an ambiguous refresh | return the stored token with its `version`, or `None` |
| `save(key, token, expected_version=...)` | after the client gets a token: initial, refreshed or exchanged | if `expected_version` is not `None`, compare-and-swap atomically; return the token with a new, strictly greater `version`, also after a `delete` |
| `delete(key)` | `sf.auth.revoke()` | remove both access and refresh token; deleting a missing key does nothing |
| `lock(key)` | around each refresh; optional | serialise refreshes for `key` across processes; the default does nothing |

## Rules for durable stores

1. Keep one record per Salesforce identity, not per app user. If two of your users share one
   Salesforce connection, they must share one record and one `token_key`. If several records
   hold copies of one refresh token, they break as soon as rotation invalidates one of them.
   If a record changes to another org or user, the client raises `TokenIdentityChanged`.
2. Make `save` with `expected_version` atomic. Use a compare-and-swap, a row lock or a
   transaction. This makes refresh safe when several processes share a store. If `save` raises
   `TokenConflict`, the client re-reads the store and uses the token the other process saved.
3. Encrypt refresh tokens at rest. A refresh token is a long-lived credential.
4. Make `delete` remove both tokens.
5. Never reuse a version for a key, also after `delete`. Otherwise a stale compare-and-swap
   from before the delete can overwrite the new token. The examples below keep the version in a
   tombstone row when they delete the tokens.

## Serialise a token

`Token` is a frozen dataclass, so your store can serialise it in any format. The examples below
use this JSON helper:

```python
import json
from datetime import datetime

from even_simpler_salesforce import Token


def token_to_json(token: Token) -> str:
    return json.dumps(
        {
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
        }
    )


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

## Example: SQLite

This durable store uses one SQLite file and only the standard library:

```python
import contextlib
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import even_simpler_salesforce as esf


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
                # data is NULL after delete(): the row keeps the version.
                "CREATE TABLE IF NOT EXISTS esf_tokens ("
                " key TEXT PRIMARY KEY, data TEXT, version INTEGER NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30, isolation_level=None)

    def load(self, key: str) -> esf.Token | None:
        with contextlib.closing(self._connect()) as db:
            row = db.execute(
                "SELECT data, version FROM esf_tokens WHERE key = ? AND data IS NOT NULL", (key,)
            ).fetchone()
        return None if row is None else token_from_json(row[0], row[1])

    def save(self, key: str, token: esf.Token, *, expected_version: int | None) -> esf.Token:
        with contextlib.closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")  # take the write lock before reading the version
            try:
                row = db.execute(
                    "SELECT version, data IS NOT NULL FROM esf_tokens WHERE key = ?", (key,)
                ).fetchone()
                current = row[0] if row and row[1] else None
                if expected_version is not None and current != expected_version:
                    raise esf.TokenConflict(f"expected version {expected_version}, found {current}")
                version = (row[0] if row else 0) + 1
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
            db.execute("UPDATE esf_tokens SET data = NULL WHERE key = ?", (key,))

    @contextlib.contextmanager
    def lock(self, key: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            yield
```

Use it like the built-in stores: `esf.Salesforce(auth, store=SqliteTokenStore("tokens.db"))`.

## Test your store { #checking-your-store-tokenstorecontract }

`even_simpler_salesforce.testing` ships the token store rules as a pytest suite. Subclass
`TokenStoreContract` for a sync store, or `AsyncTokenStoreContract` for an async one, in a
`Test*` class. Override the `make_store` fixture to return a zero-argument factory for fresh, empty stores.

```python
import pytest

from even_simpler_salesforce.testing import TokenStoreContract


class TestSqliteTokenStore(TokenStoreContract):
    @pytest.fixture
    def make_store(self, tmp_path):
        counter = iter(range(1_000_000))
        return lambda: SqliteTokenStore(tmp_path / f"tokens-{next(counter)}.db")
```

The docs tests run this example. The suite checks that:

- loading a missing key returns `None`;
- a save and load round trip keeps every field;
- versions strictly increase;
- a stale `expected_version` raises `esf.TokenConflict` and leaves the stored token unchanged;
- `delete` removes both tokens and you can call it twice;
- a save after `delete` gets a version that no earlier save had;
- keys are isolated;
- `lock(key)`, if present, works as a context manager;
- N concurrent compare-and-swap saves against the same version have exactly one winner.

The sync suite runs the concurrent saves in threads, the async suite in tasks. To change N, set
`concurrency` on the class. The default is 16.

The async suite runs each test on its own event loop with `asyncio.run`, so it doesn't need
pytest-asyncio. `make_store` can return a sync or an async factory. If the store has `aclose()`
or `close()`, the suite calls it after each test.

## Example: SQLAlchemy with PostgreSQL

This example is not part of the package. The docs tests don't run it, because it needs a
PostgreSQL server. Run `TokenStoreContract` against your version before you rely on it.

The store uses:

- a version column for compare-and-swap;
- `SELECT … FOR UPDATE` to make the check and the write atomic;
- a PostgreSQL advisory lock for `lock()`, so refreshes are serialised across processes without
  blocking the row that `save` updates;
- [Fernet](https://cryptography.io/en/latest/fernet/) to encrypt tokens at rest.

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
    Column("data", Text, nullable=True),  # Fernet-encrypted token_to_json(...); NULL after delete
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
        if row is None or row.data is None:
            return None
        return token_from_json(self.fernet.decrypt(row.data.encode()).decode(), row.version)

    def save(self, key: str, token: esf.Token, *, expected_version: int | None) -> esf.Token:
        data = self.fernet.encrypt(token_to_json(token).encode()).decode()
        try:
            with self.engine.begin() as conn:  # one transaction; row lock until commit
                row = conn.execute(
                    select(esf_tokens.c.version, esf_tokens.c.data)
                    .where(esf_tokens.c.key == key)
                    .with_for_update()
                ).first()
                current = row.version if row is not None and row.data is not None else None
                if expected_version is not None and current != expected_version:
                    raise esf.TokenConflict(f"expected version {expected_version}, found {current}")
                version = (row.version if row is not None else 0) + 1
                if row is None:
                    conn.execute(esf_tokens.insert().values(key=key, data=data, version=version))
                else:
                    conn.execute(
                        esf_tokens.update().where(esf_tokens.c.key == key).values(data=data, version=version)
                    )
        except IntegrityError:  # another process inserted the first row concurrently
            raise esf.TokenConflict("token record was created concurrently") from None
        return token.replace(version=version)

    def delete(self, key: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(esf_tokens.update().where(esf_tokens.c.key == key).values(data=None))

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

For `AsyncSalesforce`, write the same store with `async def` methods on an SQLAlchemy
`AsyncEngine`. Use `async with engine.begin() as conn: await conn.execute(...)` and
`contextlib.asynccontextmanager` for `lock()`. Then run `AsyncTokenStoreContract` against it.

## Example: Redis

This example is not part of the package. The docs tests don't run it, because it needs a Redis
server. Run `TokenStoreContract` against your version before you rely on it.

The store uses a Lua script for an atomic compare-and-swap on a hash with `data` and `version`.
Its lock is a `SET NX PX` key with a random owner value. A second script releases the lock only
for its owner. The lock has a TTL, so a crashed process can't hold it for ever. Pick a TTL
longer than a token request takes.

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
local stored = redis.call('HGET', KEYS[1], 'version')
local current = stored
if redis.call('HEXISTS', KEYS[1], 'data') == 0 then
  current = false
end
if ARGV[1] ~= '' and current ~= ARGV[1] then
  return -1
end
local version = (tonumber(stored) or 0) + 1
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
    def __init__(
        self,
        client: redis.Redis,
        fernet: Fernet,
        *,
        prefix: str = "esf:token:",
        lock_ttl: float = 30.0,
        lock_wait: float = 35.0,
    ) -> None:
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
        self.r.hdel(self.prefix + key, "data")  # keep the version

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

## Share a store between clients

- Child clients from `with_auth()` and `tagged()` use the root client's store. To give a child
  its own store, pass `store=` to `with_auth()`.
- You can share an in-memory store between client families on the same event loop for async,
  or in the same process for sync. Its lock works only inside one process.

## How it works

The in-memory stores are a dict, a version counter and a lock for each key. They keep one
record for each `token_key` until `delete`, and never drop a record by themselves. If you create
a client for each user with `with_auth()`, the root client's store grows with the number of
users. Use a durable store for that case. The store drops a lock when no caller holds it or
waits for it.

Inside one process, the client always runs one refresh at a time for each key, with or without
a store lock. You need a store lock only if several processes share a store. Without it,
compare-and-swap still keeps the stored data consistent, but two processes can both send a
refresh request.
