"""Snapshot of the public result types: field names, order, types, defaults, frozen-ness.

A change to any of these is a SemVer-relevant change and must show up in review, so the
snapshot is a literal in this file (no generated snapshot files).

simple-salesforce regressions this guards against (behaviour only, no code was read or
copied): #469 (query results came back nested, list-of-lists, for some calls) and #557
(result shape changed between releases without notice).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any

import pytest

import even_simpler_salesforce as esf


class _Required:
    def __repr__(self) -> str:
        return "REQUIRED"


REQUIRED: Any = _Required()


@dataclasses.dataclass(frozen=True)
class FACTORY:  # noqa: N801 - reads like a marker in the snapshot
    value: Any


SNAPSHOT: dict[str, list[tuple[str, str, Any]]] = {
    "QueryResult": [
        ("records", "list[T]", REQUIRED),
        ("total_size", "int", REQUIRED),
        ("done", "bool", REQUIRED),
        ("cursor", "Cursor | None", None),
    ],
    "BatchResult": [
        ("results", "list[T]", REQUIRED),
        ("operation", "str | None", None),
        ("sobject", "str | None", None),
    ],
    "RecordResult": [
        ("ok", "bool", REQUIRED),
        ("id", "str | None", None),
        ("created", "bool | None", None),
        ("errors", "tuple[ErrorDetail, ...]", ()),
    ],
    "UpsertResult": [
        ("id", "str | None", REQUIRED),
        ("created", "bool", REQUIRED),
    ],
    "UsageSnapshot": [
        ("api_used", "int | None", REQUIRED),
        ("api_max", "int | None", REQUIRED),
        ("api_fraction", "float | None", REQUIRED),
        ("as_of", "datetime | None", REQUIRED),
        ("calls_by_operation", "Mapping[str, int]", REQUIRED),
        ("calls_by_sobject", "Mapping[str, int]", REQUIRED),
        ("calls_by_caller", "Mapping[str, int]", REQUIRED),
        ("errors_by_code", "Mapping[str, int]", REQUIRED),
        ("retries", "int", REQUIRED),
        ("refreshes", "int", REQUIRED),
        ("projected_exhaustion", "datetime | None", REQUIRED),
        ("api_bursts_used", "int | None", None),
        ("api_bursts_max", "int | None", None),
    ],
    "Limit": [
        ("max", "int", REQUIRED),
        ("remaining", "int", REQUIRED),
        ("details", "Mapping[str, Any]", FACTORY({})),
    ],
    "Token": [
        ("access_token", "str", REQUIRED),
        ("instance_url", "str", REQUIRED),
        ("issued_at", "datetime", REQUIRED),
        ("refresh_token", "str | None", None),
        ("id_url", "str | None", None),
        ("org_id", "str | None", None),
        ("user_id", "str | None", None),
        ("scope", "frozenset[str]", frozenset()),
        ("signature", "str | None", None),
        ("expires_at", "datetime | None", None),
        ("version", "int", 0),
    ],
    "ErrorDetail": [
        ("error_code", "str | None", REQUIRED),
        ("message", "str", REQUIRED),
        ("fields", "tuple[str, ...]", ()),
        ("extended", "Mapping[str, Any]", FACTORY({})),
    ],
}


def shape(cls: type[Any]) -> list[tuple[str, str, Any]]:
    out: list[tuple[str, str, Any]] = []
    for f in dataclasses.fields(cls):
        if f.default is not dataclasses.MISSING:
            default: Any = f.default
        elif f.default_factory is not dataclasses.MISSING:
            default = FACTORY(f.default_factory())
        else:
            default = REQUIRED
        out.append((f.name, str(f.type), default))
    return out


@pytest.mark.parametrize("name", sorted(SNAPSHOT))
def test_fields_match_snapshot(name: str) -> None:
    cls = getattr(esf, name)
    assert name in esf.__all__
    assert dataclasses.is_dataclass(cls)
    assert shape(cls) == SNAPSHOT[name]


@pytest.mark.parametrize("name", sorted(SNAPSHOT))
def test_frozen_and_slotted(name: str) -> None:
    cls = getattr(esf, name)
    assert cls.__dataclass_params__.frozen is True
    assert "__slots__" in cls.__dict__  # no per-instance __dict__: attributes can't be added
    first = dataclasses.fields(cls)[0].name
    obj = SAMPLES[name]()
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(obj, first, None)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
SAMPLES: dict[str, Any] = {
    "QueryResult": lambda: esf.QueryResult([], 0, True),
    "BatchResult": lambda: esf.BatchResult([]),
    "RecordResult": lambda: esf.RecordResult(True),
    "UpsertResult": lambda: esf.UpsertResult("001xx0000000001AAA", True),
    "UsageSnapshot": lambda: esf.UsageSnapshot(None, None, None, None, {}, {}, {}, {}, 0, 0, None),
    "Limit": lambda: esf.Limit(10, 5),
    "Token": lambda: esf.Token("AT", "https://x.my.salesforce.com", NOW),
    "ErrorDetail": lambda: esf.ErrorDetail(None, "m"),
}


def test_samples_cover_snapshot() -> None:
    assert set(SAMPLES) == set(SNAPSHOT)


def test_mutable_defaults_are_not_shared() -> None:
    assert esf.Limit(1, 1).details is not esf.Limit(1, 1).details
    assert esf.ErrorDetail(None, "m").extended is not esf.ErrorDetail(None, "m").extended


# --- QueryResult from a parsed page is a flat list of dicts (#469) --------------------------------


def test_parsed_page_records_are_a_flat_list_of_dicts() -> None:
    body = {
        "totalSize": 2,
        "done": True,
        "records": [
            {
                "attributes": {"type": "Account", "url": "/x"},
                "Id": "001xx0000000001AAA",
                "Contacts": {"totalSize": 1, "done": True, "records": [{"Id": "003xx0000000001AAA"}]},
            },
            {"attributes": {"type": "Account", "url": "/y"}, "Id": "001xx0000000002AAA", "Contacts": None},
        ],
    }
    sf = esf.Salesforce(esf.StaticToken("AT", "https://x.my.salesforce.com"))
    try:
        page = sf._page(body, None)
    finally:
        sf.close()
    assert isinstance(page, esf.QueryResult)
    assert type(page.records) is list
    assert all(type(r) is dict for r in page.records)
    assert [r["Id"] for r in page.records] == ["001xx0000000001AAA", "001xx0000000002AAA"]
    assert (page.total_size, page.done, page.cursor) == (2, True, None)
    assert list(page) == page.records
    assert len(page) == 2


# --- BatchResult indexing / iteration -------------------------------------------------------------


def test_batch_result_sequence_semantics() -> None:
    items = [esf.RecordResult(True, "a"), esf.RecordResult(False), esf.RecordResult(True, "c")]
    b = esf.BatchResult(items, operation="collections.create", sobject="Account")
    assert len(b) == 3
    assert list(b) == items
    assert [b[i] for i in range(3)] == items
    assert b[-1] is items[-1]
    assert b.results is items
    with pytest.raises(IndexError):
        b[3]
    assert b.succeeded == [items[0], items[2]]
    assert b.failed == [items[1]]
    assert list(iter(b)) == list(iter(b))  # re-iterable, not a one-shot generator
