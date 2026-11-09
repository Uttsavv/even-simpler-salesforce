"""Guard: every exported error survives pickling with all of its data, and error building
never trips over a body with missing keys.

simple-salesforce regressions this guards against (behaviour only, no code was read or copied):

- #208, #827: exceptions that can't be pickled (``__init__`` with required positional
  arguments), which breaks multiprocessing, Celery and RQ workers.
- #794, #791: exception attributes lost or ``str()`` raising after a round trip.
- #768: building an exception from an error body with a missing key raised ``KeyError``
  instead of the real error.

The class list comes from ``errors.__all__``, so a new error class is covered automatically.
A new subclass-specific keyword argument fails :func:`test_every_extra_kwarg_has_a_sample`
until a sample value is added to ``EXTRA_SAMPLES``.
"""

from __future__ import annotations

import copy
import inspect
import pickle
from datetime import datetime, timezone
from typing import Any

import pytest

from even_simpler_salesforce import errors as E
from even_simpler_salesforce.core.classify import classify
from even_simpler_salesforce.core.http import RawResponse, RequestSpec
from even_simpler_salesforce.models import BatchResult, RecordResult, UsageSnapshot

ERROR_CLASSES: list[type[E.SalesforceError]] = [
    obj
    for name in E.__all__
    if isinstance(obj := getattr(E, name), type) and issubclass(obj, E.SalesforceError)
]

DETAILS = (
    E.ErrorDetail("REQUIRED_FIELD_MISSING", "Name missing", ("Name",), {"extendedErrorDetails": [1]}),
    E.ErrorDetail(None, "second", (), {}),
)

COMMON: dict[str, Any] = {
    "error_code": "SOME_CODE",
    "fields": ["Name", "Email"],
    "status": 400,
    "operation": "sobject.update",
    "sobject": "Lead",
    "method": "PATCH",
    "path": "/sobjects/Lead/:id",
    "request_id": "req-123",
    "retryable": True,
    "errors": list(DETAILS),
    "raw": b'[{"errorCode":"SOME_CODE"}]',
    "tag": "MY_TAG",
}

_SNAPSHOT = UsageSnapshot(
    api_used=10,
    api_max=100,
    api_fraction=0.1,
    as_of=datetime(2026, 1, 1, tzinfo=timezone.utc),
    calls_by_operation={"query": 3},
    calls_by_sobject={"Lead": 3},
    calls_by_caller={},
    errors_by_code={"X": 1},
    retries=1,
    refreshes=0,
    projected_exhaustion=None,
)

# Sample value for every subclass-specific keyword argument (by parameter name).
EXTRA_SAMPLES: dict[str, Any] = {
    "ambiguous": True,
    "error": "invalid_client",
    "error_description": "client secret is wrong",
    "matches": ("/services/data/v1/sobjects/Lead/a", "/services/data/v1/sobjects/Lead/b"),
    "duplicates": {"duplicateRule": "R1", "matchResults": []},
    "retry_after": 2.5,
    "usage": _SNAPSHOT,
    "results": BatchResult(
        [RecordResult(True, "001xx0000000001AAA", True), RecordResult(False, errors=DETAILS)],
        operation="collections.create",
        sobject="Account",
    ),
    "job_id": "750xx0000000001AAA",
    "state": "Failed",
    "error_message": "InvalidBatch",
    "body": {"message": "apex said no"},
    "graphql_errors": ({"message": "bad field", "path": ["uiapi"]},),
    "fault_code": "sf:INVALID_ID",
}

BASE_ATTRS = (
    "message",
    "error_code",
    "fields",
    "status",
    "operation",
    "sobject",
    "method",
    "path",
    "request_id",
    "retryable",
    "errors",
    "raw",
    "tag",
)


def extra_params(cls: type[E.SalesforceError]) -> list[str]:
    """Keyword-only parameters a subclass adds on top of :class:`SalesforceError`."""
    base = set(inspect.signature(E.SalesforceError.__init__).parameters)
    params = inspect.signature(cls.__init__).parameters
    return [n for n, p in params.items() if n not in base and p.kind is inspect.Parameter.KEYWORD_ONLY]


def build(cls: type[E.SalesforceError]) -> E.SalesforceError:
    extras = {name: EXTRA_SAMPLES[name] for name in extra_params(cls)}
    return cls("something failed", **COMMON, **extras)


def ids(cls: type[Any]) -> str:
    return cls.__name__


def test_discovery_finds_the_whole_hierarchy() -> None:
    assert len(ERROR_CLASSES) >= 40
    assert E.SalesforceError in ERROR_CLASSES
    assert E.ErrorDetail not in ERROR_CLASSES  # exported, but a value object


def test_every_extra_kwarg_has_a_sample() -> None:
    missing = {n for cls in ERROR_CLASSES for n in extra_params(cls)} - set(EXTRA_SAMPLES)
    assert not missing, f"add sample values to EXTRA_SAMPLES for: {sorted(missing)}"


def test_classes_with_extras_are_the_expected_ones() -> None:
    with_extras = {cls.__name__: extra_params(cls) for cls in ERROR_CLASSES if extra_params(cls)}
    assert with_extras == {
        "ApexError": ["body"],
        "BudgetExceeded": ["usage"],
        "BulkJobFailed": ["job_id", "state", "error_message"],
        "DuplicateDetected": ["duplicates"],
        "GraphQLError": ["graphql_errors"],
        "MultipleMatches": ["matches"],
        "OAuthError": ["error", "error_description"],
        "PartialFailure": ["results"],
        "RefreshFailed": ["ambiguous"],
        "RequestLimitExceeded": ["retry_after"],
        "SoapFault": ["fault_code"],
    }


@pytest.mark.parametrize("cls", ERROR_CLASSES, ids=ids)
@pytest.mark.parametrize(
    "clone",
    [
        pytest.param(lambda e: pickle.loads(pickle.dumps(e)), id="pickle"),
        pytest.param(
            lambda e: pickle.loads(pickle.dumps(e, protocol=pickle.HIGHEST_PROTOCOL)), id="pickle-highest"
        ),
        pytest.param(lambda e: pickle.loads(pickle.dumps(e, protocol=2)), id="pickle-2"),
        pytest.param(copy.deepcopy, id="deepcopy"),
        pytest.param(copy.copy, id="copy"),
    ],
)
def test_roundtrip_keeps_type_text_and_every_attribute(cls: type[E.SalesforceError], clone: Any) -> None:
    exc = build(cls)
    out = clone(exc)

    assert type(out) is cls
    assert isinstance(out, E.SalesforceError)
    assert str(out) == str(exc)
    assert repr(out) == repr(exc)
    assert repr(out).startswith(f"{cls.__name__}(")
    assert out.args == exc.args == ("something failed",)

    for name in BASE_ATTRS:
        assert getattr(out, name) == getattr(exc, name), name
    assert out.fields == ("Name", "Email")
    assert out.errors == DETAILS
    assert all(type(d) is E.ErrorDetail for d in out.errors)
    assert out.errors[0].extended == {"extendedErrorDetails": [1]}

    for name in extra_params(cls):
        expected = EXTRA_SAMPLES[name]
        got = getattr(out, name)
        assert got == expected, name
        assert type(got) is type(expected), name

    assert out.__dict__ == exc.__dict__


@pytest.mark.parametrize("cls", ERROR_CLASSES, ids=ids)
def test_roundtrip_with_defaults_only(cls: type[E.SalesforceError]) -> None:
    exc = cls()
    out = pickle.loads(pickle.dumps(exc))
    assert type(out) is cls
    assert out.__dict__ == exc.__dict__
    assert str(out) == str(exc) == ""
    assert out.retryable is cls.default_retryable


@pytest.mark.parametrize("cls", ERROR_CLASSES, ids=ids)
def test_roundtrip_keeps_cause_free_and_is_raisable(cls: type[E.SalesforceError]) -> None:
    try:
        try:
            raise ValueError("inner")
        except ValueError as inner:
            raise build(cls) from inner
    except E.SalesforceError as exc:
        out = pickle.loads(pickle.dumps(exc))
    with pytest.raises(cls) as ei:
        raise out
    assert ei.value.message == "something failed"


def test_error_detail_pickles() -> None:
    for d in DETAILS:
        assert pickle.loads(pickle.dumps(d)) == d


# --- odd bodies never raise KeyError (#768) --------------------------------------------------

SPEC = RequestSpec("PATCH", "sobjects/Lead/00Qxx0000000001AAA", operation="sobject.update", dml=True)

ODD_BODIES: list[tuple[int, bytes]] = [
    (400, b"[{}]"),
    (400, b"{}"),
    (400, b"[]"),
    (400, b"null"),
    (400, b'""'),
    (400, b"0"),
    (400, b'[null, 1, "x"]'),
    (400, b'[{"message": "only a message"}]'),
    (400, b'[{"errorCode": "INVALID_FIELD"}]'),
    (400, b'[{"errorCode": null, "message": null, "fields": null}]'),
    (400, b'[{"statusCode": "STRING_TOO_LONG", "fields": "Name"}]'),
    (400, b'[{"errorCode": "DUPLICATES_DETECTED"}]'),
    (400, b'[{"errorCode": "DUPLICATES_DETECTED", "message": "dup", "duplicateResult": null}]'),
    (400, b'{"error": null}'),
    (400, b'{"error": "invalid_grant"}'),
    (400, b'{"error_description": "no error key"}'),
    (401, b'{"error": "invalid_token"}'),
    (401, b"{}"),
    (403, b'[{"errorCode": "REQUEST_LIMIT_EXCEEDED"}]'),
    (429, b"{}"),
    (300, b"{}"),
    (300, b"[]"),
    (300, b"null"),
    (404, b'[{"errorCode": 404, "message": 7}]'),
    (500, b'[{"errorCode": "SERVER_UNAVAILABLE"}]'),
    (500, b""),
    (503, b"<html>maintenance</html>"),
    (418, b'{"unexpected": {"nested": true}}'),
]


@pytest.mark.parametrize(("status", "content"), ODD_BODIES)
def test_classify_odd_bodies_builds_a_typed_error(status: int, content: bytes) -> None:
    headers = {"content-type": "application/json"} if content and not content.startswith(b"<") else {}
    resp = RawResponse(status, headers, content)
    err = classify(resp, SPEC)  # must not raise (KeyError, TypeError, AttributeError, ...)
    assert isinstance(err, E.SalesforceError)
    assert err.status == status
    assert isinstance(str(err), str)
    assert isinstance(repr(err), str)
    clone = pickle.loads(pickle.dumps(err))
    assert type(clone) is type(err)
    assert clone.__dict__ == err.__dict__
