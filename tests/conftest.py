"""Shared test helpers.

The library's own suite does not use pytest-asyncio. Async code is driven by :class:`Harness`,
which runs coroutines on a fresh event loop per test, so one test body can drive both the async
and the sync client (``harness.run(x)`` awaits ``x`` if it is awaitable, else returns it).
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest
import respx

import even_simpler_salesforce as esf

INSTANCE = "https://acme.my.salesforce.com"
API = "68.0"
DATA = f"{INSTANCE}/services/data/v{API}"
TOKEN_URL = f"{INSTANCE}/services/oauth2/token"
ORG_ID = "00Dxx0000000001EAA"
USER_ID = "005xx0000000001AAA"
ID_URL = f"https://login.salesforce.com/id/{ORG_ID}/{USER_ID}"

KINDS = ("async", "sync")


# --- markers / options ------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "bench: performance benchmark (skipped unless selected)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    markexpr = config.getoption("markexpr", "") or ""
    enabled = bool(config.getoption("benchmark_enable", False)) or "bench" in markexpr
    if enabled:
        return
    skip = pytest.mark.skip(reason="benchmark: run with --benchmark-enable or -m bench")
    for item in items:
        if "bench" in item.keywords:
            item.add_marker(skip)


# --- tokens -------------------------------------------------------------------------------


def make_token(
    access: str = "AT-initial-0001",
    refresh: str | None = None,
    *,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
    instance_url: str = INSTANCE,
) -> esf.Token:
    return esf.Token(
        access_token=access,
        instance_url=instance_url,
        issued_at=issued_at or datetime.now(timezone.utc),
        refresh_token=refresh,
        id_url=ID_URL,
        org_id=ORG_ID,
        user_id=USER_ID,
        expires_at=expires_at,
    )


def token_body(access: str, refresh: str | None = None, *, issued_at_ms: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "access_token": access,
        "instance_url": INSTANCE,
        "id": ID_URL,
        "token_type": "Bearer",
        "issued_at": str(
            issued_at_ms if issued_at_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
        ),
        "signature": "c2lnbmF0dXJl",
        "scope": "api refresh_token",
    }
    if refresh is not None:
        body["refresh_token"] = refresh
    return body


def cc_auth(**kw: Any) -> esf.ClientCredentials:
    return esf.ClientCredentials(domain=INSTANCE, client_id="cid-123", client_secret="s3cr3t-value", **kw)


def web_auth(**kw: Any) -> esf.WebServerFlow:
    kw.setdefault("token_key", "user:42")
    return esf.WebServerFlow(
        domain=INSTANCE,
        client_id="cid-web",
        client_secret="web-s3cr3t",
        redirect_uri="https://app.example.com/cb",
        **kw,
    )


# --- harness ------------------------------------------------------------------------------


class Recorder:
    """Observer that keeps every event."""

    def __init__(self) -> None:
        self.events: list[esf.Event] = []

    def on_event(self, event: esf.Event) -> None:
        self.events.append(event)

    def named(self, name: str) -> list[esf.Event]:
        return [e for e in self.events if e.name == name]

    @property
    def requests(self) -> list[esf.Event]:
        return self.named("sf.request")


class Harness:
    """Drives either client flavour from a plain (sync) test function."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.loop = asyncio.new_event_loop()
        self._clients: list[Any] = []

    @property
    def is_async(self) -> bool:
        return self.kind == "async"

    @property
    def Client(self) -> type[Any]:  # noqa: N802
        return esf.AsyncSalesforce if self.is_async else esf.Salesforce

    def run(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return self.loop.run_until_complete(value)  # type: ignore[arg-type]
        return value

    def collect(self, it: Any) -> list[Any]:
        if hasattr(it, "__aiter__"):

            async def go() -> list[Any]:
                return [x async for x in it]

            return self.loop.run_until_complete(go())
        return list(it)

    def new_store(self) -> Any:
        return esf.AsyncInMemoryTokenStore() if self.is_async else esf.InMemoryTokenStore()

    def seed(self, store: Any, key: str, token: esf.Token) -> esf.Token:
        return self.run(store.save(key, token, expected_version=None))

    def load(self, store: Any, key: str) -> esf.Token | None:
        return self.run(store.load(key))

    def client(
        self,
        auth: Any = None,
        *,
        store: Any = None,
        token: esf.Token | None | bool = True,
        recorder: Recorder | None = None,
        retry: esf.RetryPolicy | None = None,
        **kw: Any,
    ) -> Any:
        """A client with a fake clock; seeds ``token`` into the store unless ``token=False``."""
        auth = auth if auth is not None else cc_auth()
        if store is None:
            store = self.new_store()
        if token is True:
            token = make_token()
        if token and not isinstance(auth, esf.StaticToken):
            self.seed(store, auth.token_key, token)  # type: ignore[arg-type]
        observers = list(kw.pop("observers", ()))
        if recorder is not None:
            observers.append(recorder)
        sf = self.Client(
            auth,
            store=store,
            observers=observers,
            retry=retry or esf.RetryPolicy(jitter=False),
            **kw,
        )
        self.install_clock(sf)
        self._clients.append(sf)
        return sf

    def install_clock(self, sf: Any) -> list[float]:
        """Replace ``transport.sleep`` with a recorder; returns the list of slept delays."""
        sleeps: list[float] = []
        if self.is_async:

            async def fake_sleep(d: float) -> None:
                sleeps.append(d)

            sf._transport.sleep = fake_sleep
        else:
            sf._transport.sleep = sleeps.append
        sf.sleeps = sleeps
        return sleeps

    def close(self) -> None:
        for sf in self._clients:
            try:
                self.run(sf.aclose() if self.is_async else sf.close())
            except Exception:
                pass
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        self.loop.close()


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    """A fresh event loop per test (no pytest-asyncio)."""
    lp = asyncio.new_event_loop()
    try:
        yield lp
    finally:
        lp.close()


@pytest.fixture
def arun(loop: asyncio.AbstractEventLoop) -> Any:
    """``arun(coro)`` runs ``coro`` on the per-test loop."""
    return loop.run_until_complete


@pytest.fixture(params=KINDS)
def h(request: pytest.FixtureRequest) -> Iterator[Harness]:
    """Harness parametrised over the async and the sync client."""
    harness = Harness(request.param)
    try:
        yield harness
    finally:
        harness.close()


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def rec() -> Recorder:
    return Recorder()
