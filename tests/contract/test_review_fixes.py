"""Regression tests for the 0.1 spec review and the SOQL-builder side review (both clients)."""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf
from even_simpler_salesforce.core.http import build_url, merge_call_options
from even_simpler_salesforce.core.soql import quote, redact_soql, soql_hash

from ..conftest import DATA, INSTANCE, TOKEN_URL, Harness, Recorder, cc_auth, make_token, token_body

LEAD = "00Q000000000001AAA"


# --- retry safety (ADR 0001) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "call"),
    [
        ("PATCH", lambda sf: sf.sobject("Lead").update(LEAD, {"Status": "x"})),
        ("DELETE", lambda sf: sf.sobject("Lead").delete(LEAD)),
        ("PATCH", lambda sf: sf.sobject("Lead").upsert("Ext__c", "42", {"Status": "x"})),
    ],
)
def test_writes_not_resent_after_timeout(h: Harness, mock: respx.MockRouter, method: str, call: Any) -> None:
    route = mock.route(method=method, url__startswith=f"{DATA}/sobjects/Lead/").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    sf = h.client()
    with pytest.raises(esf.RequestTimeout):
        h.run(call(sf))
    assert route.call_count == 1  # triggers/flows must not run twice


def test_writes_still_resent_after_connect_error(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.patch(f"{DATA}/sobjects/Lead/{LEAD}")
    route.side_effect = [httpx.ConnectError("refused"), httpx.Response(204)]
    sf = h.client()
    h.run(sf.sobject("Lead").update(LEAD, {"Status": "x"}))
    assert route.call_count == 2  # never sent → safe


@pytest.mark.parametrize("path", ["/services/apexrest/orders", "tooling/executeAnonymous"])
def test_code_running_gets_not_resent(h: Harness, mock: respx.MockRouter, path: str) -> None:
    url = INSTANCE + path if path.startswith("/") else f"{DATA}/{path}"
    route = mock.get(url).mock(side_effect=httpx.ReadTimeout("slow"))
    sf = h.client()
    with pytest.raises(esf.RequestTimeout):
        h.run(sf.request("GET", path))
    assert route.call_count == 1
    route.side_effect = [httpx.ReadTimeout("slow"), httpx.Response(200, json={})]
    assert h.run(sf.request("GET", path, idempotent=True)) == {}  # explicit opt-in


# --- Sforce-Call-Options merge -------------------------------------------------------------------


def test_call_options_merged_with_callers(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/x").respond(json={})
    sf = h.client(client_name="acme")
    h.run(sf.request("GET", "x", headers={"sforce-call-options": "defaultNamespace=ns"}))
    headers = route.calls.last.request.headers
    assert headers.get_list("Sforce-Call-Options") == ["client=acme; defaultNamespace=ns"]


def test_merge_call_options_unit() -> None:
    assert merge_call_options({}, "client=a") == {"Sforce-Call-Options": "client=a"}
    assert merge_call_options({"SFORCE-CALL-OPTIONS": "client=b, x=1", "A": "1"}, "client=a") == {
        "A": "1",
        "Sforce-Call-Options": "client=b; x=1",  # caller's explicit client= wins for that call
    }
    # ADR 0012: the caller's value is split on ";" and ","; options are joined with "; ".
    assert merge_call_options({"Sforce-Call-Options": "defaultNamespace=ns; x=1,y=2"}, "client=a") == {
        "Sforce-Call-Options": "client=a; defaultNamespace=ns; x=1; y=2"
    }
    assert merge_call_options({"sforce-call-options": " ; ,"}, "client=a") == {
        "Sforce-Call-Options": "client=a"
    }


# --- UNSUPPORTED_API_VERSION -----------------------------------------------------------------------


def test_unsupported_api_version_mapped(h: Harness, mock: respx.MockRouter) -> None:
    # synthetic: status/body to be confirmed in a sandbox (V21)
    body = [{"errorCode": "UNSUPPORTED_API_VERSION", "message": "Invalid Api version specified on URL"}]
    mock.get(f"{DATA}/limits").respond(404, json=body)
    sf = h.client()
    with pytest.raises(esf.UnsupportedApiVersion) as ei:
        h.run(sf.limits())
    assert ei.value.error_code == "UNSUPPORTED_API_VERSION"


# --- m1 / m2 / m9 / m10 ----------------------------------------------------------------------------


def test_cursor_with_trailing_newline_rejected(h: Harness) -> None:
    sf = h.client()
    raw = base64.urlsafe_b64encode(b"/services/data/v68.0/query/01gD0000002HU6KIAW-2000\n").decode()
    with pytest.raises(esf.MalformedQuery):
        h.run(sf.query_more(raw.rstrip("=")))
    with pytest.raises(ValueError):
        h.run(sf.sobject("Lead").get(LEAD + "\n"))


def test_calls_after_close_raise_typed(h: Harness) -> None:
    sf = h.client()
    h.run(sf.aclose() if h.is_async else sf.close())
    with pytest.raises(esf.TransportError, match="closed"):
        h.run(sf.limits())


def test_not_found_on_type_is_invalid_type(h: Harness, mock: respx.MockRouter) -> None:
    body = [{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]
    mock.get(f"{DATA}/sobjects/Nope__c/describe").respond(404, json=body)
    mock.get(f"{DATA}/sobjects/Lead/{LEAD}").respond(404, json=body)
    sf = h.client()
    with pytest.raises(esf.InvalidType):
        h.run(sf.sobject("Nope__c").describe())
    with pytest.raises(esf.NotFound):
        h.run(sf.sobject("Lead").get(LEAD))


def test_instance_url_override_not_inherited_by_with_auth(h: Harness, mock: respx.MockRouter) -> None:
    other = mock.get("https://override.my.salesforce.com/services/data/v68.0/limits").respond(json={})
    own = mock.get(f"{DATA}/limits").respond(json={})
    store = h.new_store()
    sf = h.client(store=store, instance_url="https://override.my.salesforce.com")
    h.run(sf.limits())
    assert other.called
    child_auth = esf.ClientCredentials(domain=INSTANCE, client_id="other", client_secret="s")
    h.seed(store, child_auth.token_key, make_token("AT-child"))
    h.run(sf.with_auth(child_auth).limits())
    assert own.called


# --- auth nits -------------------------------------------------------------------------------------


def test_client_credentials_invalid_grant_is_oauth_error(
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    mock.post(TOKEN_URL).respond(400, json={"error": "invalid_grant", "error_description": "bad"})
    sf = h.client(cc_auth(), token=False, recorder=rec)
    with pytest.raises(esf.OAuthError) as ei:
        h.run(sf.limits())
    assert not isinstance(ei.value, esf.ReauthorizationRequired)
    assert not rec.named("sf.auth.reauth_required")


def test_observer_may_call_client_from_refresh_event(h: Harness, mock: respx.MockRouter) -> None:
    """sf.auth.refreshed is emitted after the per-key lock is released (no deadlock)."""
    mock.post(TOKEN_URL).respond(json=token_body("AT-new"))
    mock.get(f"{DATA}/limits").respond(json={})
    seen: list[Any] = []

    class Reentrant:
        def on_event(self, event: esf.Event) -> None:
            if event.name == "sf.auth.refreshed":
                seen.append(sf._runtime.current())  # touches runtime state; must not block

    sf = h.client(cc_auth(), token=False, observers=[Reentrant()])
    h.run(sf.limits())
    assert seen
    assert seen[0] is not None


def test_session_expired_args_updated(h: Harness, mock: respx.MockRouter) -> None:
    expired = httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "Session expired"}])
    mock.get(f"{DATA}/limits").mock(return_value=expired)
    mock.post(TOKEN_URL).respond(json=token_body("AT-2"))
    sf = h.client()
    with pytest.raises(esf.SessionExpired) as ei:
        h.run(sf.limits())
    assert ei.value.args == (ei.value.message,)
    assert "after one token refresh" in str(ei.value)


def test_exchange_code_non_dict_body(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(TOKEN_URL).respond(json=["nope"])
    sf = h.client()
    flow = esf.WebServerFlow(domain=INSTANCE, client_id="c", redirect_uri="https://a/cb", token_key="u:1")
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.auth.exchange_code(flow, "code"))


# --- pure helpers ----------------------------------------------------------------------------------


def test_sosl_terms_redacted() -> None:
    q = "FIND {john.doe@example.com} IN EMAIL FIELDS RETURNING Lead(Id)"
    assert "john" not in redact_soql(q)
    assert soql_hash(q) == soql_hash("FIND {other} IN EMAIL FIELDS RETURNING Lead(Id)")


@pytest.mark.parametrize(("value", "text"), [(1e16, "10000000000000000"), (1e-7, "0.0000001"), (1.5, "1.5")])
def test_float_never_uses_exponent(value: float, text: str) -> None:
    assert quote(value) == text


def test_build_url_allows_scheme_in_query() -> None:
    url = build_url(INSTANCE, "68.0", "query?q=SELECT Id FROM X WHERE Url__c = 'https://a'")
    assert url.startswith(f"{INSTANCE}/services/data/v68.0/query?")
    with pytest.raises(ValueError):
        build_url(INSTANCE, "68.0", "https://evil.example.com/x")


def test_in_memory_store_lock_is_per_key(h: Harness) -> None:
    store = h.new_store()
    if h.is_async:

        async def go() -> None:
            async with store.lock("a"), store.lock("b"):  # different key: no deadlock
                pass

        h.run(go())
    else:
        with store.lock("a"), store.lock("b"):
            pass


# --- coverage for the new paths ------------------------------------------------------------------


def test_gather_settled_sequential_and_cancellation() -> None:
    import asyncio

    from even_simpler_salesforce._async import _compat as acompat
    from even_simpler_salesforce._sync import _compat as scompat

    async def ok() -> int:
        return 1

    async def bad() -> int:
        raise ValueError("x")

    out = asyncio.run(acompat.gather_settled(1, [ok, bad]))
    assert out[0] == 1
    assert isinstance(out[1], ValueError)

    async def cancelled() -> int:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(acompat.gather_settled(4, [ok, cancelled]))

    def sok() -> int:
        return 2

    def sbad() -> int:
        raise KeyError("y")

    s = scompat.gather_settled(1, [sok, sbad])
    assert s[0] == 2
    assert isinstance(s[1], KeyError)
    p = scompat.gather_settled(3, [sok, sbad, sok])
    assert p[0] == 2
    assert isinstance(p[1], KeyError)


def test_revoke_with_explicit_auth(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(f"{INSTANCE}/services/oauth2/revoke").respond(200)
    store = h.new_store()
    flow = esf.WebServerFlow(domain=INSTANCE, client_id="c", redirect_uri="https://a/cb", token_key="user:7")
    h.seed(store, "user:7", make_token("AT-u7", "RT-u7"))
    sf = h.client(esf.StaticToken("00D!x", INSTANCE), store=store)
    h.run(sf.auth.revoke(auth=flow))
    assert route.calls.last.request.content == b"token=RT-u7"
    assert h.load(store, "user:7") is None
    h.run(sf.auth.revoke("user:8", auth=flow))  # nothing stored under that key: no call
    assert route.call_count == 1


def test_unexpected_exception_in_chunk_propagates(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()

    def boom(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("bug")

    mock.post(f"{DATA}/composite/sobjects").mock(side_effect=boom)
    with pytest.raises(RuntimeError):
        h.run(sf.collections.create([{"Name": i} for i in range(400)], sobject="Account"))


def test_poller_refused_after_close(h: Harness) -> None:
    sf = h.client()
    h.run(sf.aclose() if h.is_async else sf.close())
    with pytest.raises(RuntimeError, match="closed"):
        sf.start_limits_poller()


def test_html_5xx_chunk_marked_unknown(h: Harness, mock: respx.MockRouter) -> None:
    """A proxy's HTML 502 may have reached Salesforce: the chunk's outcome is unknown."""
    route = mock.post(f"{DATA}/composite/sobjects")
    ok = httpx.Response(200, json=[{"id": "001000000000001AAA", "success": True, "errors": []}] * 200)
    html = httpx.Response(502, text="<html>bad gateway</html>", headers={"content-type": "text/html"})
    route.side_effect = [html, ok]
    sf = h.client(retry=esf.RetryPolicy(max_attempts=1))
    with pytest.raises(esf.PartialFailure) as ei:
        h.run(sf.collections.create([{"Name": i} for i in range(400)], sobject="Account", concurrency=1))
    assert ei.value.results is not None
    assert ei.value.results[0].errors[0].extended["outcome"] == "unknown"


def test_debug_attempt_event_masks_external_id(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(f"{DATA}/sobjects/Lead/Ext__c/secret-value").respond(json={"Id": LEAD})
    sf = h.client(recorder=rec)
    sf._transport.debug = 2
    h.run(sf.sobject("Lead").get_by_external_id("Ext__c", "secret-value"))
    assert "secret-value" not in repr([e.fields for e in rec.events])
