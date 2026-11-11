"""Fixtures for the Pub/Sub contract tests: a fake gRPC server and helpers for both clients."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

pytest.importorskip("grpc")
pytest.importorskip("fastavro")

from ...conftest import Harness, Recorder  # noqa: E402
from ._server import FakePubSubServer  # noqa: E402


@pytest.fixture
def server() -> Iterator[FakePubSubServer]:
    srv = FakePubSubServer()
    try:
        yield srv
    finally:
        srv.stop()


class PubSubHarness:
    """Opens a subscription on either client and consumes it from a plain test function."""

    def __init__(self, h: Harness, server: FakePubSubServer, rec: Recorder) -> None:
        self.h = h
        self.server = server
        self.rec = rec
        self._sf: Any = None

    @property
    def sf(self) -> Any:
        if self._sf is None:
            self._sf = self.client()
        return self._sf

    def client(self, **kw: Any) -> Any:
        kw.setdefault("recorder", self.rec)
        return self.h.client(pubsub_endpoint=self.server.endpoint, **kw)

    def store(self) -> Any:
        import even_simpler_salesforce as esf

        return esf.AsyncInMemoryReplayStore() if self.h.is_async else esf.InMemoryReplayStore()

    def consume(
        self,
        topic: str = "/event/Order_Event__e",
        n: int = 1,
        *,
        sf: Any = None,
        each: Callable[[Any], None] | None = None,
        **kw: Any,
    ) -> list[Any]:
        """Subscribe, take ``n`` events (calling ``each`` on them), then leave the block."""
        sub = (sf or self.sf).pubsub.subscribe(topic, **kw)
        out: list[Any] = []
        if self.h.is_async:

            async def go() -> None:
                async with sub as events:
                    if n == 0:
                        return
                    async for ev in events:
                        out.append(ev)
                        if each is not None:
                            each(ev)
                        if len(out) >= n:
                            break

            self.h.run(go())
        else:
            with sub as events:
                if n:
                    for ev in events:
                        out.append(ev)
                        if each is not None:
                            each(ev)
                        if len(out) >= n:
                            break
        return out

    def load(self, store: Any, key: str) -> Any:
        return self.h.run(store.load(key))

    def save(self, store: Any, key: str, value: bytes) -> None:
        self.h.run(store.save(key, value))


@pytest.fixture
def ps(h: Harness, server: FakePubSubServer, rec: Recorder) -> Iterator[PubSubHarness]:
    yield PubSubHarness(h, server, rec)
    # Close the clients (and their gRPC channels) before the server stops.
    for sf in h._clients:
        try:
            h.run(sf.aclose() if h.is_async else sf.close())
        except Exception:
            pass
    h._clients.clear()
