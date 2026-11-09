from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import gzip
import json
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core import jsonlib
from even_simpler_salesforce.core.http import (
    GZIP_THRESHOLD,
    RawResponse,
    build_url,
    check_record_id,
    check_sobject,
    gzip_body,
    json_body,
    path_template,
    url_segment,
)
from even_simpler_salesforce.core.limits import LimitInfo, parse_limit_info
from even_simpler_salesforce.core.records import (
    COLLECTION_READ_CHUNK,
    COLLECTION_WRITE_CHUNK,
    apply_model,
    chunked,
    duplicate_rule_header,
    format_sf_datetime,
    parse_record_results,
    parse_upsert,
    with_type,
)

from ..fixtures import load_fixture

INST = "https://acme.my.salesforce.com"

# --- build_url --------------------------------------------------------------------------------


def test_build_url_relative_and_absolute() -> None:
    assert build_url(INST, "68.0", "sobjects/Lead") == f"{INST}/services/data/v68.0/sobjects/Lead"
    assert build_url(INST + "/", "68.0", "query") == f"{INST}/services/data/v68.0/query"
    assert build_url(INST, "68.0", "/services/apexrest/orders") == f"{INST}/services/apexrest/orders"
    assert build_url(INST, "68.0", "/services/oauth2/userinfo") == f"{INST}/services/oauth2/userinfo"
    assert build_url(INST, "68.0", "query?q=a/../b") == f"{INST}/services/data/v68.0/query?q=a/../b"


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example.com/services/data",
        "http://x/services/",
        "//evil.example.com/services/data",
        "/services/data/../../etc/passwd",
        "sobjects/../../oauth2/token",
        "./query",
        "/services/data/v68.0/./query",
        "/etc/passwd",
        "/servicesX/data",
        "/",
    ],
)
def test_build_url_rejects(path: str) -> None:
    with pytest.raises(ValueError):
        build_url(INST, "68.0", path)


# --- path_template ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("sobjects/Lead/00Qxx0000000001AAA", "/sobjects/Lead/:id"),
        ("/services/data/v68.0/sobjects/Lead/00Qxx0000000001AAA", "/sobjects/Lead/:id"),
        ("sobjects/Lead/00Qxx0000000001", "/sobjects/Lead/:id"),
        ("sobjects/Lead/describe", "/sobjects/Lead/describe"),
        ("sobjects/Account/AccountNumber", "/sobjects/Account/AccountNumber"),  # no digit → not an id
        ("/services/data/v68.0/query/01gxx0000000001AAA-2000", "/query/:locator"),
        ("query?q=SELECT+Id", "/query"),
        ("00Qxx0000000001AAA", "/:id"),
        ("/services/apexrest/x/00Qxx0000000001AAA/y", "/services/apexrest/x/:id/y"),
    ],
)
def test_path_template(path: str, expected: str) -> None:
    assert path_template(path) == expected


# --- validators / encoders -------------------------------------------------------------------------


def test_check_sobject_and_id() -> None:
    assert check_sobject("Custom__c") == "Custom__c"
    for bad in ("", "1A", "Lead/../x", "Le ad", None):
        with pytest.raises(ValueError):
            check_sobject(bad)  # type: ignore[arg-type]
    assert check_record_id("00Qxx0000000001") == "00Qxx0000000001"
    assert check_record_id("00Qxx0000000001AAA") == "00Qxx0000000001AAA"
    for bad in ("", "short", "00Qxx0000000001AA", "00Qxx000000000/AAA", None):
        with pytest.raises(ValueError):
            check_record_id(bad)  # type: ignore[arg-type]


def test_url_segment_json_gzip() -> None:
    assert url_segment("a/b c?") == "a%2Fb%20c%3F"
    content, ctype = json_body({"a": 1})
    assert ctype == "application/json"
    assert json.loads(content) == {"a": 1}
    big = b"x" * (GZIP_THRESHOLD + 1)
    assert gzip.decompress(gzip_body(big)) == big


def test_raw_response() -> None:
    r = RawResponse(
        200, {"content-type": "Application/JSON; charset=UTF-8", "sforce-request-id": "r1"}, b'{"a":1}'
    )
    assert r.content_type == "application/json"
    assert r.is_json
    assert r.json() == {"a": 1}
    assert r.json() is r.json()  # cached
    assert r.request_id == "r1"
    assert RawResponse(200, {"x-request-id": "r2"}, b"").request_id == "r2"
    assert RawResponse(200, {"x-b3-traceid": "r3"}, b"").request_id == "r3"
    assert RawResponse(200, {}, b"").request_id is None
    assert RawResponse(200, {}, b"").json() is None
    assert RawResponse(200, {"content-type": "application/vnd.x+json"}, b"1").is_json
    assert not RawResponse(200, {"content-type": "text/csv"}, b"1").is_json
    with pytest.raises(ValueError):
        RawResponse(200, {}, b"{nope").json()
    custom = RawResponse(200, {}, b"[1]", loads=lambda b: ("custom", b))
    assert custom.json() == ("custom", b"[1]")


# --- jsonlib --------------------------------------------------------------------------------------


def test_jsonlib_roundtrip_and_default() -> None:
    obj = {
        "d": dt.date(2024, 1, 2),
        "t": dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc),
        "n": decimal.Decimal("1.50"),
        "s": {1},
        "f": frozenset({2}),
        "tu": (3, 4),
        "u": "ü",
    }
    for dumps in (jsonlib.dumps, jsonlib._std_dumps):
        out = json.loads(dumps(obj))
        assert out["d"] == "2024-01-02"
        assert out["t"].startswith("2024-01-02T03:04:05")
        assert out["n"] in ("1.50", 1.5)
        assert out["s"] == [1]
        assert out["f"] == [2]
        assert out["tu"] == [3, 4]
        assert out["u"] == "ü"
    assert jsonlib.loads(b'{"a": [1]}') == {"a": [1]}
    assert jsonlib.BACKEND in ("orjson", "json")
    with pytest.raises(TypeError):
        jsonlib._std_dumps({"x": object()})
    with pytest.raises(TypeError):
        jsonlib.dumps({"x": object()})


# --- limits header ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("api-usage=10018/100000", LimitInfo(10018, 100000)),
        ("api-usage=10018/100000; api-bursts=1/750", LimitInfo(10018, 100000, 1, 750)),
        (" API-Usage = 5 / 10 ;api-bursts=2/3 ", LimitInfo(5, 10, 2, 3)),
        ("api-bursts=1/750", LimitInfo(None, None, 1, 750)),
        ("per-app-api-usage=1/2; api-usage=3/4", LimitInfo(3, 4)),
        ("garbage; api-usage=x/y", None),
        ("", None),
        (None, None),
        ("per-app-api-usage=17/250(appName=sample)", None),
    ],
)
def test_parse_limit_info(value: str | None, expected: LimitInfo | None) -> None:
    assert parse_limit_info(value) == expected


# --- records ---------------------------------------------------------------------------------------


def test_chunk_sizes_and_chunked() -> None:
    assert (COLLECTION_WRITE_CHUNK, COLLECTION_READ_CHUNK) == (200, 2000)
    assert [list(c) for c in chunked([1, 2, 3, 4, 5], 2)] == [[1, 2], [3, 4], [5]]
    assert list(chunked([], 3)) == []


def test_with_type() -> None:
    row = {"Name": "x"}
    out = with_type(row, "Account")
    assert out == {"Name": "x", "attributes": {"type": "Account"}}
    assert "attributes" not in row  # copied
    assert with_type({"attributes": {"type": "Lead"}}, "Account")["attributes"]["type"] == "Lead"
    assert with_type({"attributes": {"type": "Lead"}}, None)["attributes"]["type"] == "Lead"
    with pytest.raises(ValueError):
        with_type({"Name": "x"}, None)
    with pytest.raises(ValueError):
        with_type({"attributes": "junk"}, None)


def test_parse_record_results() -> None:
    res = parse_record_results(load_fixture("collections/mixed.json"), 2)
    assert res[0] == esf.RecordResult(ok=True, id="001xx0000000001AAA", created=None, errors=())
    assert res[1].ok is False
    assert res[1].errors[0].error_code == "REQUIRED_FIELD_MISSING"
    assert res[1].errors[0].fields == ("Name",)
    assert parse_record_results([{"success": True, "id": "1", "created": True}], 1)[0].created is True
    with pytest.raises(esf.UnexpectedResponse):
        parse_record_results([{"success": True}], 2)
    with pytest.raises(esf.UnexpectedResponse):
        parse_record_results({"not": "a list"}, 1)
    with pytest.raises(esf.UnexpectedResponse):
        parse_record_results(["x"], 1)


def test_parse_upsert() -> None:
    assert parse_upsert(201, {"id": "a", "created": True}) == esf.UpsertResult("a", True)
    assert parse_upsert(200, {"id": "a", "created": False}) == esf.UpsertResult("a", False)
    assert parse_upsert(201, {"id": "a"}) == esf.UpsertResult("a", True)
    assert parse_upsert(204, None) == esf.UpsertResult(None, False)


def test_duplicate_rule_header_and_dates() -> None:
    assert duplicate_rule_header(True) == "allowSave=true; includeRecordDetails=false; runAsCurrentUser=false"
    assert (
        duplicate_rule_header(False, True, True)
        == "allowSave=false; includeRecordDetails=true; runAsCurrentUser=true"
    )
    tz = dt.timezone(dt.timedelta(hours=-5))
    assert format_sf_datetime(dt.datetime(2024, 1, 1, 19, 0, tzinfo=tz)) == "2024-01-02T00:00:00Z"
    with pytest.raises(ValueError):
        format_sf_datetime(dt.datetime(2024, 1, 1))


@dataclasses.dataclass
class LeadDC:
    Id: str
    Name: str


def test_apply_model() -> None:
    rows: list[dict[str, Any]] = [{"attributes": {"type": "Lead"}, "Id": "1", "Name": "a"}]
    assert apply_model(rows, None) == rows
    assert apply_model(rows, LeadDC) == [LeadDC("1", "a")]
    pydantic = pytest.importorskip("pydantic")

    class LeadPD(pydantic.BaseModel):
        Id: str
        Name: str

    out = apply_model(rows, LeadPD)
    assert out[0].Id == "1"
    assert isinstance(out[0], LeadPD)
