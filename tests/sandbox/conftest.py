"""Fixtures for real-org tests. Everything here skips unless ``ESF_SANDBOX_*`` is configured.

- ``settings``: parsed :class:`~tests.sandbox.settings.Settings` (skips when not configured; in
  web-server mode it primes the SQLite token store once per session).
- ``sb``: a :class:`Driver` parametrised over the async and the sync client.
- ``tag``: the run's unique prefix (``esf-sandbox-<hex>``) for every record a test creates;
  ``cleanup`` registers ids, and the session deletes all tagged records at the end.
- ``writes`` / ``heavy_writes``: skip unless writes (resp. ``ESF_SANDBOX_ALLOW_WRITES=1``) are on.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

import even_simpler_salesforce as esf

from .settings import Settings, load_settings

CLEANUP_SOBJECT = "Lead"


class Recorder:
    """Observer keeping every event (sandbox copy; ``tests/conftest`` is for mocked tests)."""

    def __init__(self) -> None:
        self.events: list[esf.Event] = []

    def on_event(self, event: esf.Event) -> None:
        self.events.append(event)

    def requests(self, operation: str | None = None) -> list[esf.Event]:
        return [
            e
            for e in self.events
            if e.name == "sf.request" and (operation is None or e.get("operation") == operation)
        ]


class Driver:
    """Drives the async or the sync client from a plain test function, against the real org."""

    def __init__(self, kind: str, settings: Settings, stores: dict[str, Any]) -> None:
        self.kind = kind
        self.settings = settings
        self._stores = stores
        self.loop = asyncio.new_event_loop()
        self._clients: list[Any] = []
        self.recorder = Recorder()

    @property
    def is_async(self) -> bool:
        return self.kind == "async"

    def run(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return self.loop.run_until_complete(value)  # type: ignore[arg-type]
        return value

    def collect(self, it: Any, limit: int | None = None) -> list[Any]:
        if hasattr(it, "__aiter__"):

            async def go() -> list[Any]:
                out: list[Any] = []
                async for x in it:
                    out.append(x)
                    if limit is not None and len(out) >= limit:
                        break
                return out

            return self.loop.run_until_complete(go())
        out: list[Any] = []
        for x in it:
            out.append(x)
            if limit is not None and len(out) >= limit:
                break
        return out

    def client(self, **kw: Any) -> Any:
        observers = [*kw.pop("observers", ()), self.recorder]
        cls = esf.AsyncSalesforce if self.is_async else esf.Salesforce
        sf = cls(
            self.settings.auth(),
            store=self.settings.store_for(self.kind, self._stores),
            observers=observers,
            **self.settings.client_kwargs(**kw),
        )
        self._clients.append(sf)
        return sf

    def close(self) -> None:
        for sf in self._clients:
            with contextlib.suppress(Exception):
                self.run(sf.aclose() if self.is_async else sf.close())
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        self.loop.close()


class Cleanup:
    """Ids created during the run, deleted at session end (plus anything carrying the tag)."""

    def __init__(self) -> None:
        self.ids: set[str] = set()

    def add(self, *ids: str | None) -> None:
        self.ids.update(i for i in ids if i)

    def discard(self, *ids: str) -> None:
        self.ids.difference_update(ids)


@pytest.fixture(scope="session")
def settings() -> Settings:
    s = load_settings()
    if not s.configured:
        pytest.skip(s.skip_reason)
    if s.web_server:
        try:
            s.prime()
        except esf.ReauthorizationRequired as exc:
            pytest.fail(
                f"sandbox: the stored refresh token was rejected ({exc.error_code}); log in again with "
                "`uv run python scripts/sandbox_login.py` or set a fresh ESF_SANDBOX_REFRESH_TOKEN",
                pytrace=False,
            )
    return s


@pytest.fixture(scope="session")
def _stores() -> dict[str, Any]:
    return {}


@pytest.fixture(scope="session")
def tag() -> str:
    return f"esf-sandbox-{uuid.uuid4().hex[:12]}"


@pytest.fixture(scope="session")
def cleanup(settings: Settings, tag: str) -> Iterator[Cleanup]:
    c = Cleanup()
    yield c
    _final_cleanup(settings, tag, c)


def _final_cleanup(settings: Settings, tag: str, c: Cleanup) -> None:
    if settings.read_only:
        return
    with settings.sync_client() as sf:
        like = "esf-sandbox-%" if settings.sweep else f"{tag}%"
        soql = esf.soql(f"SELECT Id FROM {CLEANUP_SOBJECT} WHERE LastName LIKE {{}}", like)
        tagged = [r["Id"] for r in sf.query_iter(soql)]
        objects = {CLEANUP_SOBJECT}
        if settings.external_id:
            obj = settings.external_id[0]
            if obj != CLEANUP_SOBJECT:
                objects.add(obj)
                soql = esf.soql(f"SELECT Id FROM {esf.soql_identifier(obj)} WHERE LastName LIKE {{}}", like)
                with contextlib.suppress(esf.SalesforceError):
                    tagged += [r["Id"] for r in sf.query_iter(soql)]
        ids = sorted(set(tagged) | c.ids)
        if ids:
            result = sf.collections.delete(ids)
            leftovers = [
                r for r in result.failed if not any(e.error_code == "ENTITY_IS_DELETED" for e in r.errors)
            ]
            if leftovers:
                print(f"\nsandbox cleanup: {len(leftovers)} record(s) could not be deleted: {leftovers[:5]}")


@pytest.fixture(params=["async", "sync"])
def sb(request: pytest.FixtureRequest, settings: Settings, _stores: dict[str, Any]) -> Iterator[Driver]:
    d = Driver(request.param, settings, _stores)
    try:
        yield d
    finally:
        d.close()


@pytest.fixture
def writes(settings: Settings) -> None:
    if settings.read_only:
        pytest.skip("sandbox: ESF_SANDBOX_READ_ONLY=1 (no writes)")


@pytest.fixture
def heavy_writes(settings: Settings, writes: None) -> None:
    if not settings.allow_writes:
        pytest.skip("sandbox: heavy writes (>200 rows) need ESF_SANDBOX_ALLOW_WRITES=1")


@pytest.fixture
def external_id(settings: Settings, writes: None) -> tuple[str, str]:
    if settings.external_id is None:
        pytest.skip("sandbox: set ESF_SANDBOX_EXTERNAL_ID_FIELD=<Object>.<Field> (a writable external id)")
    obj, _ = settings.external_id
    if obj not in REQUIRED_FIELDS:
        pytest.skip(f"sandbox: external-id tests support {sorted(REQUIRED_FIELDS)} objects, not {obj}")
    return settings.external_id


# Minimal required fields per sObject for the records these tests create (LastName holds the tag).
REQUIRED_FIELDS: dict[str, dict[str, str]] = {
    "Lead": {"Company": "esf-sandbox"},
    "Contact": {},
}


def new_row(sobject: str, tag: str, suffix: str = "", **fields: Any) -> dict[str, Any]:
    return {"LastName": f"{tag}{suffix}", **REQUIRED_FIELDS[sobject], **fields}
