"""§19 acceptance test on a real org: refresh-token rotation from 4 processes, one CAS store.

"With rotation enabled in a sandbox, no ``invalid_grant`` across 1,000 refresh cycles from 4
processes sharing a CAS store."

Needs web-server mode (``ESF_SANDBOX_DOMAIN``, ``ESF_SANDBOX_CLIENT_ID``, optional
``ESF_SANDBOX_CLIENT_SECRET``, and ``ESF_SANDBOX_REFRESH_TOKEN`` or a token saved by
``scripts/sandbox_login.py``) for an External Client App / connected app with **refresh token
rotation on**. ``ESF_SANDBOX_ROTATION_CYCLES`` (default 1000) is the total number of rotations
of the shared chain; the 4 processes (2 async, 2 sync clients) race for them.

Facts relied on (official *OAuth 2.0 Refresh Token Flow* help page,
https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_refresh_token_flow.htm):
with rotation on, each refresh returns a new refresh token and reusing a rotated-out one
invalidates the chain. So a single lost update would surface here as ``invalid_grant``
(``ReauthorizationRequired``) and leave the stored token unusable.

The mocked twin that runs in normal CI is ``tests/contract/test_rotation_multiprocess.py``.
"""

from __future__ import annotations

import pytest

from .rotation import RotationConfig, run_rotation, summarise
from .settings import Settings

pytestmark = pytest.mark.sandbox


def test_rotation_four_processes_no_invalid_grant(settings: Settings) -> None:
    if not settings.web_server:
        pytest.skip(
            "sandbox: the rotation test needs web-server mode (ESF_SANDBOX_DOMAIN + ESF_SANDBOX_CLIENT_ID "
            "+ ESF_SANDBOX_REFRESH_TOKEN or scripts/sandbox_login.py)"
        )
    assert settings.domain
    assert settings.client_id
    total = settings.rotation_cycles
    cfg = RotationConfig(
        db_path=str(settings.token_db),
        token_key=settings.token_key,
        domain=settings.domain,
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        redirect_uri=settings.redirect_uri,
        rotations=total,
        verify_every=settings.rotation_verify_every,
        pause=settings.rotation_pause,
        api_version=settings.api_version,
    )
    reports = run_rotation(cfg, ("async", "sync", "async", "sync"))
    detail = summarise(reports)
    print(f"\nrotation: {total} rotations requested\n{detail}")

    assert sum(r.reauth_required for r in reports) == 0, detail
    assert all(not r.errors for r in reports), detail
    posts = sum(r.posts for r in reports)
    assert posts >= total, detail
    assert sum(r.rotated for r in reports) == posts, f"rotation looks OFF for this app\n{detail}"

    # The final stored refresh token still works: one more refresh + a call with the new token.
    store = settings.sync_store()
    final = store.load(settings.token_key)
    assert final is not None
    assert final.refresh_token
    with settings.sync_client() as sf:
        new = sf._runtime.refresh(final)  # pyright: ignore[reportPrivateUsage]
        assert new.refresh_token != final.refresh_token
        assert sf.limits()["DailyApiRequests"].max > 0
