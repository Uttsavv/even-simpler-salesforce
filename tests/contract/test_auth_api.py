"""``sf.auth``: exchange_code, revoke, userinfo, introspect, token (§6.1)."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import INSTANCE, TOKEN_URL, Harness, make_token, token_body, web_auth
from ..fixtures import load_fixture

REVOKE_URL = f"{INSTANCE}/services/oauth2/revoke"
USERINFO_URL = f"{INSTANCE}/services/oauth2/userinfo"
INTROSPECT_URL = f"{INSTANCE}/services/oauth2/introspect"


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def test_exchange_code_replaces_a_stored_token_with_an_unsafe_instance_url(
    h: Harness, mock: respx.MockRouter
) -> None:
    """Logging in again is the way out of a bad store entry, so it must not be refused."""
    mock.post(TOKEN_URL).respond(json=token_body("AT-new", "RT-new"))
    store = h.new_store()
    h.seed(store, "user:7", make_token(instance_url="http://attacker.example"))
    base = h.client(store=store)
    flow = web_auth(token_key="user:7")
    start = flow.authorization_url(state="s")
    saved = h.run(base.auth.exchange_code(flow, "the-code", code_verifier=start.code_verifier))
    assert saved.access_token == "AT-new"
    stored = h.load(store, "user:7")
    assert stored is not None
    assert stored.instance_url == INSTANCE


def test_exchange_code_saves_to_store(h: Harness, mock: respx.MockRouter) -> None:
    tok = mock.post(TOKEN_URL).respond(json=token_body("AT-code", "RT-code"))
    store = h.new_store()
    base = h.client(store=store)
    flow = web_auth(token_key="user:7")
    start = flow.authorization_url(state="s")
    saved = h.run(base.auth.exchange_code(flow, "the-code", code_verifier=start.code_verifier))
    assert (saved.access_token, saved.refresh_token, saved.version) == ("AT-code", "RT-code", 1)
    f = form(tok.calls.last.request)
    assert f["grant_type"] == "authorization_code"
    assert f["code"] == "the-code"
    assert f["code_verifier"] == start.code_verifier
    assert f["redirect_uri"] == "https://app.example.com/cb"
    assert h.load(store, "user:7").refresh_token == "RT-code"
    # A child using the flow picks the token up without another POST.
    user_sf = base.with_auth(flow)
    assert h.run(user_sf.auth.token()).access_token == "AT-code"
    assert tok.call_count == 1


def test_exchange_code_replaces_existing_record(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(TOKEN_URL).respond(json=token_body("AT-new", "RT-new"))
    store = h.new_store()
    h.seed(store, "user:7", make_token("AT-old", "RT-old"))
    sf = h.client(store=store)
    saved = h.run(sf.auth.exchange_code(web_auth(token_key="user:7"), "c"))
    assert saved.version == 2
    assert saved.access_token == "AT-new"


def test_exchange_code_errors(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(TOKEN_URL)
    route.side_effect = [
        httpx.Response(400, json={"error": "invalid_grant", "error_description": "authentication failure"}),
        httpx.Response(500),
        httpx.Response(200, text="<html/>"),
    ]
    sf = h.client()
    flow = web_auth()
    with pytest.raises(esf.OAuthError) as ei:
        h.run(sf.auth.exchange_code(flow, "bad"))
    assert ei.value.error == "invalid_grant"
    assert ei.value.operation == "oauth.exchange_code"
    with pytest.raises(esf.OAuthError) as ei2:
        h.run(sf.auth.exchange_code(flow, "bad"))
    assert ei2.value.message == "HTTP 500"
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.auth.exchange_code(flow, "bad"))


def test_exchange_code_with_introspection(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(TOKEN_URL).respond(json=token_body("AT-code", "RT-code"))
    intro = mock.post(INTROSPECT_URL).respond(json=load_fixture("oauth/introspect_active.json"))
    store = h.new_store()
    sf = h.client(store=store)
    flow = web_auth(token_key="user:9", expiry="introspect")
    saved = h.run(sf.auth.exchange_code(flow, "c"))
    assert saved.expires_at == datetime.fromtimestamp(1893456000, tz=timezone.utc)
    assert h.load(store, "user:9").expires_at == saved.expires_at
    assert form(intro.calls.last.request)["client_secret"] == "web-s3cr3t"
    assert sf._transport.tokens["user:9"].expires_at == saved.expires_at


def test_revoke_uses_refresh_token_and_clears_store(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(REVOKE_URL).respond(200)
    store = h.new_store()
    auth = web_auth()
    sf = h.client(auth, store=store, token=make_token("AT-x", "RT-x"))
    h.run(sf.auth.token())  # load into memory
    h.run(sf.auth.revoke())
    assert form(route.calls.last.request) == {"token": "RT-x"}
    assert h.load(store, auth.token_key) is None
    assert sf._runtime.current() is None


def test_revoke_other_key_and_access_token_fallback(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(REVOKE_URL).respond(400, json={"error": "invalid_token"})
    store = h.new_store()
    h.seed(store, "user:other", make_token("AT-only"))
    sf = h.client(store=store)
    h.run(sf.auth.revoke("user:other"))  # already-invalid token counts as revoked
    assert form(route.calls.last.request) == {"token": "AT-only"}
    assert h.load(store, "user:other") is None
    h.run(sf.auth.revoke("user:missing"))  # nothing stored: no POST
    assert route.call_count == 1


def test_revoke_error_raises_and_keeps_store(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(REVOKE_URL).respond(503)
    store = h.new_store()
    auth = web_auth()
    sf = h.client(auth, store=store, token=make_token("AT", "RT"))
    with pytest.raises(esf.OAuthError) as ei:
        h.run(sf.auth.revoke())
    assert ei.value.operation == "oauth.revoke"
    assert h.load(store, auth.token_key) is not None


def test_revoke_static_token_posts_to_instance(h: Harness, mock: respx.MockRouter) -> None:
    """Review m11: a StaticToken revoke really revokes (at its instance), not just forgets."""
    route = mock.post(f"{INSTANCE}/services/oauth2/revoke").respond(200)
    sf = h.client(esf.StaticToken("00D!x", INSTANCE))
    h.run(sf.auth.revoke())
    assert route.called
    assert route.calls.last.request.content == b"token=00D%21x"


def test_revoke_other_key_needs_auth_for_static_client(h: Harness) -> None:
    sf = h.client(esf.StaticToken("00D!x", INSTANCE))
    with pytest.raises(ValueError, match="auth="):
        h.run(sf.auth.revoke("user:9"))


def test_userinfo_goes_through_pipeline(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(USERINFO_URL).respond(json=load_fixture("oauth/userinfo.json"))
    sf = h.client()
    info = h.run(sf.auth.userinfo())
    assert info["user_id"] == "005xx0000000001AAA"
    assert route.calls.last.request.headers["Authorization"] == "Bearer AT-initial-0001"


def test_introspect(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(INTROSPECT_URL)
    route.side_effect = [
        httpx.Response(200, json=load_fixture("oauth/introspect_active.json")),
        httpx.Response(200, json=["odd"]),
        httpx.Response(401, json={"error": "invalid_client", "error_description": "nope"}),
    ]
    store = h.new_store()
    h.seed(store, "user:5", make_token("AT-five"))
    sf = h.client(web_auth(), store=store, token=make_token("AT-main", "RT"))
    assert h.run(sf.auth.introspect())["active"] is True
    f = form(route.calls.last.request)
    assert f["token"] == "AT-main"
    assert f["client_id"] == "cid-web"
    assert h.run(sf.auth.introspect("user:5")) == {}
    assert form(route.calls.last.request)["token"] == "AT-five"
    with pytest.raises(esf.OAuthError) as ei:
        h.run(sf.auth.introspect())
    assert ei.value.status == 401
    assert ei.value.operation == "oauth.introspect"


def test_introspect_acquires_token_when_missing(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(TOKEN_URL).respond(json=token_body("AT-fresh"))
    route = mock.post(INTROSPECT_URL).respond(json={"active": True})
    sf = h.client(token=False)
    h.run(sf.auth.introspect())
    assert form(route.calls.last.request)["token"] == "AT-fresh"


def test_introspect_refresh_token_hint(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(INTROSPECT_URL).respond(json={"active": True})
    sf = h.client(web_auth(), token=make_token("AT", "RT-hint"))
    h.run(sf._runtime.introspect(make_token("AT", "RT-hint"), hint="refresh_token"))
    f = form(route.calls.last.request)
    assert f["token"] == "RT-hint"
    assert f["token_type_hint"] == "refresh_token"
