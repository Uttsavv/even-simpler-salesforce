"""The package's own ``esf_sandbox`` marker and ``esf_sandbox_auth`` fixture against a real org.

These follow the plugin's rules only (``ESF_SANDBOX_ACCESS_TOKEN`` + ``INSTANCE_URL``, client
credentials or JWT bearer); web-server mode is not something the plugin knows about, so with
only a refresh token configured these tests are skipped by the marker.
"""

from __future__ import annotations

import asyncio

import pytest

import even_simpler_salesforce as esf

pytestmark = [pytest.mark.sandbox, pytest.mark.esf_sandbox]


def test_plugin_auth_sync(esf_sandbox_auth: esf.Auth) -> None:
    with esf.Salesforce(esf_sandbox_auth, client_name="esf-sandbox") as sf:
        assert sf.limits()["DailyApiRequests"].max > 0


def test_plugin_auth_async(esf_sandbox_auth: esf.Auth) -> None:
    async def main() -> None:
        async with esf.AsyncSalesforce(esf_sandbox_auth, client_name="esf-sandbox") as sf:
            assert (await sf.limits())["DailyApiRequests"].max > 0

    asyncio.run(main())
