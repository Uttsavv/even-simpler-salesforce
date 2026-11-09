"""A fake Salesforce token endpoint with refresh-token rotation, shared by several processes.

``synthetic``: behaviour follows the official *OAuth 2.0 Refresh Token Flow* help page
(https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_refresh_token_flow.htm),
not recorded traffic:

- every successful refresh returns a new access token **and a new refresh token**; the old
  refresh token stops working;
- presenting a rotated-out refresh token **revokes the whole chain** (the current refresh token
  and every access token issued from it) and returns ``invalid_grant``;
- a refresh that overlaps another one with the same refresh token gets HTTP 400
  "Token request is already being processed" (counted as an *overlap*).

State lives in a SQLite file so all worker processes see one org. :func:`install` (entered by
each worker, see ``tests/sandbox/rotation.py``) starts respx in that process and routes the
token endpoint and ``GET /services/data/v68.0/limits`` to this fake.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import respx

INSTANCE = "https://acme.my.salesforce.com"
TOKEN_URL = f"{INSTANCE}/services/oauth2/token"
LIMITS_URL = f"{INSTANCE}/services/data/v68.0/limits"
ID_URL = "https://login.salesforce.com/id/00Dxx0000000001EAA/005xx0000000001AAA"
CLIENT_ID = "cid-rotation"
CLIENT_SECRET = "s3cr3t-rotation"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS refresh_tokens (rt TEXT PRIMARY KEY, state TEXT NOT NULL);  -- current|used|revoked
CREATE TABLE IF NOT EXISTS access_tokens (at TEXT PRIMARY KEY, valid INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS inflight (rt TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, n INTEGER NOT NULL);
"""

COUNTERS = ("refreshes", "reuse", "overlaps", "unknown", "api_ok", "api_401")


class FakeOrg:
    """Shared fake org state (one SQLite file)."""

    def __init__(self, path: str | Path, *, delay: float = 0.002) -> None:
        self.path = str(path)
        self.delay = delay  # time a refresh is "in flight" (widens race windows)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=60, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA synchronous=OFF")  # fake org: speed over durability
        return conn

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def create(self, refresh_token: str, access_token: str) -> None:
        conn = self._conn()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        finally:
            conn.close()
        with self._tx() as c:
            c.execute("INSERT INTO refresh_tokens VALUES (?, 'current')", (refresh_token,))
            c.execute("INSERT INTO access_tokens VALUES (?, 1)", (access_token,))
            c.executemany("INSERT INTO counters VALUES (?, 0)", [(n,) for n in COUNTERS])

    @staticmethod
    def _bump(c: sqlite3.Connection, name: str) -> None:
        c.execute("UPDATE counters SET n = n + 1 WHERE name = ?", (name,))

    def counters(self) -> dict[str, int]:
        conn = self._conn()
        try:
            return {str(k): int(v) for k, v in conn.execute("SELECT name, n FROM counters")}
        finally:
            conn.close()

    def current_refresh_token(self) -> str | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT rt FROM refresh_tokens WHERE state = 'current'").fetchone()
            return str(row[0]) if row else None
        finally:
            conn.close()

    # --- endpoints ---------------------------------------------------------------------

    def token(self, request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode()))
        if form.get("grant_type") != "refresh_token" or form.get("client_id") != CLIENT_ID:
            return httpx.Response(400, json={"error": "invalid_client_id", "error_description": "bad client"})
        rt = form.get("refresh_token", "")
        with self._tx() as c:
            if c.execute("SELECT 1 FROM inflight WHERE rt = ?", (rt,)).fetchone():
                self._bump(c, "overlaps")
                return httpx.Response(
                    400,
                    json={
                        "error": "invalid_request",
                        "error_description": "Token request is already being processed",
                    },
                )
            row = c.execute("SELECT state FROM refresh_tokens WHERE rt = ?", (rt,)).fetchone()
            if row is None or row[0] == "revoked":
                self._bump(c, "unknown")
                return self._invalid_grant()
            if row[0] == "used":  # reuse of a rotated-out token: revoke the chain
                self._bump(c, "reuse")
                c.execute("UPDATE refresh_tokens SET state = 'revoked'")
                c.execute("UPDATE access_tokens SET valid = 0")
                return self._invalid_grant()
            c.execute("INSERT INTO inflight VALUES (?)", (rt,))
        time.sleep(self.delay)  # the request is being processed
        new_rt, new_at = f"RT-{uuid.uuid4().hex}", f"AT-{uuid.uuid4().hex}"
        with self._tx() as c:
            c.execute("DELETE FROM inflight WHERE rt = ?", (rt,))
            state = c.execute("SELECT state FROM refresh_tokens WHERE rt = ?", (rt,)).fetchone()
            if state is None or state[0] != "current":  # revoked meanwhile
                return self._invalid_grant()
            c.execute("UPDATE refresh_tokens SET state = 'used' WHERE rt = ?", (rt,))
            c.execute("INSERT INTO refresh_tokens VALUES (?, 'current')", (new_rt,))
            c.execute("INSERT INTO access_tokens VALUES (?, 1)", (new_at,))
            self._bump(c, "refreshes")
        body: dict[str, Any] = {
            "access_token": new_at,
            "refresh_token": new_rt,
            "instance_url": INSTANCE,
            "id": ID_URL,
            "token_type": "Bearer",
            "issued_at": str(int(time.time() * 1000)),
            "signature": "c2lnbmF0dXJl",
            "scope": "api refresh_token",
        }
        return httpx.Response(200, json=body)

    def limits(self, request: httpx.Request) -> httpx.Response:
        at = request.headers.get("Authorization", "").removeprefix("Bearer ")
        with self._tx() as c:
            row = c.execute("SELECT valid FROM access_tokens WHERE at = ?", (at,)).fetchone()
            ok = bool(row and row[0])
            self._bump(c, "api_ok" if ok else "api_401")
        if not ok:
            return httpx.Response(
                401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "Session expired"}]
            )
        return httpx.Response(
            200,
            json={"DailyApiRequests": {"Max": 15000, "Remaining": 14000}},
            headers={"Sforce-Limit-Info": "api-usage=1000/15000"},
        )

    @staticmethod
    def _invalid_grant() -> httpx.Response:
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "expired access/refresh token"}
        )


@contextlib.contextmanager
def install(arg: str | None) -> Iterator[None]:
    """Route this process's httpx traffic to the fake org. ``arg`` = JSON ``{"path", "delay"}``."""
    opts: dict[str, Any] = json.loads(arg or "{}")
    org = FakeOrg(opts["path"], delay=float(opts.get("delay", 0.002)))
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(side_effect=org.token)
        router.get(LIMITS_URL).mock(side_effect=org.limits)
        yield
