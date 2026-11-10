"""Login flows on an Experience Cloud site URL (``domain="https://<site>/<prefix>"``).

Salesforce serves the OAuth endpoints (authorize, token, revoke, introspect) under the site URL:
https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_endpoints.htm&type=5
For the JWT bearer flow the ``aud`` claim is the site URL:
https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_jwt_flow.htm&type=5
The token responses below are synthetic.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, INSTANCE, Harness, make_token, token_body

SITE = "https://acme.my.site.com/dms"
SITE_TOKEN_URL = f"{SITE}/services/oauth2/token"


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def site_flow() -> esf.WebServerFlow:
    return esf.WebServerFlow(
        domain=SITE + "/",
        client_id="cid-site",
        client_secret="site-s3cr3t",
        redirect_uri="https://app.example.com/cb",
        token_key="member:1",
    )


def test_jwt_bearer_posts_to_the_site_with_site_audience(h: Harness, mock: respx.MockRouter) -> None:
    jwt = pytest.importorskip("jwt")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    tok = mock.post(SITE_TOKEN_URL).respond(json=token_body("AT-site"))
    api = mock.get(f"{DATA}/limits").respond(json={})
    auth = esf.JwtBearer(domain=SITE, client_id="cid", username="member@acme.example", private_key=pem)
    sf = h.client(auth, token=False)

    h.run(sf.limits())

    assertion = form(tok.calls.last.request)["assertion"]
    claims = jwt.decode(assertion, key.public_key(), algorithms=["RS256"], audience=SITE)
    assert claims["aud"] == SITE
    # API calls go to the instance URL from the token response.
    assert api.calls.last.request.headers["Authorization"] == "Bearer AT-site"


def test_web_server_flow_uses_site_endpoints(h: Harness, mock: respx.MockRouter) -> None:
    tok = mock.post(SITE_TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json=token_body("AT-code", "RT-code")),
            httpx.Response(200, json=token_body("AT-refreshed")),
        ]
    )
    intro = mock.post(f"{SITE}/services/oauth2/introspect").respond(json={"active": True})
    revoke = mock.post(f"{SITE}/services/oauth2/revoke").respond(200)
    flow = site_flow()
    sf = h.client(flow, token=False)

    start = flow.authorization_url(state="s")
    assert start.url.startswith(f"{SITE}/services/oauth2/authorize?")
    saved = h.run(sf.auth.exchange_code(flow, "the-code", code_verifier=start.code_verifier))
    assert saved.instance_url == INSTANCE
    assert form(tok.calls.last.request)["grant_type"] == "authorization_code"

    assert h.run(sf.auth.refresh()).access_token == "AT-refreshed"
    assert form(tok.calls.last.request)["refresh_token"] == "RT-code"

    assert h.run(sf.auth.introspect())["active"] is True
    assert form(intro.calls.last.request)["client_id"] == "cid-site"
    h.run(sf.auth.revoke())
    assert form(revoke.calls.last.request) == {"token": "RT-code"}


def test_site_token_loaded_from_store(h: Harness, mock: respx.MockRouter) -> None:
    tok = mock.post(SITE_TOKEN_URL)
    api = mock.get(f"{DATA}/limits").respond(json={})
    sf = h.client(site_flow(), token=make_token("AT-stored", "RT"))
    h.run(sf.limits())
    assert not tok.called
    assert api.calls.last.request.headers["Authorization"] == "Bearer AT-stored"
