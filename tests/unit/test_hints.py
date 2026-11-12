"""``core/hints.py``: hints for common, fixable errors (spec §8.5). Pure; no network."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core import hints as H

from ..fixtures import load_fixture

LEAD = load_fixture("sobject/describe_lead.json")  # recorded


def lookup(**objects: Mapping[str, Any]) -> H.DescribeLookup:
    return lambda name: objects.get(name)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "\nSELECT Emial FROM Lead\n       ^\nERROR at Row:1:Column:8\n"
            "No such column 'Emial' on entity 'Lead'.",
            ("Emial", "Lead"),
        ),
        ("No such column 'Emial' on sobject of type Lead", ("Emial", "Lead")),
        ("No such column 'Owner.Emial' on entity 'User'.", ("Owner.Emial", "User")),
        ("Unable to create/update fields: Name.", None),
    ],
)
def test_missing_column(message: str, expected: tuple[str, str] | None) -> None:
    assert H.missing_column(message) == expected


def test_field_suggestions_are_case_insensitive_and_skip_exact_matches() -> None:
    assert H.field_suggestions("Emial", LEAD) == ["Email"]
    assert H.field_suggestions("lastname", LEAD)[0] == "LastName"  # recorded describe: FirstName too
    assert H.field_suggestions("Email", LEAD) == []  # the field exists: nothing to suggest
    assert H.field_suggestions("Xyzzy", LEAD) == []
    assert H.field_suggestions("Email", {"fields": "odd"}) == []
    assert H.field_suggestions("Email", {"fields": [{"name": 3}, "x", {"label": "Email"}]}) == []


@pytest.mark.parametrize(
    ("soql", "odd"),
    [
        ("SELECT Id FROM Lead WHERE LastName = 'O'Brien'", True),
        ("SELECT Id FROM Lead WHERE LastName = 'O\\'Brien'", False),
        ("SELECT Id FROM Lead WHERE LastName = 'back\\\\' AND Company = 'x'", False),
        ("SELECT Id FROM Lead", False),
    ],
)
def test_unbalanced_quote(soql: str, odd: bool) -> None:
    """Escapes from *Quoted String Escape Sequences* (SOQL and SOSL Reference):
    https://developer.salesforce.com/docs/atlas.en-us.soql_sosl.meta/soql_sosl/sforce_api_calls_soql_select_quotedstringescapes.htm
    """
    assert H.unbalanced_quote(soql) is odd


def invalid_field(message: str, sobject: str | None = "Lead") -> esf.InvalidField:
    return esf.InvalidField(message, error_code="INVALID_FIELD", status=400, sobject=sobject)


def test_invalid_field_suggests_from_cached_describe() -> None:
    err = invalid_field("No such column 'Emial' on entity 'Lead'.")
    assert H.hint_for(err, describe=lookup(Lead=LEAD)) == "did you mean 'Email'?"


def test_invalid_field_several_suggestions() -> None:
    desc = {"fields": [{"name": "Score__c"}, {"name": "Scores__c"}]}
    err = invalid_field("No such column 'Score_c' on sobject of type Lead")
    assert H.hint_for(err, describe=lookup(Lead=desc)) == "did you mean 'Score__c' or 'Scores__c'?"


def test_invalid_field_uses_the_entity_in_the_message() -> None:
    # A column missing on a parent (User) is not matched against the call's sObject.
    err = invalid_field("No such column 'Emial' on entity 'User'.", sobject="Lead")
    assert H.hint_for(err, describe=lookup(Lead=LEAD)) is None
    # The caller's spelling of the same object is tried too.
    err = invalid_field("No such column 'Emial' on entity 'Lead'.", sobject="lead")
    assert H.hint_for(err, describe=lookup(lead=LEAD)) == "did you mean 'Email'?"
    # Relationship paths: only the last part is compared.
    err = invalid_field("No such column 'Owner.Emial' on entity 'Lead'.")
    assert H.hint_for(err, describe=lookup(Lead=LEAD)) == "did you mean 'Email'?"


def test_invalid_field_without_cached_describe_or_match_has_no_hint() -> None:
    err = invalid_field("No such column 'Emial' on entity 'Lead'.")
    assert H.hint_for(err) is None
    assert H.hint_for(err, describe=lookup()) is None
    assert (
        H.hint_for(invalid_field("No such column 'Xyzzy' on entity 'Lead'."), describe=lookup(Lead=LEAD))
        is None
    )
    assert (
        H.hint_for(invalid_field("Unable to create/update fields: Name."), describe=lookup(Lead=LEAD)) is None
    )
    assert (
        H.hint_for(invalid_field("No such column 'X' on entity 'Lead'.", sobject=None), describe=lookup())
        is None
    )


def test_session_hint_only_after_a_refresh() -> None:
    err = esf.SessionExpired("Session expired or invalid", error_code="INVALID_SESSION_ID", status=401)
    assert H.hint_for(err) is None
    hint = H.hint_for(err, refreshed=True)
    assert hint is not None
    assert "'api' OAuth scope" in hint
    local = esf.SessionExpired("StaticToken expired")  # no HTTP status: raised by the client
    assert H.hint_for(local, refreshed=True) is None
    soap = esf.SessionExpired("Invalid Session ID", error_code="INVALID_SESSION_ID", status=500)
    assert H.hint_for(soap, refreshed=True) == hint  # a SOAP fault arrives as HTTP 500


def test_api_disabled_and_request_limit_hints() -> None:
    disabled = esf.Forbidden(
        "The REST API is not enabled for this Organization.", error_code="API_DISABLED_FOR_ORG"
    )
    assert "'API Enabled' permission" in (H.hint_for(disabled) or "")
    limit = esf.RequestLimitExceeded("TotalRequests Limit exceeded.", error_code="REQUEST_LIMIT_EXCEEDED")
    hint = H.hint_for(limit) or ""
    assert "sf.usage.snapshot()" in hint
    assert H.USAGE_PAGE in hint


def test_malformed_query_hint_needs_an_unbalanced_quote() -> None:
    soql = "SELECT Id FROM Lead WHERE LastName = 'O'Brien'"
    err = esf.MalformedQuery("unexpected token: Brien", error_code="MALFORMED_QUERY", status=400)
    hint = H.hint_for(err, soql=soql)
    assert hint is not None
    assert "sf.query(" in hint
    assert "Brien" not in hint  # hints never quote record data
    assert H.hint_for(err, soql="SELECT Id FROM Lead WHERE LastName = 'Curie'") is None
    assert H.hint_for(err) is None


def test_other_errors_have_no_hint() -> None:
    assert H.hint_for(esf.NotFound("gone", error_code="NOT_FOUND")) is None


def test_add_hint_keeps_an_existing_hint_and_never_raises() -> None:
    err = invalid_field("No such column 'Emial' on entity 'Lead'.")
    err.hint = "set by the caller"
    H.add_hint(err, describe=lookup(Lead=LEAD))
    assert err.hint == "set by the caller"

    def broken(name: str) -> Mapping[str, Any] | None:
        raise RuntimeError("cache down")

    err = invalid_field("No such column 'Emial' on entity 'Lead'.")
    H.add_hint(err, describe=broken)
    assert err.hint is None
    H.add_hint(err, describe=lookup(Lead=LEAD))
    assert err.hint == "did you mean 'Email'?"
