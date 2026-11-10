"""Values in SOQL methods (``soql_values``), ``flatten`` and explain parsing (§9.2)."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core.records import flatten, parse_query_plans
from even_simpler_salesforce.core.soql import soql, soql_values

from ..fixtures import load_fixture

# --- soql_values -----------------------------------------------------------------------------


def test_no_values_sends_template_unchanged() -> None:
    q = "SELECT Id FROM Lead WHERE Name LIKE '{literal}' AND x = '{{'"
    assert soql_values(q, ()) == q


@pytest.mark.parametrize(
    "values",
    [("a'b@x.com",), (["Open", "Working"],), (dt.date(2024, 1, 2),), (None,), (True,), (3.5,)],
)
def test_same_quoting_as_soql(values: tuple[Any, ...]) -> None:
    template = "SELECT Id FROM Lead WHERE X = {}"
    assert soql_values(template, values) == soql(template, *values)


@pytest.mark.parametrize("template", ["X = {name}", "X LIKE {:like}", "X = {0}", "X = {!r}"])
def test_only_positional_placeholders(template: str) -> None:
    with pytest.raises(ValueError, match="esf.soql"):
        soql_values("SELECT Id FROM Lead WHERE " + template, ("v",))


@pytest.mark.parametrize(
    ("template", "values"),
    [
        ("SELECT Id FROM Lead WHERE A = {} AND B = {}", ("a",)),
        ("SELECT Id FROM Lead WHERE A = {}", ("a", "b")),
        ("SELECT Id FROM Lead", ("unused",)),
    ],
)
def test_placeholder_count_must_match(template: str, values: tuple[Any, ...]) -> None:
    with pytest.raises(ValueError, match="placeholder"):
        soql_values(template, values)


# --- flatten ---------------------------------------------------------------------------------


def test_flatten_lookups_nulls_children() -> None:
    row = {
        "attributes": {"type": "Account", "url": "/x"},
        "Id": "001",
        "Owner": {
            "attributes": {"type": "User"},
            "Name": "Ann",
            "Manager": {"attributes": {}, "Email": "m@x"},
        },
        "Parent": None,
        "Contacts": {
            "totalSize": 2,
            "done": True,
            "records": [
                {"attributes": {"type": "Contact"}, "LastName": "A", "Account": {"Name": "x"}},
                {"attributes": {"type": "Contact"}, "LastName": "B", "Account": None},
            ],
        },
        "Empty": {"totalSize": 0, "done": True, "records": []},
    }
    assert flatten(row) == {
        "Id": "001",
        "Owner.Name": "Ann",
        "Owner.Manager.Email": "m@x",
        "Parent": None,
        "Contacts": [{"LastName": "A", "Account.Name": "x"}, {"LastName": "B", "Account": None}],
        "Empty": [],
    }


def test_flatten_compound_field_without_records_key_is_flattened() -> None:
    row = {"BillingAddress": {"city": "Pune", "records": "not-a-list"}}
    assert flatten(row) == {"BillingAddress.city": "Pune", "BillingAddress.records": "not-a-list"}


def test_flatten_does_not_mutate_and_rejects_models() -> None:
    row = {"attributes": {"type": "Lead"}, "Id": "1"}
    flatten(row)
    assert "attributes" in row

    @dataclass
    class Lead:
        Id: str

    with pytest.raises(TypeError, match="plain dict"):
        flatten(Lead("1"))  # type: ignore[arg-type]
    assert esf.flatten is flatten
    with pytest.raises(TypeError, match="plain dict row, not str"):
        flatten("Id")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "rows",
    [
        [{"Id": "1"}],
        ({"Id": "1"},),
        esf.QueryResult(records=[{"Id": "1"}], total_size=1, done=True),
        iter([{"Id": "1"}]),
    ],
    ids=["list", "tuple", "QueryResult", "iterator"],
)
def test_flatten_many_rows_points_to_comprehension(rows: object) -> None:
    with pytest.raises(TypeError, match=re.escape("[esf.flatten(r) for r in page]")):
        flatten(rows)  # type: ignore[arg-type]


# --- explain ---------------------------------------------------------------------------------


def test_parse_query_plans_keeps_order_and_fields() -> None:
    plans = parse_query_plans(load_fixture("query/explain.json"))
    assert [p.leading_operation for p in plans] == ["Index", "TableScan"]
    first, second = plans
    assert first == esf.QueryPlan("Index", 0.0, 1, 120000, "Lead", ("Email",), ())
    assert second.relative_cost == 2.8
    assert second.notes == (
        esf.QueryPlanNote(
            "Not considering filter for optimization because unindexed", ("IsDeleted",), "Lead"
        ),
    )


@pytest.mark.parametrize("body", [None, [], {"plans": "x"}, {"plans": [1]}])
def test_parse_query_plans_rejects_bad_shapes(body: Any) -> None:
    with pytest.raises(esf.UnexpectedResponse):
        parse_query_plans(body)


def test_parse_query_plans_defaults_and_odd_notes() -> None:
    (plan,) = parse_query_plans({"plans": [{"notes": ["junk", {"description": None}]}]})
    assert plan == esf.QueryPlan("", 0.0, 0, 0, "", (), (esf.QueryPlanNote("", (), None),))
