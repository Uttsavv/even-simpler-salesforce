"""Token refresh (§6.4, §6.5) through the full pipeline, for both clients."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, INSTANCE, TOKEN_URL, Harness, Recorder, cc_auth, make_token, token_body, web_auth
from ..fixtures import load_fixture

LEAD_ID = "00Qxx0000000001AAA"
GET_URL = f"{DATA}/sobjects/Lead/{LEAD_ID}"
INTROSPECT_URL = f"{INSTANCE}/services/oauth2/introspect"
EXPIRED = httpx.Response(401, json=load_fixture("errors/invalid_session.json"))
OK = httpx.Response(200, json={"Id": LEAD_ID})


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def bearer(request: httpx.Request) -> str:
    return request.headers["Authorization"].removeprefix("Bearer ")


def get(h: Harness, sf: Any) -> Any:
    return h.run(sf.sobject("Lead").get(LEAD_ID))


def auth_by_token(responses: dict[str, httpx.Response]) -> Any:
    """respx side effect that answers by the bearer token used."""

    def handler(request: httpx.Request) -> httpx.Response:
        return responses[bearer(request)]

    return handler


# --- 401 → one refresh → resend ---------------------------------------------------------------


def test_401_refreshes_once_and_resends(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    api = mock.get(GET_URL).mock(side_effect=auth_by_token({"AT-initial-0001": EXPIRED, "AT-2": OK}))
    tok = mock.post(TOKEN_URL).respond(json=token_body("AT-2"))
    store = h.new_store()
    auth = cc_auth()
    sf = h.client(auth, store=store, recorder=rec)
    assert get(h, sf) == {"Id": LEAD_ID}
    assert tok.call_count == 1
    assert form(tok.calls.last.request) == {
        "grant_type": "client_credentials",
        "client_id": "cid-123",
        "client_secret": "s3cr3t-value",
    }
    assert [bearer(c.request) for c in api.calls] == ["AT-initial-0001", "AT-2"]
    assert h.load(store, auth.token_key).access_token == "AT-2"
    ev = rec.requests[-1]
    assert ev["refreshed"] is True
    assert ev["outcome"] == "ok"
    assert ev["attempts"] == 2
    (refreshed,) = rec.named("sf.auth.refreshed")
    assert refreshed["flow"] == "client_credentials"
    assert refreshed["rotated"] is False
    assert refreshed["proactive"] is False
    assert refreshed["initial"] is False
    assert len(refreshed["token_key_hash"]) == 12
    assert sf.usage.snapshot().refreshes == 1
    # The new token is reused for later calls (no further refresh).
    get(h, sf)
    assert tok.call_count == 1


def test_second_401_raises_session_expired(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    api = mock.get(GET_URL).mock(return_value=EXPIRED)
    tok = mock.post(TOKEN_URL).respond(json=token_body("AT-2"))
    sf = h.client(recorder=rec)
    with pytest.raises(esf.SessionExpired) as ei:
        get(h, sf)
    assert "after one token refresh" in ei.value.message
    assert ei.value.error_code == "INVALID_SESSION_ID"
    assert tok.call_count == 1
    assert api.call_count == 2
    assert rec.requests[-1]["refreshed"] is True
    assert rec.requests[-1]["outcome"] == "error"


def test_oauth_invalid_token_401_also_refreshes(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(GET_URL).mock(
        side_effect=auth_by_token(
            {"AT-initial-0001": httpx.Response(401, json={"error": "invalid_token"}), "AT-2": OK}
        )
    )
    tok = mock.post(TOKEN_URL).respond(json=token_body("AT-2"))
    sf = h.client()
    assert get(h, sf) == {"Id": LEAD_ID}
    assert tok.call_count == 1


def test_static_token_401_raises_without_token_post(h: Harness, mock: respx.MockRouter) -> None:
    api = mock.get(GET_URL).mock(return_value=EXPIRED)
    tok = mock.post(url__regex=r".*/services/oauth2/token").respond(json=token_body("x"))
    sf = h.client(esf.StaticToken("00D!static", INSTANCE))
    with pytest.raises(esf.SessionExpired):
        get(h, sf)
    assert not tok.called
    assert api.call_count == 1
    assert bearer(api.calls.last.request) == "00D!static"


def test_static_token_refresh_api_raises(h: Harness) -> None:
    sf = h.client(esf.StaticToken("00D!static", INSTANCE))
    tok = h.run(sf.auth.token())
    with pytest.raises(esf.SessionExpired):
        h.run(sf._runtime.refresh(tok))


# --- rotation -------------------------------------------------------------------------------------


def test_rotation_saved_before_resend(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    store = h.new_store()
    auth = web_auth()
    seen_in_store: list[tuple[str, str | None]] = []

    def api(request: httpx.Request) -> httpx.Response:
        if bearer(request) == "AT-initial-0001":
            return EXPIRED
        stored = store._data[auth.token_key]  # what is persisted at the moment of the resend
        seen_in_store.append((stored.access_token, stored.refresh_token))
        return OK

    mock.get(GET_URL).mock(side_effect=api)
    tok = mock.post(TOKEN_URL).respond(json=token_body("AT-2", "RT-2"))
    sf = h.client(auth, store=store, token=make_token(refresh="RT-1"), recorder=rec)
    assert get(h, sf) == {"Id": LEAD_ID}
    assert seen_in_store == [("AT-2", "RT-2")]
    f = form(tok.calls.last.request)
    assert f["grant_type"] == "refresh_token"
    assert f["refresh_token"] == "RT-1"
    assert f["client_secret"] == "web-s3cr3t"
    (refreshed,) = rec.named("sf.auth.refreshed")
    assert refreshed["rotated"] is True
    assert refreshed["flow"] == "web_server"
    saved = h.load(store, auth.token_key)
    assert saved.version == 2  # seeded (1) + rotation save


def test_refresh_without_rotation_keeps_refresh_token(h: Harness, mock: respx.MockRouter) -> None:
    store = h.new_store()
    auth = web_auth()
    mock.get(GET_URL).mock(side_effect=auth_by_token({"AT-initial-0001": EXPIRED, "AT-2": OK}))
    mock.post(TOKEN_URL).respond(json=token_body("AT-2"))  # no refresh_token in the response
    sf = h.client(auth, store=store, token=make_token(refresh="RT-1"))
    get(h, sf)
    saved = h.load(store, auth.token_key)
    assert (saved.access_token, saved.refresh_token) == ("AT-2", "RT-1")


def test_web_flow_without_refresh_token_needs_reauth(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(GET_URL).mock(return_value=EXPIRED)
    tok = mock.post(TOKEN_URL)
    sf = h.client(web_auth(), token=make_token())
    with pytest.raises(esf.ReauthorizationRequired) as ei:
        get(h, sf)
    assert ei.value.error_code == "no_refresh_token"
    assert not tok.called
    sf2 = h.client(web_auth(token_key="user:none"), token=False)
    with pytest.raises(esf.ReauthorizationRequired):
        get(h, sf2)


# --- "Token request is already being processed" -------------------------------------------------------


def test_busy_backs_off_and_retries(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(GET_URL).mock(side_effect=auth_by_token({"AT-initial-0001": EXPIRED, "AT-2": OK}))
    busy = httpx.Response(400, json=load_fixture("oauth/busy.json"))
    tok = mock.post(TOKEN_URL)
    tok.side_effect = [busy, busy, httpx.Response(200, json=token_body("AT-2", "RT-2"))]
    sf = h.client(web_auth(), token=make_token(refresh="RT-1"))
    assert get(h, sf) == {"Id": LEAD_ID}
    assert tok.call_count == 3
    assert sf.sleeps == [0.25, 0.5]


def test_busy_is_bounded(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(GET_URL).mock(return_value=EXPIRED)
    tok = mock.post(TOKEN_URL).respond(400, json=load_fixture("oauth/busy.json"))
    sf = h.client(web_auth(), token=make_token(refresh="RT-1"))
    with pytest.raises(esf.RefreshFailed) as ei:
        get(h, sf)
    assert tok.call_count == 3  # bounded: 3 attempts in total
    assert "already being processed" in ei.value.message
    assert ei.value.ambiguous is False


def test_busy_reloads_store_and_uses_other_processes_token(h: Harness, mock: respx.MockRouter) -> None:
    store = h.new_store()
    auth = web_auth()

    def busy_then_other_process_saves(request: httpx.Request) -> httpx.Response:
        current = store._data[auth.token_key]
        store._data[auth.token_key] = make_token("AT-other", "RT-other").replace(version=current.version + 1)
        return httpx.Response(400, json=load_fixture("oauth/busy.json"))

    api = mock.get(GET_URL).mock(side_effect=auth_by_token({"AT-initial-0001": EXPIRED, "AT-other": OK}))
    tok = mock.post(TOKEN_URL).mock(side_effect=busy_then_other_process_saves)
    sf = h.client(auth, store=store, token=make_token(refresh="RT-1"))
    assert get(h, sf) == {"Id": LEAD_ID}
    assert tok.call_count == 1
    assert bearer(api.calls.last.request) == "AT-other"


# --- invalid_grant ---------------------------------------------------------------------------------


def test_invalid_grant_requires_reauthorization(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    api = mock.get(GET_URL).mock(return_value=EXPIRED)
    tok = mock.post(TOKEN_URL).respond(400, json=load_fixture("oauth/invalid_grant.json"))
    auth = web_auth()
    sf = h.client(auth, token=make_token(refresh="RT-revoked"), recorder=rec)
    with pytest.raises(esf.ReauthorizationRequired) as ei:
        get(h, sf)
    assert ei.value.error_code == "invalid_grant"
    assert ei.value.message == "expired access/refresh token"
    assert tok.call_count == 1
    assert api.call_count == 1
    (ev,) = rec.named("sf.auth.reauth_required")
    assert ev["token_key_hash"] == sf._runtime.key_hash
    assert auth.token_key not in str(ev.fields)
    assert rec.requests[-1]["error_class"] == "ReauthorizationRequired"


# --- ambiguous refresh ---------------------------------------------------------------------------------


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("reset")])
def test_ambiguous_refresh_is_not_resent(h: Harness, mock: respx.MockRouter, exc: Exception) -> None:
    api = mock.get(GET_URL).mock(return_value=EXPIRED)
    tok = mock.post(TOKEN_URL).mock(side_effect=exc)
    sf = h.client(web_auth(), token=make_token(refresh="RT-1"))
    with pytest.raises(esf.RefreshFailed) as ei:
        get(h, sf)
    assert ei.value.ambiguous is True
    assert tok.call_count == 1
    assert api.call_count == 1
    assert sf.sleeps == [0.25]  # brief pause before re-loading the store


def test_ambiguous_refresh_uses_store_if_it_changed(h: Harness, mock: respx.MockRouter) -> None:
    store = h.new_store()
    auth = web_auth()

    def saved_then_timed_out(request: httpx.Request) -> httpx.Response:
        current = store._data[auth.token_key]
        store._data[auth.token_key] = make_token("AT-landed", "RT-landed").replace(
            version=current.version + 1
        )
        raise httpx.ReadTimeout("slow")

    api = mock.get(GET_URL).mock(side_effect=auth_by_token({"AT-initial-0001": EXPIRED, "AT-landed": OK}))
    tok = mock.post(TOKEN_URL).mock(side_effect=saved_then_timed_out)
    sf = h.client(auth, store=store, token=make_token(refresh="RT-1"))
    assert get(h, sf) == {"Id": LEAD_ID}
    assert tok.call_count == 1
    assert [bearer(c.request) for c in api.calls] == ["AT-initial-0001", "AT-landed"]


def test_client_credentials_timeout_is_safe_to_resend(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(GET_URL).mock(side_effect=auth_by_token({"AT-initial-0001": EXPIRED, "AT-2": OK}))
    tok = mock.post(TOKEN_URL)
    tok.side_effect = [httpx.ReadTimeout("slow"), httpx.Response(200, json=token_body("AT-2"))]
    sf = h.client()
    assert get(h, sf) == {"Id": LEAD_ID}
    assert tok.call_count == 2


def test_client_credentials_timeouts_exhausted(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(GET_URL).mock(return_value=EXPIRED)
    tok = mock.post(TOKEN_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    sf = h.client()
    with pytest.raises(esf.RefreshFailed) as ei:
        get(h, sf)
    assert ei.value.ambiguous is True
    assert tok.call_count == 3


def test_connect_error_on_refresh_is_retried_even_with_refresh_token(
    h: Harness, mock: respx.MockRouter
) -> None:
    mock.get(GET_URL).mock(side_effect=auth_by_token({"AT-initial-0001": EXPIRED, "AT-2": OK}))
    tok = mock.post(TOKEN_URL)
    tok.side_effect = [httpx.ConnectError("refused"), httpx.Response(200, json=token_body("AT-2", "RT-2"))]
    sf = h.client(web_auth(), token=make_token(refresh="RT-1"))
    assert get(h, sf) == {"Id": LEAD_ID}
    assert tok.call_count == 2


def test_connect_error_on_refresh_exhausted(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(GET_URL).mock(return_value=EXPIRED)
    tok = mock.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("refused"))
    sf = h.client(web_auth(), token=make_token(refresh="RT-1"))
    with pytest.raises(esf.RefreshFailed) as ei:
        get(h, sf)
    assert ei.value.ambiguous is False
    assert tok.call_count == 3


# --- other token endpoint failures ------------------------------------------------------------------


def test_token_endpoint_oauth_error(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(GET_URL).mock(return_value=EXPIRED)
    mock.post(TOKEN_URL).respond(400, json={"error": "invalid_client", "error_description": "bad secret"})
    sf = h.client()
    with pytest.raises(esf.OAuthError) as ei:
        get(h, sf)
    assert ei.value.error == "invalid_client"
    assert ei.value.operation == "oauth.refresh"
    assert "s3cr3t-value" not in str(ei.value)
    assert "s3cr3t-value" not in repr(ei.value)


def test_token_endpoint_5xx(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(GET_URL).mock(side_effect=auth_by_token({"AT-initial-0001": EXPIRED, "AT-2": OK}))
    tok = mock.post(TOKEN_URL)
    tok.side_effect = [httpx.Response(503), httpx.Response(200, json=token_body("AT-2"))]
    sf = h.client()  # client credentials: retried
    assert get(h, sf) == {"Id": LEAD_ID}
    assert tok.call_count == 2


def test_token_endpoint_5xx_not_resent_for_refresh_token_grant(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(GET_URL).mock(return_value=EXPIRED)
    tok = mock.post(TOKEN_URL).respond(503, text="down")
    sf = h.client(web_auth(), token=make_token(refresh="RT-1"))
    with pytest.raises(esf.RefreshFailed) as ei:
        get(h, sf)
    assert ei.value.status == 503
    assert tok.call_count == 1  # never resend a refresh-token grant blindly


@pytest.mark.parametrize(
    "resp",
    [
        httpx.Response(200, text="<html/>"),
        httpx.Response(200, json=["not", "a", "dict"]),
        httpx.Response(200, json={"instance_url": INSTANCE}),
    ],
)
def test_token_endpoint_bad_success_body(h: Harness, mock: respx.MockRouter, resp: httpx.Response) -> None:
    mock.get(GET_URL).mock(return_value=EXPIRED)
    mock.post(TOKEN_URL).mock(return_value=resp)
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse):
        get(h, sf)


# --- store races ---------------------------------------------------------------------------------------


def test_other_process_already_refreshed(h: Harness, mock: respx.MockRouter) -> None:
    store = h.new_store()
    auth = web_auth()
    api = mock.get(GET_URL)
    sf = h.client(auth, store=store, token=make_token(refresh="RT-1"))

    def first(request: httpx.Request) -> httpx.Response:
        if bearer(request) == "AT-initial-0001":
            # Another process refreshes while our request is in flight.
            cur = store._data[auth.token_key]
            store._data[auth.token_key] = make_token("AT-other", "RT-other").replace(version=cur.version + 1)
            return EXPIRED
        return OK

    api.mock(side_effect=first)
    tok = mock.post(TOKEN_URL)
    assert get(h, sf) == {"Id": LEAD_ID}
    assert not tok.called
    assert bearer(api.calls.last.request) == "AT-other"


def test_token_conflict_uses_stored_token(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    store = h.new_store()
    auth = web_auth()

    def token_post(request: httpx.Request) -> httpx.Response:
        # Another process saves while our refresh is in flight → our CAS save conflicts.
        cur = store._data[auth.token_key]
        store._data[auth.token_key] = cur.replace(access_token="AT-winner", version=cur.version + 5)
        return httpx.Response(200, json=token_body("AT-mine", "RT-mine"))

    api = mock.get(GET_URL).mock(side_effect=auth_by_token({"AT-initial-0001": EXPIRED, "AT-winner": OK}))
    mock.post(TOKEN_URL).mock(side_effect=token_post)
    sf = h.client(auth, store=store, token=make_token(refresh="RT-1"), recorder=rec)
    assert get(h, sf) == {"Id": LEAD_ID}
    assert bearer(api.calls.last.request) == "AT-winner"
    assert h.load(store, auth.token_key).access_token == "AT-winner"
    assert not rec.named("sf.auth.refreshed")


def test_token_conflict_with_deleted_record_raises(h: Harness, mock: respx.MockRouter) -> None:
    store = h.new_store()
    auth = web_auth()

    def token_post(request: httpx.Request) -> httpx.Response:
        store._data.clear()  # e.g. the user logged out elsewhere
        return httpx.Response(200, json=token_body("AT-mine", "RT-mine"))

    mock.get(GET_URL).mock(return_value=EXPIRED)
    mock.post(TOKEN_URL).mock(side_effect=token_post)
    sf = h.client(auth, store=store, token=make_token(refresh="RT-1"))
    with pytest.raises(esf.TokenConflict):
        get(h, sf)


# --- initial acquisition -------------------------------------------------------------------------------


def test_initial_acquisition_client_credentials(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    tok = mock.post(TOKEN_URL).respond(json=token_body("AT-new"))
    api = mock.get(GET_URL).respond(json={"Id": LEAD_ID})
    store = h.new_store()
    sf = h.client(store=store, token=False, recorder=rec)
    assert repr(sf).endswith("auth=client_credentials>")
    assert "not connected" in repr(sf)
    get(h, sf)
    get(h, sf)
    assert tok.call_count == 1
    assert bearer(api.calls.last.request) == "AT-new"
    (ev,) = rec.named("sf.auth.refreshed")
    assert ev["initial"] is True
    assert sf.usage.snapshot().refreshes == 0
    assert INSTANCE in repr(sf)
    with esf.capture() as summary:
        get(h, sf)
    assert summary.as_fields()["sf_refreshed"] is False


def test_initial_acquisition_jwt_bearer(h: Harness, mock: respx.MockRouter) -> None:
    jwt = pytest.importorskip("jwt")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    tok = mock.post(TOKEN_URL).respond(json=token_body("AT-jwt"))
    mock.get(GET_URL).respond(json={"Id": LEAD_ID})
    auth = esf.JwtBearer(domain=INSTANCE, client_id="cid", username="u@x.com", private_key=pem)
    sf = h.client(auth, token=False)
    get(h, sf)
    f = form(tok.calls.last.request)
    assert f["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    claims = jwt.decode(
        f["assertion"], key.public_key(), algorithms=["RS256"], audience="https://login.salesforce.com"
    )
    assert claims["sub"] == "u@x.com"


def test_token_loaded_from_store_without_post(h: Harness, mock: respx.MockRouter) -> None:
    tok = mock.post(TOKEN_URL)
    api = mock.get(GET_URL).respond(json={"Id": LEAD_ID})
    sf = h.client(token=make_token("AT-from-store"))
    get(h, sf)
    assert not tok.called
    assert bearer(api.calls.last.request) == "AT-from-store"


# --- proactive refresh ---------------------------------------------------------------------------------


def test_refresh_ahead(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    api = mock.get(GET_URL).respond(json={"Id": LEAD_ID})
    tok = mock.post(TOKEN_URL).respond(
        json=token_body("AT-2", "RT-2", issued_at_ms=int(now.timestamp() * 1000))
    )
    auth = web_auth(refresh_ahead=timedelta(hours=1))
    old = make_token(refresh="RT-1", issued_at=now - timedelta(hours=2))
    sf = h.client(auth, token=old, recorder=rec)
    sf._transport.now = lambda: now
    get(h, sf)
    assert tok.call_count == 1
    assert [bearer(c.request) for c in api.calls] == ["AT-2"]
    (ev,) = rec.named("sf.auth.refreshed")
    assert ev["proactive"] is True
    get(h, sf)
    assert tok.call_count == 1  # fresh now
    sf._transport.now = lambda: now + timedelta(hours=1, seconds=1)
    get(h, sf)
    assert tok.call_count == 2


def test_refresh_ahead_not_due(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(GET_URL).respond(json={"Id": LEAD_ID})
    tok = mock.post(TOKEN_URL)
    auth = web_auth(refresh_ahead=timedelta(hours=1))
    sf = h.client(auth, token=make_token(refresh="RT-1"))
    get(h, sf)
    assert not tok.called


def test_expiry_introspect_with_exp(h: Harness, mock: respx.MockRouter) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    exp = int((now + timedelta(hours=2)).timestamp())
    body = load_fixture("oauth/introspect_active.json")
    body["exp"] = exp
    intro = mock.post(INTROSPECT_URL).respond(json=body)
    tok = mock.post(TOKEN_URL)
    tok.side_effect = [httpx.Response(200, json=token_body(f"AT-{i}")) for i in range(1, 4)]
    api = mock.get(GET_URL).respond(json={"Id": LEAD_ID})
    store = h.new_store()
    auth = cc_auth(expiry="introspect")
    sf = h.client(auth, store=store, token=False)
    sf._transport.now = lambda: now
    get(h, sf)
    assert tok.call_count == 1
    assert intro.call_count == 1
    f = form(intro.calls.last.request)
    assert f["token"] == "AT-1"
    assert f["token_type_hint"] == "access_token"
    saved = h.load(store, auth.token_key)
    assert saved.expires_at == datetime.fromtimestamp(exp, tz=timezone.utc)
    get(h, sf)
    assert tok.call_count == 1
    # Within the 60 s skew of exp → refreshed proactively before the call.
    sf._transport.now = lambda: datetime.fromtimestamp(exp - 30, tz=timezone.utc)
    get(h, sf)
    assert tok.call_count == 2
    assert bearer(api.calls.last.request) == "AT-2"


def test_expiry_introspect_without_exp_falls_back(
    h: Harness, mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    intro = mock.post(INTROSPECT_URL).respond(json=load_fixture("oauth/introspect_no_exp.json"))
    tok = mock.post(TOKEN_URL).mock(side_effect=lambda r: httpx.Response(200, json=token_body("AT-n")))
    mock.get(GET_URL).mock(side_effect=auth_by_token({"AT-initial-0001": EXPIRED, "AT-n": OK}))
    store = h.new_store()
    auth = cc_auth(expiry="introspect")
    sf = h.client(auth, store=store)
    with caplog.at_level(logging.INFO, logger="esf"):
        get(h, sf)
        sf._runtime.transport.tokens.clear()
        h.run(sf._runtime.refresh(make_token("AT-n")))
    assert tok.call_count == 2
    assert intro.call_count == 2
    assert h.load(store, auth.token_key).expires_at is None
    msgs = [r.getMessage() for r in caplog.records if "introspection" in r.getMessage()]
    assert len(msgs) == 1  # logged once
    assert "no 'exp'" in msgs[0]


def test_expiry_introspect_failure_falls_back(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(INTROSPECT_URL).respond(500, json={"error": "server_error"})
    mock.post(TOKEN_URL).respond(json=token_body("AT-n"))
    mock.get(GET_URL).respond(json={"Id": LEAD_ID})
    sf = h.client(cc_auth(expiry="introspect"), token=False)
    get(h, sf)
    assert sf._runtime.current().expires_at is None


def test_expiry_introspect_save_conflict_keeps_stored_token(h: Harness, mock: respx.MockRouter) -> None:
    store = h.new_store()
    auth = cc_auth(expiry="introspect")

    def introspect(request: httpx.Request) -> httpx.Response:
        cur = store._data[auth.token_key]
        store._data[auth.token_key] = cur.replace(access_token="AT-elsewhere", version=cur.version + 1)
        return httpx.Response(200, json=load_fixture("oauth/introspect_active.json"))

    mock.post(INTROSPECT_URL).mock(side_effect=introspect)
    mock.post(TOKEN_URL).respond(json=token_body("AT-1"))
    api = mock.get(GET_URL).respond(json={"Id": LEAD_ID})
    sf = h.client(auth, store=store, token=False)
    get(h, sf)
    assert bearer(api.calls.last.request) == "AT-elsewhere"
