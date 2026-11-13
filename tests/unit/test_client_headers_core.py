"""Pure helpers for the client-wide headers (D30, ADR 0012, ``big-bang.md`` §10.2, §7.1 step 7).

Sources:
- *Call Options Header*, *MRU Header* and *Warning Header* in the REST API Developer Guide:
  https://developer.salesforce.com/docs/platform/api-rest/guide/headers.html
- Namespace prefix rule (*Register a Namespace for a First-Generation Managed Package*):
  https://developer.salesforce.com/docs/atlas.en-us.packagingGuide.meta/packagingGuide/register_namespace_prefix.htm
"""

from __future__ import annotations

import pytest

from even_simpler_salesforce.core.http import (
    RequestSpec,
    call_options,
    check_namespace,
    merge_call_options,
    mru_header,
    parse_warning,
)


@pytest.mark.parametrize("ns", ["a", "battle", "Acme_Pkg", "a1", "x" * 15, "a_b_c"])
def test_namespace_accepted(ns: str) -> None:
    assert check_namespace(ns) == ns


@pytest.mark.parametrize("ns", ["", "1bad", "_a", "a__b", "x" * 16, "a-b", "a b", "ab;c", None, 5])
def test_namespace_rejected(ns: object) -> None:
    with pytest.raises(ValueError, match="default_namespace"):
        check_namespace(ns)  # type: ignore[arg-type]


def test_call_options_joins_with_semicolon() -> None:
    assert call_options(None, None) is None
    assert call_options("acme", None) == "client=acme"
    assert call_options(None, "battle") == "defaultNamespace=battle"
    assert call_options("acme", "battle") == "client=acme; defaultNamespace=battle"


def test_caller_default_namespace_wins_for_one_call() -> None:
    merged = merge_call_options(
        {"sforce-call-options": "defaultNamespace=other"}, "client=acme; defaultNamespace=battle"
    )
    assert merged == {"Sforce-Call-Options": "client=acme; defaultNamespace=other"}


@pytest.mark.parametrize(
    ("operation", "path"),
    [
        ("sobject.create", "sobjects/Lead/"),
        ("sobject.get", "sobjects/Lead/00Q000000000001AAA"),
        ("sobject.get_by_external_id", "sobjects/Lead/Ext__c/1"),
        ("sobject.update", "sobjects/Lead/00Q000000000001AAA"),
        ("sobject.upsert", "sobjects/Lead/Ext__c/1"),
    ],
)
def test_mru_on_documented_resources(operation: str, path: str) -> None:
    spec = RequestSpec("GET", path, operation=operation)
    assert mru_header(spec, False) == {"Sforce-Mru": "updateMru=false"}
    assert mru_header(spec, True) == {"Sforce-Mru": "updateMru=true"}
    assert mru_header(spec, None) == {}


@pytest.mark.parametrize(
    ("operation", "path"),
    [
        ("query", "query"),
        ("sobject.get_by_external_id", "query"),  # the SOQL fallback for .json/.xml values
        ("sobject.upsert", "composite/sobjects/Lead/Ext__c"),  # the Collections fallback
        ("sobject.delete", "sobjects/Lead/00Q000000000001AAA"),
        ("tooling.sobject.get", "tooling/sobjects/ApexClass/01p000000000001AAA"),
        ("raw", "sobjects/Lead/00Q000000000001AAA"),
        ("collections.create", "composite/sobjects"),
    ],
)
def test_mru_not_sent_elsewhere(operation: str, path: str) -> None:
    assert mru_header(RequestSpec("GET", path, operation=operation), False) == {}


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        (
            '299 - "This API is deprecated and will be removed by Summer \'22."',
            "This API is deprecated and will be removed by Summer '22.",
        ),
        ('299 sfdc "Deprecated" "Wed, 01 Jan 2025 00:00:00 GMT"', "Deprecated"),
        ('299 - "say \\"hi\\""', 'say "hi"'),
        ("299 Deprecated API version", "Deprecated API version"),
        ('299 - "x', "x"),
        ('299 - ""', None),
        ("plain text", "plain text"),
    ],
)
def test_parse_warning(value: str | None, text: str | None) -> None:
    assert parse_warning(value) == text
