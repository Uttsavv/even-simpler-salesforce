"""The package's own ``esf_sandbox`` marker and ``esf_sandbox_auth`` fixture against a real org.

These follow the plugin's rules only (``ESF_SANDBOX_ACCESS_TOKEN`` + ``INSTANCE_URL``, client
credentials or JWT bearer). Web-server mode is not something the plugin knows about: its
``DOMAIN`` + ``CLIENT_ID`` + ``CLIENT_SECRET`` would be read as a client-credentials login, which
the web-server app usually doesn't allow (``invalid_grant: no client credentials user enabled``).
So these tests skip in web-server mode.
"""

from __future__ import annotations

import asyncio

import pytest

import even_simpler_salesforce as esf

from .settings import load_settings

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.esf_sandbox,
    pytest.mark.skipif(
        load_settings().web_server,
        reason="sandbox: web-server mode; the plugin would read the app secret as client credentials",
    ),
]


def test_plugin_auth_sync(esf_sandbox_auth: esf.Auth) -> None:
    with esf.Salesforce(esf_sandbox_auth, client_name="esf-sandbox") as sf:
        assert sf.limits()["DailyApiRequests"].max > 0


def test_plugin_auth_async(esf_sandbox_auth: esf.Auth) -> None:
    async def main() -> None:
        async with esf.AsyncSalesforce(esf_sandbox_auth, client_name="esf-sandbox") as sf:
            assert (await sf.limits())["DailyApiRequests"].max > 0

    asyncio.run(main())
