"""A refreshed or re-read token must belong to the same org and user (both clients).

The refresh-token, JWT bearer and client-credentials token responses all carry ``id``
(``https://login.salesforce.com/id/<orgId>/<userId>``):

- https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_refresh_token_flow.htm&type=5
- https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_jwt_flow.htm&type=5
- https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_client_credentials_flow.htm&type=5

The bodies below are ``synthetic``: ``token_body`` with another ``id``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import (
    DATA,
    INSTANCE,
    ORG_ID,
    TOKEN_URL,
    USER_ID,
    Harness,
    cc_auth,
    make_token,
    token_body,
    web_auth,
)
from ..fixtures import load_fixture

LEAD_ID = "00Qxx0000000001AAA"
GET_URL = f"{DATA}/sobjects/Lead/{LEAD_ID}"
INTROSPECT_URL = f"{INSTANCE}/services/oauth2/introspect"
EXPIRED = httpx.Response(401, json=load_fixture("errors/invalid_session.json"))
OK = httpx.Response(200, json={"Id": LEAD_ID})
OTHER_USER = "005xx0000000002AAA"
OTHER_ORG = "00Dxx0000000002EAA"


def other_body(
    access: str, refresh: str | None = None, *, org: str = ORG_ID, user: str = OTHER_USER
) -> dict[str, Any]:
    body = token_body(access, refresh)
    body["id"] = f"https://login.salesforce.com/id/{org}/{user}"
    return body


def other_token(
    access: str, refresh: str | None = None, *, org: str = ORG_ID, user: str = OTHER_USER
) -> esf.Token:
    return make_token(access, refresh).replace(
        id_url=f"https://login.salesforce.com/id/{org}/{user}", org_id=org, user_id=user
    )


def bearer(request: httpx.Request) -> str:
    return request.headers["Authorization"].removeprefix("Bearer ")


def get(h: Harness, sf: Any) -> Any:
    return h.run(sf.sobject("Lead").get(LEAD_ID))


def jwt_auth() -> esf.JwtBearer:
    pytest.importorskip("jwt")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return esf.JwtBearer(domain=INSTANCE, client_id="cid", username="u@x.com", private_key=pem)


@pytest.mark.parametrize("flow", ["web", "client_credentials", "jwt"])
@pytest.mark.parametrize(
    ("org", "user", "changed"), [(ORG_ID, OTHER_USER, "user"), (OTHER_ORG, USER_ID, "org")]
)
def test_refresh_response_for_another_identity_raises(
    h: Harness, mock: respx.MockRouter, flow: str, org: str, user: str, changed: str
) -> None:
    auth = {"web": web_auth, "client_credentials": cc_auth, "jwt": jwt_auth}[flow]()
    api = mock.get(GET_URL).mock(return_value=EXPIRED)
    mock.post(TOKEN_URL).respond(json=other_body("AT-intruder", "RT-intruder", org=org, user=user))
    store = h.new_store()
    sf = h.client(auth, store=store, token=make_token(refresh="RT-1"))
    with pytest.raises(esf.TokenIdentityChanged) as ei:
        get(h, sf)
    assert isinstance(ei.value, esf.AuthError)
    assert f"another {changed} " in ei.value.message
    assert "token_key_hash=" in ei.value.message
    text = str(ei.value) + repr(ei.value)
    for secret in ("AT-intruder", "RT-intruder", "AT-initial-0001", "RT-1"):
        assert secret not in text
    assert ei.value.docs_url is not None
    assert ei.value.docs_url.endswith("#tokenidentitychanged")
    # Never stored, never sent.
    assert h.load(store, auth.token_key).access_token == "AT-initial-0001"
    assert all(bearer(c.request) != "AT-intruder" for c in api.calls)


def test_refresh_with_same_identity_and_15_char_ids_is_used(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(GET_URL).mock(side_effect=lambda r: OK if bearer(r) == "AT-2" else EXPIRED)
    mock.post(TOKEN_URL).respond(json=token_body("AT-2"))
    short = make_token(refresh="RT-1").replace(org_id=ORG_ID[:15], user_id=USER_ID[:15])
    sf = h.client(web_auth(), token=short)
    assert get(h, sf) == {"Id": LEAD_ID}


def test_refresh_without_id_keeps_previous_identity(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(GET_URL).mock(side_effect=lambda r: OK if bearer(r) == "AT-2" else EXPIRED)
    body = token_body("AT-2")
    del body["id"]
    mock.post(TOKEN_URL).respond(json=body)
    sf = h.client(web_auth(), token=make_token(refresh="RT-1"))
    assert get(h, sf) == {"Id": LEAD_ID}
    assert sf._runtime.current().user_id == USER_ID


def test_store_refreshed_by_other_process_for_another_user_raises(h: Harness, mock: respx.MockRouter) -> None:
    store = h.new_store()
    auth = web_auth()
    sf = h.client(auth, store=store, token=make_token(refresh="RT-1"))

    def first(request: httpx.Request) -> httpx.Response:
        cur = store._data[auth.token_key]
        store._data[auth.token_key] = other_token("AT-other", "RT-other").replace(version=cur.version + 1)
        return EXPIRED

    api = mock.get(GET_URL).mock(side_effect=first)
    tok = mock.post(TOKEN_URL)
    with pytest.raises(esf.TokenIdentityChanged, match="stored token"):
        get(h, sf)
    assert not tok.called
    assert api.call_count == 1


def test_busy_reload_for_another_user_raises(h: Harness, mock: respx.MockRouter) -> None:
    store = h.new_store()
    auth = web_auth()

    def busy_then_other_saves(request: httpx.Request) -> httpx.Response:
        cur = store._data[auth.token_key]
        store._data[auth.token_key] = other_token("AT-other", "RT-other").replace(version=cur.version + 1)
        return httpx.Response(400, json=load_fixture("oauth/busy.json"))

    api = mock.get(GET_URL).mock(return_value=EXPIRED)
    mock.post(TOKEN_URL).mock(side_effect=busy_then_other_saves)
    sf = h.client(auth, store=store, token=make_token(refresh="RT-1"))
    with pytest.raises(esf.TokenIdentityChanged):
        get(h, sf)
    assert api.call_count == 1


def test_ambiguous_reload_for_another_user_raises(h: Harness, mock: respx.MockRouter) -> None:
    store = h.new_store()
    auth = web_auth()

    def saved_then_timed_out(request: httpx.Request) -> httpx.Response:
        cur = store._data[auth.token_key]
        store._data[auth.token_key] = other_token("AT-other", "RT-other").replace(version=cur.version + 1)
        raise httpx.ReadTimeout("slow")

    api = mock.get(GET_URL).mock(return_value=EXPIRED)
    mock.post(TOKEN_URL).mock(side_effect=saved_then_timed_out)
    sf = h.client(auth, store=store, token=make_token(refresh="RT-1"))
    with pytest.raises(esf.TokenIdentityChanged):
        get(h, sf)
    assert api.call_count == 1


def test_token_conflict_reload_for_another_user_raises(h: Harness, mock: respx.MockRouter) -> None:
    store = h.new_store()
    auth = web_auth()

    def token_post(request: httpx.Request) -> httpx.Response:
        cur = store._data[auth.token_key]
        store._data[auth.token_key] = other_token("AT-winner", "RT-winner").replace(version=cur.version + 5)
        return httpx.Response(200, json=token_body("AT-mine", "RT-mine"))

    api = mock.get(GET_URL).mock(return_value=EXPIRED)
    mock.post(TOKEN_URL).mock(side_effect=token_post)
    sf = h.client(auth, store=store, token=make_token(refresh="RT-1"))
    with pytest.raises(esf.TokenIdentityChanged):
        get(h, sf)
    assert api.call_count == 1


def test_introspect_conflict_reload_for_another_user_raises(h: Harness, mock: respx.MockRouter) -> None:
    store = h.new_store()
    auth = cc_auth(expiry="introspect")

    def introspect(request: httpx.Request) -> httpx.Response:
        cur = store._data[auth.token_key]
        store._data[auth.token_key] = other_token("AT-elsewhere").replace(version=cur.version + 1)
        return httpx.Response(200, json=load_fixture("oauth/introspect_active.json"))

    mock.post(INTROSPECT_URL).mock(side_effect=introspect)
    mock.post(TOKEN_URL).respond(json=token_body("AT-1"))
    api = mock.get(GET_URL).respond(json={"Id": LEAD_ID})
    sf = h.client(auth, store=store, token=False)
    with pytest.raises(esf.TokenIdentityChanged):
        get(h, sf)
    assert not api.called
