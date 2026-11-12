"""Pure helpers for the external-id fallback (V19) and write headers (V24). No network."""

from __future__ import annotations

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core import records as R


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a.json", True),
        ("a.xml", True),
        ("a.html", True),
        ("A.JSON", True),  # observed to work on the org, but the fallback is safe for any case
        ("x.y.json", True),
        ("a.csv", False),
        ("a.htm", False),
        ("a.json.x", False),
        ("a.jsonx", False),
        ("json", False),
        ("a@b.com", False),
        ("", False),
    ],
)
def test_needs_external_id_fallback(value: str, expected: bool) -> None:
    assert R.needs_external_id_fallback(value) is expected


def test_external_id_lookup_soql_quotes_the_value() -> None:
    assert R.external_id_lookup_soql("Lead", "Ext__c", "it's.json") == (
        "SELECT Id FROM Lead WHERE Ext__c = 'it\\'s.json'"
    )
    with pytest.raises(ValueError):
        R.external_id_lookup_soql("Lead", "Ext__c; DROP", "x.json")


def test_external_id_in_data() -> None:
    assert R.external_id_in_data("Ext__c", {"Name": "x"}) is None
    err = R.external_id_in_data("Ext__c", {"ext__C": "x"})
    assert isinstance(err, esf.InvalidField)
    assert err.error_code == "INVALID_FIELD"
    assert err.status == 400
    assert err.message == "The Ext__c field should not be specified in the sobject data."


def test_duplicate_matches() -> None:
    a, b = "00Qxx0000000001AAA", "00Qxx0000000002AAA"
    detail = esf.ErrorDetail(
        error_code="DUPLICATE_EXTERNAL_ID",
        message=f"Ext: Label: more than one record found for external id field: [{a}, {b}]",
    )
    err = R.duplicate_matches(detail, "68.0", "Lead")
    assert isinstance(err, esf.MultipleMatches)
    assert err.status == 300
    assert err.matches == (
        f"/services/data/v68.0/sobjects/Lead/{a}",
        f"/services/data/v68.0/sobjects/Lead/{b}",
    )
    vague = R.duplicate_matches(
        esf.ErrorDetail(error_code="DUPLICATE_EXTERNAL_ID", message="no ids"), "68.0", "Lead"
    )
    assert vague is not None
    assert vague.matches == ()
    assert "several" in vague.message
    assert R.duplicate_matches(esf.ErrorDetail(error_code="OTHER", message="x"), "68.0", "Lead") is None


def test_query_ids() -> None:
    body = {"records": [{"Id": "00Qxx0000000001AAA"}, {"Id": None}, "junk", {"Name": "x"}]}
    assert R.query_ids(body) == ["00Qxx0000000001AAA"]
    for bad in (None, [], {"records": "x"}):
        with pytest.raises(esf.UnexpectedResponse):
            R.query_ids(bad)


def test_write_headers() -> None:
    assert R.write_headers(None, None) == {}
    assert R.write_headers(True, False) == {
        "Sforce-Auto-Assign": "TRUE",
        "Sforce-Duplicate-Rule-Header": "allowSave=false; includeRecordDetails=false; runAsCurrentUser=false",
    }
    assert R.write_headers(False, None) == {"Sforce-Auto-Assign": "FALSE"}
    assert R.write_headers("01Qxx0000000001AAA", None) == {"Sforce-Auto-Assign": "01Qxx0000000001AAA"}
    with pytest.raises(ValueError):
        R.write_headers("not an id", None)
