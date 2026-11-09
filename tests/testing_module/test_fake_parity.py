"""The fakes expose the same public surface as the real clients, with the same signatures."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import AsyncFakeSalesforce, FakeSalesforce, _state

AUTH = esf.StaticToken("x", "https://acme.my.salesforce.com")
SOQL = "SELECT Id FROM Lead WHERE Email = 'ada@example.com'"


def _params(fn: Any) -> list[tuple[str, str]]:
    return [(p.name, p.kind.name) for p in inspect.signature(fn).parameters.values()]


def _compare(real: Any, fake: Any) -> list[str]:
    problems: list[str] = []
    for name in dir(real):
        if name.startswith("_"):
            continue
        if not hasattr(fake, name):
            problems.append(f"missing {name}")
            continue
        attr = getattr(real, name)
        if inspect.ismethod(attr) and _params(attr) != _params(getattr(fake, name)):
            problems.append(f"signature of {name}")
    return problems


@pytest.mark.parametrize(
    ("real_cls", "fake_cls"), [(esf.Salesforce, FakeSalesforce), (esf.AsyncSalesforce, AsyncFakeSalesforce)]
)
def test_fake_matches_real_surface(real_cls: Any, fake_cls: Any) -> None:
    real, fake = real_cls(AUTH), fake_cls(AUTH)
    assert _compare(real, fake) == []
    assert _compare(real.sobject("Lead"), fake.sobject("Lead")) == []
    assert _compare(real.collections, fake.collections) == []
    assert _compare(real.auth, fake.auth) == []


def test_positional_values_and_single_row_helpers() -> None:
    fake = FakeSalesforce()
    lead = fake.new_id("Lead")
    fake.on_query(SOQL, rows=[{"Id": lead}])
    assert fake.query("SELECT Id FROM Lead WHERE Email = {}", "ada@example.com").records[0]["Id"] == lead
    assert fake.query_one("SELECT Id FROM Lead WHERE Email = {}", "ada@example.com")["Id"] == lead
    assert fake.query_first("SELECT Id FROM Lead WHERE Email = {}", "ada@example.com")["Id"] == lead
    assert fake.count("SELECT Id FROM Lead WHERE Email = {}", "ada@example.com") == 1
    assert [r["Id"] for r in fake.query_iter("SELECT Id FROM Lead WHERE Email = {}", "ada@example.com")] == [
        lead
    ]

    fake.on_query("SELECT Id FROM Lead WHERE Email = 'none'", rows=[])
    with pytest.raises(esf.NotFound):
        fake.query_one("SELECT Id FROM Lead WHERE Email = {}", "none")
    assert fake.query_first("SELECT Id FROM Lead WHERE Email = {}", "none") is None

    fake.on_query("SELECT Id FROM Lead", rows=[{"Id": lead}, {"Id": fake.new_id("Lead")}])
    with pytest.raises(esf.MultipleMatches) as ei:
        fake.query_one("SELECT Id FROM Lead")
    assert lead in ei.value.matches


def test_explain_instance_url_and_with_auth() -> None:
    fake = FakeSalesforce()
    assert fake.explain("SELECT Id FROM Lead") == []
    plan = esf.QueryPlan("Index", 0.1, 1, 100, "Lead")
    fake.set_explain([plan])
    assert fake.explain("SELECT Id FROM Lead WHERE Id = {}", "00Q000000000001AAA") == [plan]
    assert fake.instance_url == "https://example.my.salesforce.com"
    assert FakeSalesforce(AUTH).instance_url == "https://acme.my.salesforce.com"
    cc = esf.ClientCredentials(domain="https://beta.my.salesforce.com", client_id="c", client_secret="s")
    assert fake.with_auth(cc).instance_url == "https://beta.my.salesforce.com"
    child = fake.with_auth(cc, instance_url="https://gamma.my.salesforce.com/")
    assert child.instance_url == "https://gamma.my.salesforce.com"
    assert child.tagged("job").instance_url == "https://gamma.my.salesforce.com"
    with pytest.raises(ValueError, match="https://"):
        fake.with_auth(cc, instance_url="http://gamma.my.salesforce.com")


def test_fake_auth_api() -> None:
    fake = FakeSalesforce(AUTH)
    assert fake.auth.token().instance_url == "https://acme.my.salesforce.com"
    flow = esf.WebServerFlow(
        domain="https://acme.my.salesforce.com",
        client_id="c",
        redirect_uri="https://app.example.com/cb",
        token_key="user:1",
    )
    assert fake.auth.exchange_code(flow, "code").access_token == "fake-access-token"
    fake.auth.revoke("user:1")
    assert fake.auth.userinfo()["user_id"]
    assert fake.auth.introspect() == {"active": True}
    ops = [c.operation for c in fake.calls]
    assert ops == ["oauth.exchange_code", "oauth.revoke", "oauth.userinfo", "oauth.introspect"]


def test_async_fake_query_helpers_and_auth() -> None:
    async def main() -> None:
        async with AsyncFakeSalesforce(AUTH) as fake:
            lead = fake.new_id("Lead")
            fake.on_query(SOQL, rows=[{"Id": lead}])
            q = "SELECT Id FROM Lead WHERE Email = {}"
            assert (await fake.query(q, "ada@example.com")).records[0]["Id"] == lead
            assert (await fake.query_one(q, "ada@example.com"))["Id"] == lead
            assert (await fake.query_first(q, "ada@example.com"))["Id"] == lead
            assert await fake.count(q, "ada@example.com") == 1
            assert [r["Id"] async for r in fake.query_iter(q, "ada@example.com")] == [lead]
            assert await fake.explain("SELECT Id FROM Lead") == []
            assert fake.instance_url == "https://acme.my.salesforce.com"
            assert (await fake.auth.token()).instance_url == "https://acme.my.salesforce.com"
            await fake.auth.revoke()
            assert (await fake.auth.userinfo())["user_id"]
            assert await fake.auth.introspect() == {"active": True}
            flow = esf.WebServerFlow(
                domain="https://acme.my.salesforce.com",
                client_id="c",
                redirect_uri="https://a/cb",
                token_key="k",
            )
            assert (await fake.auth.exchange_code(flow, "code")).instance_url
            child = fake.with_auth(AUTH, instance_url="https://z.my.salesforce.com")
            assert child.instance_url == "https://z.my.salesforce.com"

    asyncio.run(main())


def test_repeated_injected_failure_is_a_fresh_exception() -> None:
    fake = FakeSalesforce()
    fake.fail_next("limits", esf.ServerError("boom"), times=3)
    seen = []
    for _ in range(3):
        with pytest.raises(esf.ServerError) as ei:
            fake.limits()
        seen.append(ei.value)
    assert len({id(e) for e in seen}) == 3


def test_open_locators_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_state, "MAX_OPEN_LOCATORS", 2)
    fake = FakeSalesforce()
    fake.on_query("SELECT Id FROM Lead", rows=[{"Id": fake.new_id("Lead")} for _ in range(300)])
    first = fake.query("SELECT Id FROM Lead", page_size=200)
    for _ in range(2):
        fake.query("SELECT Id FROM Lead", page_size=200)
    assert len(fake._state.locators) == 2
    with pytest.raises(esf.MalformedQuery):
        fake.query_more(first.cursor)  # type: ignore[arg-type]
