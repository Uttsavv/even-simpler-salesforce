"""``sf.instance_url``: read-only, the current token's instance (§20: replaces sf_instance/base_url)."""

from __future__ import annotations

import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, INSTANCE, Harness


def test_none_before_first_call_then_token_instance(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/limits").respond(json={})
    sf = h.client()
    assert sf.instance_url is None  # no token in memory yet, and no I/O to find one
    h.run(sf.limits())
    assert sf.instance_url == INSTANCE


def test_static_token_and_override(h: Harness) -> None:
    sf = h.client(esf.StaticToken("00D!x", INSTANCE + "/"))
    assert sf.instance_url is None
    h.run(sf.auth.token())
    assert sf.instance_url == INSTANCE
    other = h.client(instance_url="https://override.my.salesforce.com/")
    assert other.instance_url == "https://override.my.salesforce.com"
    assert other.tagged(caller="x").instance_url == "https://override.my.salesforce.com"
