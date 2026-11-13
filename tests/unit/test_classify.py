from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core.classify import (
    CODE_MAP,
    TAG_PATTERN,
    classify,
    error_class_for,
    error_from_detail,
    parse_details,
    parse_retry_after,
)
from even_simpler_salesforce.core.http import RawResponse, RequestSpec

from ..fixtures import load_fixture


def resp(
    status: int, body: Any = None, *, headers: dict[str, str] | None = None, text: bytes | None = None
) -> RawResponse:
    h = {"content-type": "application/json;charset=UTF-8"}
    h.update(headers or {})
    content = text if text is not None else (json.dumps(body).encode() if body is not None else b"")
    return RawResponse(status, h, content)


SPEC = RequestSpec("GET", "sobjects/Lead/00Qxx0000000001AAA", operation="sobject.get", sobject="Lead")
DML = RequestSpec(
    "PATCH", "sobjects/Lead/00Qxx0000000001AAA", operation="sobject.update", sobject="Lead", dml=True
)


def rest_error(code: str, message: str = "boom", fields: list[str] | None = None) -> list[dict[str, Any]]:
    return [{"errorCode": code, "message": message, "fields": fields or []}]


# --- every code in CODE_MAP -------------------------------------------------------------------


@pytest.mark.parametrize(("code", "cls"), sorted(CODE_MAP.items()))
def test_every_code_maps(code: str, cls: type[esf.SalesforceError]) -> None:
    status = 401 if code == "INVALID_SESSION_ID" else 400
    exc = classify(resp(status, rest_error(code, "msg", ["F1"])), DML)
    assert type(exc) is cls
    assert exc.error_code == code
    assert exc.message == "msg"
    assert exc.fields == ("F1",)
    assert exc.status == status
    assert exc.operation == "sobject.update"
    assert exc.sobject == "Lead"
    assert exc.method == "PATCH"
    assert exc.path == "/sobjects/Lead/:id"
    assert exc.errors[0].error_code == code
    assert isinstance(exc.raw, bytes)


def test_spec_listed_codes_are_mapped() -> None:
    expected = {
        "MALFORMED_QUERY": esf.MalformedQuery,
        "INVALID_QUERY_LOCATOR": esf.MalformedQuery,
        "INVALID_FIELD": esf.InvalidField,
        "INVALID_FIELD_FOR_INSERT_UPDATE": esf.InvalidField,
        "INVALID_TYPE": esf.InvalidType,
        "NOT_FOUND": esf.NotFound,
        "ENTITY_IS_DELETED": esf.NotFound,
        "INSUFFICIENT_ACCESS_OR_READONLY": esf.Forbidden,
        "API_DISABLED_FOR_ORG": esf.Forbidden,
        "FIELD_CUSTOM_VALIDATION_EXCEPTION": esf.ValidationRuleFailed,
        "REQUIRED_FIELD_MISSING": esf.RequiredFieldMissing,
        "DUPLICATES_DETECTED": esf.DuplicateDetected,
        "INVALID_CROSS_REFERENCE_KEY": esf.InvalidReference,
        "FIELD_INTEGRITY_EXCEPTION": esf.InvalidReference,
        "STRING_TOO_LONG": esf.StringTooLong,
        "INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST": esf.InvalidPicklistValue,
        "UNABLE_TO_LOCK_ROW": esf.RowLocked,
        "CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY": esf.RecordSaveBlocked,
        "CANNOT_EXECUTE_FLOW_TRIGGER": esf.FlowFailed,
        "APEX_FAILED": esf.ApexActionFailed,
        "STORAGE_LIMIT_EXCEEDED": esf.StorageLimitExceeded,
        "REQUEST_LIMIT_EXCEEDED": esf.RequestLimitExceeded,
        "SERVER_UNAVAILABLE": esf.ServerError,
        "APEX_ERROR": esf.ApexError,
    }
    for code, cls in expected.items():
        assert CODE_MAP[code] is cls, code


def test_row_locked_is_retryable_and_others_not() -> None:
    assert classify(resp(400, rest_error("UNABLE_TO_LOCK_ROW")), DML).retryable is True
    assert classify(resp(400, rest_error("INVALID_FIELD")), DML).retryable is False


def test_duplicates_carry_duplicate_result() -> None:
    body = load_fixture("errors/duplicates_detected.json")
    exc = classify(resp(400, body), DML)
    assert isinstance(exc, esf.DuplicateDetected)
    assert exc.duplicates["duplicateRule"] == "Standard_Lead_Duplicate_Rule"


def test_multiple_errors_kept() -> None:
    body = rest_error("REQUIRED_FIELD_MISSING", "a", ["LastName"]) + rest_error("STRING_TOO_LONG", "b")
    exc = classify(resp(400, body), DML)
    assert isinstance(exc, esf.RequiredFieldMissing)
    assert [e.error_code for e in exc.errors] == ["REQUIRED_FIELD_MISSING", "STRING_TOO_LONG"]


def test_status_code_key_and_extended_fields() -> None:
    exc = classify(resp(400, [{"statusCode": "INVALID_FIELD", "message": "x", "extra": 1}]), SPEC)
    assert isinstance(exc, esf.InvalidField)
    assert exc.errors[0].extended == {"extra": 1}


def test_request_id_header() -> None:
    exc = classify(resp(404, rest_error("NOT_FOUND"), headers={"sforce-request-id": "req-1"}), SPEC)
    assert exc.request_id == "req-1"


# --- unknown codes / status fallbacks -------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "dml", "cls"),
    [
        (400, False, esf.RequestError),
        (400, True, esf.RecordError),
        (418, False, esf.RequestError),
        (500, False, esf.ServerError),
        (403, False, esf.Forbidden),
        (404, True, esf.NotFound),
    ],
)
def test_unknown_code_falls_back_by_status(status: int, dml: bool, cls: type[Exception]) -> None:
    spec = DML if dml else SPEC
    exc = classify(resp(status, rest_error("BRAND_NEW_CODE")), spec)
    assert type(exc) is cls
    assert exc.error_code == "BRAND_NEW_CODE"


@pytest.mark.parametrize(
    ("status", "cls", "retryable"),
    [
        (300, esf.MultipleMatches, False),
        (401, esf.SessionExpired, False),
        (403, esf.Forbidden, False),
        (404, esf.NotFound, False),
        (409, esf.Conflict, False),
        (410, esf.Gone, False),
        (412, esf.Conflict, False),
        (414, esf.RequestTooLarge, False),
        (420, esf.ServerError, False),
        (428, esf.Conflict, False),
        (429, esf.RequestLimitExceeded, False),
        (431, esf.RequestTooLarge, False),
        (400, esf.RequestError, False),
        (500, esf.ServerError, True),
        (502, esf.ServerError, True),
        (503, esf.ServerError, True),
        (504, esf.ServerError, True),
        (501, esf.ServerError, False),
        (599, esf.ServerError, False),
    ],
)
def test_status_fallbacks_empty_body(status: int, cls: type[esf.SalesforceError], retryable: bool) -> None:
    exc = classify(resp(status), SPEC)
    assert type(exc) is cls
    assert exc.status == status
    assert exc.retryable is retryable
    assert not exc.raw


def test_unknown_status_dml_empty_body() -> None:
    assert type(classify(resp(422), DML)) is esf.RecordError


def test_error_class_for_without_status() -> None:
    assert error_class_for(None, None, dml=False) is esf.RequestError
    assert error_class_for(None, None, dml=True) is esf.RecordError
    assert error_class_for("NOPE", 599, dml=False) is esf.ServerError


def test_server_unavailable_code_is_retryable_even_on_other_status() -> None:
    exc = classify(resp(400, rest_error("SERVER_UNAVAILABLE")), SPEC)
    assert isinstance(exc, esf.ServerError)
    assert exc.retryable is True
    exc2 = classify(resp(503, rest_error("SOMETHING")), SPEC)
    assert isinstance(exc2, esf.ServerError)
    assert exc2.retryable is True
    exc3 = classify(resp(420, rest_error("SOMETHING")), SPEC)
    assert isinstance(exc3, esf.ServerError)
    assert exc3.retryable is False


def test_success_and_not_modified() -> None:
    for s in (200, 201, 204, 299):
        assert classify(resp(s, {}), SPEC) is None
    assert isinstance(classify(resp(304), SPEC), esf.SalesforceError)
    spec304 = RequestSpec("GET", "sobjects/describe", allow_not_modified=True)
    assert classify(resp(304), spec304) is None


def test_multiple_matches_body() -> None:
    body = load_fixture("errors/multiple_matches.json")
    exc = classify(resp(300, body), DML)
    assert isinstance(exc, esf.MultipleMatches)
    assert exc.matches == tuple(body)
    assert "2 records" in exc.message
    exc2 = classify(resp(300, {"unexpected": True}), DML)
    assert isinstance(exc2, esf.MultipleMatches)
    assert exc2.matches == ()


def test_request_limit_with_retry_after() -> None:
    exc = classify(resp(403, rest_error("REQUEST_LIMIT_EXCEEDED"), headers={"retry-after": "7"}), SPEC)
    assert isinstance(exc, esf.RequestLimitExceeded)
    assert exc.retry_after == 7.0
    exc2 = classify(resp(429, headers={"retry-after": "3"}), SPEC)
    assert isinstance(exc2, esf.RequestLimitExceeded)
    assert exc2.retry_after == 3.0
    exc3 = classify(resp(429), SPEC)
    assert isinstance(exc3, esf.RequestLimitExceeded)
    assert exc3.retry_after is None


# --- non-JSON bodies ----------------------------------------------------------------------------


def test_html_body() -> None:
    html = load_fixture("errors/maintenance.html.json")
    exc = classify(resp(503, headers={"content-type": "text/html"}, text=html.encode()), SPEC)
    assert isinstance(exc, esf.UnexpectedResponse)
    assert exc.retryable is True
    assert "text/html" in exc.message
    assert exc.status == 503
    exc2 = classify(resp(400, headers={"content-type": "text/html"}, text=b"<html>nope</html>"), SPEC)
    assert isinstance(exc2, esf.UnexpectedResponse)
    assert exc2.retryable is False


@pytest.mark.parametrize(
    ("fixture", "status"),
    [("tooling/execute_uri_too_long.json", 414), ("tooling/execute_headers_too_large.json", 431)],
)
def test_too_large_html_page_is_request_too_large(fixture: str, status: int) -> None:
    """Recorded (V45): the web tier answers a long ``executeAnonymous`` URL with an HTML page."""
    page = load_fixture(fixture)
    assert isinstance(page, str)
    exc = classify(
        resp(status, headers={"content-type": "text/html;charset=iso-8859-1"}, text=page.encode()), SPEC
    )
    assert type(exc) is esf.RequestTooLarge
    assert exc.status == status
    assert exc.retryable is False
    assert exc.raw is not None
    assert b"Too L" in exc.raw
    plain = classify(
        resp(431, headers={"content-type": "text/plain"}, text=b"Request Header Fields Too Large"), SPEC
    )
    assert type(plain) is esf.RequestTooLarge


def test_non_json_without_content_type_and_truncated_raw() -> None:
    big = b"x" * 10_000
    r = RawResponse(502, {}, big)
    exc = classify(r, SPEC)
    assert isinstance(exc, esf.UnexpectedResponse)
    assert "no content-type" in exc.message
    assert isinstance(exc.raw, bytes)
    assert len(exc.raw) == 4096


def test_json_without_error_entries_falls_back() -> None:
    exc = classify(resp(400, {"foo": "bar"}), SPEC)
    assert type(exc) is esf.RequestError
    assert exc.message == "HTTP 400"
    assert exc.raw == b'{"foo": "bar"}'


# --- OAuth bodies ------------------------------------------------------------------------------


OAUTH = RequestSpec("GET", "/services/oauth2/userinfo", operation="oauth.userinfo")


def test_oauth_bodies() -> None:
    e1 = classify(
        resp(400, {"error": "invalid_grant", "error_description": "expired access/refresh token"}), OAUTH
    )
    assert isinstance(e1, esf.ReauthorizationRequired)
    assert e1.error_code == "invalid_grant"
    assert e1.message == "expired access/refresh token"
    e2 = classify(resp(401, {"error": "invalid_token", "error_description": "bad"}), OAUTH)
    assert isinstance(e2, esf.SessionExpired)
    e3 = classify(resp(400, {"error": "invalid_token"}), OAUTH)
    assert isinstance(e3, esf.SessionExpired)
    assert e3.message == "invalid_token"
    e4 = classify(
        resp(400, {"error": "invalid_client_id", "error_description": "client identifier invalid"}), OAUTH
    )
    assert isinstance(e4, esf.OAuthError)
    assert e4.error == "invalid_client_id"
    assert e4.error_description == "client identifier invalid"
    assert e4.error_code == "invalid_client_id"
    e5 = classify(resp(401, {"error": "whatever"}), OAUTH)
    assert isinstance(e5, esf.SessionExpired)


def test_error_key_outside_oauth_is_not_oauth() -> None:
    # An Apex REST class may return {"error": "..."}; that is not an OAuth failure.
    apex = RequestSpec("GET", "/services/apexrest/Orders/42", operation="raw")
    e404 = classify(resp(404, {"error": "order not found"}), apex)
    assert type(e404) is esf.NotFound
    assert e404.raw is not None
    e401 = classify(resp(401, {"error": "custom"}), SPEC)
    assert not isinstance(e401, esf.OAuthError)
    assert e401 is not None
    assert e401.error_code is None


def test_invalid_token_401_on_any_path_names_the_token() -> None:
    """The transport resends a write after a 401 only if the error says the token was refused."""
    data = RequestSpec("POST", "sobjects/Lead", operation="raw")
    exc = classify(resp(401, {"error": "invalid_token", "error_description": "expired"}), data)
    assert type(exc) is esf.SessionExpired
    assert (exc.error_code, exc.message, exc.status) == ("invalid_token", "expired", 401)
    bare = classify(resp(401, {"error": "invalid_token"}), data)
    assert bare is not None
    assert bare.message == "invalid_token"
    # Apex REST can send this body itself after its code ran: it doesn't name the token.
    apex = RequestSpec("POST", "/services/apexrest/Orders", operation="raw")
    forged = classify(resp(401, {"error": "invalid_token"}), apex)
    assert forged is not None
    assert forged.error_code != "invalid_token"
    # Not a 401, or a REST error list: no OAuth reading.
    other = classify(resp(400, {"error": "invalid_token"}), data)
    assert other is not None
    assert other.error_code is None


def test_apex_error_keeps_body() -> None:
    body = [{"errorCode": "APEX_ERROR", "message": "System.NullPointerException"}]
    exc = classify(resp(500, body), RequestSpec("POST", "/services/apexrest/Score", operation="raw"))
    assert isinstance(exc, esf.ApexError)
    assert exc.body == body


def test_invalid_session_rest_body() -> None:
    body = load_fixture("errors/invalid_session.json")
    exc = classify(resp(401, body), SPEC)
    assert isinstance(exc, esf.SessionExpired)
    assert exc.error_code == "INVALID_SESSION_ID"


# --- custom [TAG] error tags ---------------------------------------------------------------------


class AddressRequired(esf.ValidationRuleFailed):
    pass


class NotASubclass(esf.NotFound):
    pass


def test_tag_registered_subclass() -> None:
    body = rest_error("FIELD_CUSTOM_VALIDATION_EXCEPTION", "[ADDR_REQUIRED] Street is required", ["Street"])
    exc = classify(resp(400, body), DML, {"ADDR_REQUIRED": AddressRequired})
    assert type(exc) is AddressRequired
    assert exc.message == "Street is required"
    assert exc.tag == "ADDR_REQUIRED"
    assert exc.fields == ("Street",)
    assert isinstance(exc, esf.ValidationRuleFailed)


def test_tag_unregistered_populates_tag_only() -> None:
    body = rest_error("FIELD_CUSTOM_VALIDATION_EXCEPTION", "[OTHER_TAG] Something")
    exc = classify(resp(400, body), DML, {"ADDR_REQUIRED": AddressRequired})
    assert type(exc) is esf.ValidationRuleFailed
    assert exc.tag == "OTHER_TAG"
    assert exc.message == "[OTHER_TAG] Something"
    exc2 = classify(resp(400, body), DML)
    assert exc2.tag == "OTHER_TAG"


def test_tag_non_subclass_falls_back_to_default_class() -> None:
    body = rest_error("FIELD_CUSTOM_VALIDATION_EXCEPTION", "[ADDR_REQUIRED] Street is required")
    exc = classify(resp(400, body), DML, {"ADDR_REQUIRED": NotASubclass})
    assert type(exc) is esf.ValidationRuleFailed
    assert exc.tag == "ADDR_REQUIRED"
    assert exc.message == "[ADDR_REQUIRED] Street is required"


@pytest.mark.parametrize(
    ("message", "tag"),
    [
        ("[AB] x", "AB"),
        ("[A] x", None),  # too short
        ("[lower] x", None),
        (" [AB] x", None),  # must be at the start
        ("[" + "A" * 64 + "] x", "A" * 64),
        ("[" + "A" * 65 + "] x", None),
        ("[A_1] x", "A_1"),
    ],
)
def test_tag_pattern(message: str, tag: str | None) -> None:
    m = TAG_PATTERN.match(message)
    assert (m.group("tag") if m else None) == tag


# --- helpers ----------------------------------------------------------------------------------


def test_parse_details_shapes() -> None:
    assert parse_details(None) == ()
    assert parse_details("x") == ()
    assert parse_details([1, {"nothing": 1}]) == ()
    one = parse_details({"errorCode": "X", "message": "m", "fields": "notalist"})
    assert one[0].fields == ()
    assert one[0].error_code == "X"
    assert parse_details([{"message": "only"}])[0].error_code is None
    assert parse_details([{"errorCode": "X", "message": None}])[0].message == ""


def test_parse_retry_after() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after(" 5 ") == 5.0
    assert parse_retry_after("-3") == 0.0
    assert parse_retry_after("1.5") == 1.5
    later = format_datetime(now + timedelta(seconds=30), usegmt=True)
    assert parse_retry_after(later, now=now) == 30.0
    assert parse_retry_after(format_datetime(now - timedelta(seconds=30), usegmt=True), now=now) == 0.0
    assert parse_retry_after("Wed, 01 Jan 2026 00:00:10", now=now) == 10.0  # naive date → UTC
    assert parse_retry_after("garbage") is None
    assert parse_retry_after(later) is not None  # default "now"


def test_error_from_detail_without_spec() -> None:
    d = esf.ErrorDetail("INVALID_FIELD", "m", ("A",))
    exc = error_from_detail(d)
    assert isinstance(exc, esf.InvalidField)
    assert exc.operation is None
    assert exc.path is None
    assert exc.errors == (d,)
    exc2 = error_from_detail(esf.ErrorDetail("X", "m"), status=503, dml=False)
    assert isinstance(exc2, esf.ServerError)
    assert exc2.retryable is True
