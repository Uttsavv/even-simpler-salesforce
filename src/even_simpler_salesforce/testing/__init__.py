"""Testing support for apps that use even-simpler-salesforce (§14).

- :class:`FakeSalesforce` / :class:`AsyncFakeSalesforce`: in-memory clients with scripted
  queries, error injection and a call log.
- :func:`record` / :func:`replay`: capture real traffic into redacted JSON fixtures and serve it
  back through ``httpx.MockTransport`` (see :mod:`even_simpler_salesforce.testing.recorder`).
- :class:`TokenStoreContract` / :class:`AsyncTokenStoreContract`: a pytest suite for your own
  token store (see :mod:`even_simpler_salesforce.testing.token_store_contract`; needs pytest).
- :class:`ReplayStoreContract` / :class:`AsyncReplayStoreContract`: the same for a Pub/Sub
  replay store (see :mod:`even_simpler_salesforce.testing.replay_store_contract`).
- ``fake.pubsub``: emit platform and change events in a test and consume them with
  ``fake.pubsub.subscribe`` (:class:`FakePubSub`); no gRPC involved.
- A pytest plugin (auto-registered) with the ``esf_fake``, ``esf_async_fake`` and
  ``esf_replay`` fixtures and the ``esf_sandbox`` marker.

Importing this package needs neither pytest nor respx; the contract classes import pytest
on first access.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._fake import (
    AsyncFakeActions,
    AsyncFakeApex,
    AsyncFakeCollections,
    AsyncFakeSalesforce,
    AsyncFakeSObject,
    FakeActions,
    FakeApex,
    FakeCollections,
    FakeSalesforce,
    FakeSObject,
)
from ._fake_org import AsyncFakeLeads, AsyncFakePlatformEvents, FakeLeads, FakePlatformEvents
from ._files import AsyncFakeFiles, AsyncFakeFileStream, FakeFiles, FakeFileStream
from ._graphql import GraphQLScript
from ._ids import key_prefix
from ._pubsub import AsyncFakePubSub, AsyncFakeSubscription, FakePubSub, FakeSubscription
from ._state import FakeCall, FakeRequest, QueryScript, UnscriptedCall
from .recorder import RecordingTransport, Redactor, ReplayMismatch, record, replay

if TYPE_CHECKING:
    from . import replay_store_contract as replay_store_contract
    from . import token_store_contract as token_store_contract
    from .replay_store_contract import AsyncReplayStoreContract, ReplayStoreContract
    from .token_store_contract import AsyncTokenStoreContract, TokenStoreContract

__all__ = [
    "AsyncFakeActions",
    "AsyncFakeApex",
    "AsyncFakeCollections",
    "AsyncFakeLeads",
    "AsyncFakePlatformEvents",
    "AsyncFakePubSub",
    "AsyncFakeSubscription",
    "AsyncFakeFileStream",
    "AsyncFakeFiles",
    "AsyncFakeSObject",
    "AsyncFakeSalesforce",
    "AsyncReplayStoreContract",
    "AsyncTokenStoreContract",
    "FakeActions",
    "FakeApex",
    "FakeCall",
    "FakeCollections",
    "FakeLeads",
    "FakePlatformEvents",
    "FakePubSub",
    "FakeFileStream",
    "FakeFiles",
    "FakeRequest",
    "FakeSObject",
    "FakeSalesforce",
    "FakeSubscription",
    "GraphQLScript",
    "QueryScript",
    "RecordingTransport",
    "ReplayStoreContract",
    "Redactor",
    "ReplayMismatch",
    "TokenStoreContract",
    "UnscriptedCall",
    "key_prefix",
    "record",
    "replay",
    "replay_store_contract",
    "token_store_contract",
]

# The contract suites import pytest, so they load on first access.
_LAZY = {
    "AsyncTokenStoreContract": "token_store_contract",
    "TokenStoreContract": "token_store_contract",
    "token_store_contract": "token_store_contract",
    "AsyncReplayStoreContract": "replay_store_contract",
    "ReplayStoreContract": "replay_store_contract",
    "replay_store_contract": "replay_store_contract",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        import importlib

        mod = importlib.import_module(f"{__name__}.{_LAZY[name]}")
        return mod if name == _LAZY[name] else getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
