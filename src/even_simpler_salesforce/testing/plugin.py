"""pytest plugin for apps using even-simpler-salesforce (auto-registered via ``pytest11``).

Fixtures:

- ``esf_fake``: a fresh :class:`~even_simpler_salesforce.testing.FakeSalesforce`.
- ``esf_async_fake``: a fresh :class:`~even_simpler_salesforce.testing.AsyncFakeSalesforce`.
- ``esf_replay``: factory ``esf_replay(path, **kwargs) -> httpx.MockTransport``; relative paths
  resolve against the requesting test file's directory.
- ``esf_sandbox_auth``: an ``Auth`` built from ``ESF_SANDBOX_*`` variables (skips if unset).

Marker:

- ``@pytest.mark.esf_sandbox``: real-org test, skipped unless ``ESF_SANDBOX_ACCESS_TOKEN`` +
  ``ESF_SANDBOX_INSTANCE_URL``, or ``ESF_SANDBOX_DOMAIN`` + ``ESF_SANDBOX_CLIENT_ID`` +
  ``ESF_SANDBOX_CLIENT_SECRET`` (or ``ESF_SANDBOX_USERNAME`` + ``ESF_SANDBOX_PRIVATE_KEY_FILE``)
  are set.

Importing this module needs only pytest (respx is not required).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    import httpx

    from ..auth.flows import Auth
    from ._fake import AsyncFakeSalesforce, FakeSalesforce

__all__ = [
    "SANDBOX_PREFIX",
    "esf_async_fake",
    "esf_fake",
    "esf_replay",
    "esf_sandbox_auth",
    "pytest_configure",
    "pytest_runtest_setup",
]

SANDBOX_PREFIX = "ESF_SANDBOX_"

pytest.register_assert_rewrite("even_simpler_salesforce.testing.token_store_contract")


def _sandbox_configured() -> bool:
    from ..cli._env import env_auth_configured

    return env_auth_configured(os.environ, SANDBOX_PREFIX)


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``esf_sandbox`` marker."""
    config.addinivalue_line(
        "markers",
        "esf_sandbox: test needs a real Salesforce org (skipped unless ESF_SANDBOX_* env vars are set)",
    )


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip ``esf_sandbox`` tests when no sandbox login is configured."""
    if item.get_closest_marker("esf_sandbox") is not None and not _sandbox_configured():
        pytest.skip(f"esf_sandbox: set {SANDBOX_PREFIX}* environment variables to run")


@pytest.fixture
def esf_fake() -> Iterator[FakeSalesforce]:
    """A fresh in-memory sync fake client."""
    from ._fake import FakeSalesforce

    fake = FakeSalesforce()
    yield fake
    fake.close()


@pytest.fixture
def esf_async_fake() -> Iterator[AsyncFakeSalesforce]:
    """A fresh in-memory async fake client."""
    from ._fake import AsyncFakeSalesforce

    fake = AsyncFakeSalesforce()
    yield fake
    fake.sync.close()


@pytest.fixture
def esf_replay(request: pytest.FixtureRequest) -> Callable[..., httpx.MockTransport]:
    """Factory: ``esf_replay("fixtures/x.json", match_query=True, repeat=False)``."""
    from .recorder import replay

    base = Path(str(request.path)).parent

    def factory(path: str | Path, **kwargs: Any) -> httpx.MockTransport:
        p = Path(path)
        return replay(p if p.is_absolute() else base / p, **kwargs)

    return factory


@pytest.fixture
def esf_sandbox_auth() -> Auth:
    """``Auth`` from ``ESF_SANDBOX_*`` variables; skips the test when they are not set."""
    from ..cli._env import EnvConfigError, auth_from_env

    try:
        return auth_from_env(os.environ, SANDBOX_PREFIX)
    except EnvConfigError as exc:
        pytest.skip(f"esf_sandbox: {exc}")
