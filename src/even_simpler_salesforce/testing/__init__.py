"""Testing support for apps that use even-simpler-salesforce (§14).

- :class:`FakeSalesforce` / :class:`AsyncFakeSalesforce`: in-memory clients with scripted
  queries, error injection and a call log.
- :func:`record` / :func:`replay`: capture real traffic into redacted JSON fixtures and serve it
  back through ``httpx.MockTransport`` (see :mod:`even_simpler_salesforce.testing.recorder`).
- :class:`TokenStoreContract` / :class:`AsyncTokenStoreContract`: a pytest suite for your own
  token store (see :mod:`even_simpler_salesforce.testing.token_store_contract`; needs pytest).
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
from ._ids import key_prefix
from ._state import FakeCall, FakeRequest, QueryScript, UnscriptedCall
from .recorder import RecordingTransport, Redactor, ReplayMismatch, record, replay

if TYPE_CHECKING:
    from . import token_store_contract as token_store_contract
    from .token_store_contract import AsyncTokenStoreContract, TokenStoreContract

__all__ = [
    "AsyncFakeActions",
    "AsyncFakeApex",
    "AsyncFakeCollections",
    "AsyncFakeSObject",
    "AsyncFakeSalesforce",
    "AsyncTokenStoreContract",
    "FakeActions",
    "FakeApex",
    "FakeCall",
    "FakeCollections",
    "FakeRequest",
    "FakeSObject",
    "FakeSalesforce",
    "QueryScript",
    "RecordingTransport",
    "Redactor",
    "ReplayMismatch",
    "TokenStoreContract",
    "UnscriptedCall",
    "key_prefix",
    "record",
    "replay",
    "token_store_contract",
]

_LAZY = {"AsyncTokenStoreContract", "TokenStoreContract", "token_store_contract"}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        import importlib

        mod = importlib.import_module(f"{__name__}.token_store_contract")
        return mod if name == "token_store_contract" else getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
