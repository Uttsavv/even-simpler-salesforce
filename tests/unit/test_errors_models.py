from __future__ import annotations

import copy
import inspect
import pickle
from datetime import datetime, timezone
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce import errors as E

ALL_ERRORS = sorted(
    (obj for _, obj in inspect.getmembers(E, inspect.isclass) if issubclass(obj, esf.SalesforceError)),
    key=lambda c: c.__name__,
)


def test_every_error_is_exported() -> None:
    for cls in ALL_ERRORS:
        assert getattr(esf, cls.__name__) is cls
        assert cls.__name__ in esf.__all__


@pytest.mark.parametrize("cls", ALL_ERRORS, ids=lambda c: c.__name__)
def test_pickle_roundtrip(cls: type[esf.SalesforceError]) -> None:
    exc = cls(
        "msg",
        error_code="CODE",
        fields=["A", "B"],
        status=400,
        operation="sobject.update",
        sobject="Lead",
        method="PATCH",
        path="/sobjects/Lead/:id",
        request_id="rid",
        errors=[esf.ErrorDetail("CODE", "msg", ("A",), {"x": 1})],
        raw=b"raw",
        tag="TAG",
    )
    for clone in (pickle.loads(pickle.dumps(exc)), copy.deepcopy(exc), copy.copy(exc)):
        assert type(clone) is cls
        assert clone.__dict__ == exc.__dict__
        assert str(clone) == str(exc)
        assert clone.args == ("msg",)


def test_pickle_keeps_subclass_specific_attributes() -> None:
    cases: list[esf.SalesforceError] = [
        esf.RefreshFailed("x", ambiguous=True),
        esf.OAuthError("x", error="invalid_client", error_description="desc"),
        esf.MultipleMatches("x", matches=("a", "b")),
        esf.DuplicateDetected("x", duplicates={"r": 1}),
        esf.RequestLimitExceeded("x", retry_after=3.0),
        esf.BudgetExceeded("x", usage=None),
        esf.PartialFailure("x", results=esf.BatchResult([esf.RecordResult(False)])),
        esf.BulkJobFailed("x", job_id="750", state="Failed", error_message="e"),
        esf.ApexError("x", body={"a": 1}),
        esf.GraphQLError("x", graphql_errors=({"message": "m"},)),
        esf.SoapFault("x", fault_code="sf:INVALID"),
    ]
    for exc in cases:
        clone = pickle.loads(pickle.dumps(exc))
        assert clone.__dict__ == exc.__dict__, type(exc)


def test_custom_subclass_pickles() -> None:
    clone = pickle.loads(pickle.dumps(AddressRequired("Street is required", tag="ADDR_REQUIRED")))
    assert type(clone) is AddressRequired
    assert clone.tag == "ADDR_REQUIRED"


class AddressRequired(esf.ValidationRuleFailed):
    pass


def _docs(name: str) -> str:
    return f"\n  docs: {E.ERRORS_PAGE}#{name}"


def test_str_and_repr() -> None:
    assert str(esf.SalesforceError("plain")) == "plain" + _docs("salesforceerror")
    assert str(esf.SalesforceError("m", error_code="X")) == "X: m" + _docs("salesforceerror")
    assert str(
        esf.InvalidField("m", error_code="INVALID_FIELD", operation="query", sobject="Lead", status=400)
    ) == ("INVALID_FIELD: m (query, Lead, HTTP 400)" + _docs("invalidfield"))
    assert str(esf.NotFound("m", status=404)) == "m (HTTP 404)" + _docs("notfound")
    assert str(esf.NotFound("m", operation="sobject.get")) == "m (sobject.get)" + _docs("notfound")
    r = repr(esf.NotFound("m", error_code="NOT_FOUND", status=404, operation="sobject.get"))
    assert r == "NotFound(message='m', error_code='NOT_FOUND', status=404, operation='sobject.get')"


def test_str_appends_hint_then_docs_url() -> None:
    exc = esf.InvalidField("m", status=400, hint="did you mean 'Email'?")
    assert str(exc) == "m (HTTP 400)\n  hint: did you mean 'Email'?" + _docs("invalidfield")
    custom = esf.NotFound("m", docs_url="https://example.com/runbook#404")
    assert str(custom) == "m\n  docs: https://example.com/runbook#404"


def test_docs_url_points_at_the_nearest_library_class() -> None:
    assert esf.InvalidField().docs_url == f"{E.ERRORS_PAGE}#invalidfield"
    # a class registered in error_tags gets its library parent's entry
    assert AddressRequired().docs_url == f"{E.ERRORS_PAGE}#validationrulefailed"
    assert E.docs_url_for(ValueError) is None


def test_old_pickles_without_hint_fields_still_format() -> None:
    exc = esf.NotFound("m")
    del exc.__dict__["hint"], exc.__dict__["docs_url"]  # state as pickled by 0.1
    clone = pickle.loads(pickle.dumps(exc))
    assert str(clone) == "m"
    assert clone.hint is None


def test_defaults_and_retryable() -> None:
    e = esf.SalesforceError()
    assert (e.message, e.error_code, e.fields, e.errors, e.retryable, e.tag) == (
        "",
        None,
        (),
        (),
        False,
        None,
    )
    assert esf.ConnectError().retryable is True
    assert esf.RequestTimeout().retryable is True
    assert esf.RowLocked().retryable is True
    assert esf.RowLocked(retryable=False).retryable is False
    assert esf.OAuthError(error="e").error_code == "e"
    assert esf.OAuthError(error="e", error_code="other").error_code == "other"
    assert esf.RefreshFailed().ambiguous is False


@pytest.mark.parametrize(
    ("child", "parent"),
    [
        (esf.ConnectError, esf.TransportError),
        (esf.RequestTimeout, esf.TransportError),
        (esf.SessionExpired, esf.AuthError),
        (esf.ReauthorizationRequired, esf.AuthError),
        (esf.RefreshFailed, esf.AuthError),
        (esf.TokenConflict, esf.AuthError),
        (esf.OAuthError, esf.AuthError),
        (esf.MalformedQuery, esf.RequestError),
        (esf.InvalidField, esf.RequestError),
        (esf.InvalidType, esf.RequestError),
        (esf.NotFound, esf.RequestError),
        (esf.Forbidden, esf.RequestError),
        (esf.MultipleMatches, esf.RequestError),
        (esf.Conflict, esf.RequestError),
        (esf.Gone, esf.RequestError),
        (esf.RequestTooLarge, esf.RequestError),
        (esf.UnsupportedApiVersion, esf.RequestError),
        (esf.ValidationRuleFailed, esf.RecordError),
        (esf.RequiredFieldMissing, esf.RecordError),
        (esf.DuplicateDetected, esf.RecordError),
        (esf.InvalidReference, esf.RecordError),
        (esf.StringTooLong, esf.RecordError),
        (esf.InvalidPicklistValue, esf.RecordError),
        (esf.RowLocked, esf.RecordError),
        (esf.RecordSaveBlocked, esf.RecordError),
        (esf.FlowFailed, esf.RecordError),
        (esf.StorageLimitExceeded, esf.RecordError),
        (esf.RequestLimitExceeded, esf.LimitError),
        (esf.BudgetExceeded, esf.LimitError),
    ],
)
def test_hierarchy(child: type[Exception], parent: type[Exception]) -> None:
    assert issubclass(child, parent)
    assert issubclass(child, esf.SalesforceError)


# --- models ------------------------------------------------------------------------------------


def test_batch_result() -> None:
    ok = esf.RecordResult(True, "001xx0000000001AAA", True)
    bad = esf.RecordResult(
        False, None, None, (esf.ErrorDetail("REQUIRED_FIELD_MISSING", "Name missing", ("Name",)),)
    )
    bad2 = esf.RecordResult(False)
    b = esf.BatchResult([ok, bad, bad2], operation="collections.create", sobject="Account")
    assert len(b) == 3
    assert list(b) == [ok, bad, bad2]
    assert b[1] is bad
    assert b.succeeded == [ok]
    assert b.failed == [bad, bad2]
    with pytest.raises(esf.PartialFailure) as ei:
        b.raise_for_errors()
    exc = ei.value
    assert exc.results is b
    assert exc.error_code == "REQUIRED_FIELD_MISSING"
    assert exc.fields == ("Name",)
    assert exc.operation == "collections.create"
    assert exc.sobject == "Account"
    assert "2 of 3 records failed; first: REQUIRED_FIELD_MISSING: Name missing" in exc.message
    esf.BatchResult([ok]).raise_for_errors()
    with pytest.raises(esf.PartialFailure) as ei2:
        esf.BatchResult([bad2]).raise_for_errors()
    assert ei2.value.error_code is None
    assert ei2.value.message == "1 of 1 records failed"
    plain: esf.BatchResult[Any] = esf.BatchResult([{"x": 1}])
    assert plain.succeeded == [{"x": 1}]
    assert plain.failed == []


def test_query_result_limit_upsert() -> None:
    q = esf.QueryResult([{"Id": "1"}], total_size=5, done=False, cursor=esf.Cursor("abc"))
    assert len(q) == 1
    assert list(q) == [{"Id": "1"}]
    lim = esf.Limit(100, 30)
    assert lim.used == 70
    assert lim.details == {}
    assert esf.UpsertResult("x", True).created is True
    snap = esf.UsageSnapshot(None, None, None, datetime.now(timezone.utc), {}, {}, {}, {}, 0, 0, None)
    assert snap.api_bursts_used is None


def test_config_defaults() -> None:
    assert esf.Timeouts() == esf.Timeouts(connect=5, read=60, write=60, pool=5)
    b = esf.Budget()
    assert (b.warn_at, b.pause_low_priority_above, b.block_all_above) == ((0.5, 0.8, 0.95), 0.8, None)
    d = esf.DescribeCacheConfig()
    assert (d.ttl, d.max_entries, d.revalidate) == (3600.0, 500, True)
    with pytest.raises(AttributeError):
        b.warn_at = ()  # type: ignore[misc]


def test_public_surface() -> None:
    for name in esf.__all__:
        assert hasattr(esf, name), name
    for name in (
        "AsyncSalesforce",
        "Salesforce",
        "Token",
        "TokenStore",
        "InMemoryTokenStore",
        "soql",
        "Cursor",
        "Budget",
        "Observer",
        "capture",
        "__version__",
        "WebServerFlow",
        "ClientCredentials",
        "JwtBearer",
        "StaticToken",
        "QueryResult",
        "BatchResult",
    ):
        assert name in esf.__all__
    assert issubclass(esf.EsfDeprecationWarning, DeprecationWarning)


def test_error_detail_and_record_result_are_hashable() -> None:
    a = esf.ErrorDetail("CODE", "msg", ("A",), {"x": 1})
    b = esf.ErrorDetail("CODE", "msg", ("A",), {"x": 1})
    c = esf.ErrorDetail("CODE", "msg", ("A",), {"x": 2})
    assert a == b
    assert hash(a) == hash(b)
    assert a != c  # extended still counts for equality
    assert len({a, b, c}) == 2
    r = esf.RecordResult(False, None, None, (a,))
    assert hash(r) == hash(esf.RecordResult(False, None, None, (b,)))
    assert {r: 1}[esf.RecordResult(False, None, None, (b,))] == 1
