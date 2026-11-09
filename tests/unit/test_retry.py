from __future__ import annotations

import random

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core.http import RequestSpec
from even_simpler_salesforce.core.retry import RetryDecision, backoff_delay, decide_retry

GET = RequestSpec("GET", "query")
HEAD = RequestSpec("HEAD", "query")
PATCH = RequestSpec("PATCH", "sobjects/Lead/00Qxx0000000001AAA")
PUT = RequestSpec("PUT", "x")
DELETE = RequestSpec("DELETE", "sobjects/Lead/00Qxx0000000001AAA")
POST = RequestSpec("POST", "sobjects/Lead")
POST_IDEM = RequestSpec("POST", "composite/sobjects/Lead", idempotent=True)
PATCH_NOT_IDEM = RequestSpec("PATCH", "x", idempotent=False)

IDEMPOTENT = [GET, HEAD, PATCH, PUT, DELETE, POST_IDEM]
NON_IDEMPOTENT = [POST, PATCH_NOT_IDEM]
ALL = IDEMPOTENT + NON_IDEMPOTENT


def _ids(specs: list[RequestSpec]) -> list[str]:
    return [
        f"{s.method}{'-idem' if s.idempotent else ''}{'-noidem' if s.idempotent is False else ''}"
        for s in specs
    ]


# --- §7.4 decision table ----------------------------------------------------------------------


@pytest.mark.parametrize("spec", ALL, ids=_ids(ALL))
def test_connect_error_always_retried(spec: RequestSpec) -> None:
    d = decide_retry(spec, esf.ConnectError("x"), sent=False)
    assert d == RetryDecision(True, "connect_error")


@pytest.mark.parametrize("spec", IDEMPOTENT, ids=_ids(IDEMPOTENT))
def test_timeout_after_send_retried_when_idempotent(spec: RequestSpec) -> None:
    assert decide_retry(spec, esf.RequestTimeout("x"), sent=True) == RetryDecision(True, "timeout")
    assert decide_retry(spec, esf.TransportError("reset"), sent=True) == RetryDecision(
        True, "transport_error"
    )


@pytest.mark.parametrize("spec", NON_IDEMPOTENT, ids=_ids(NON_IDEMPOTENT))
def test_timeout_after_send_not_retried_for_post(spec: RequestSpec) -> None:
    assert decide_retry(spec, esf.RequestTimeout("x"), sent=True).retry is False
    assert decide_retry(spec, esf.TransportError("reset"), sent=True).retry is False


@pytest.mark.parametrize("status", [500, 502, 503, 504])
@pytest.mark.parametrize("spec", ALL, ids=_ids(ALL))
def test_5xx(spec: RequestSpec, status: int) -> None:
    d = decide_retry(spec, esf.ServerError("x", status=status), sent=True)
    assert d.retry is spec.is_idempotent
    if d.retry:
        assert d.reason == f"http_{status}"


@pytest.mark.parametrize("spec", ALL, ids=_ids(ALL))
def test_server_unavailable_code(spec: RequestSpec) -> None:
    d = decide_retry(spec, esf.ServerError("x", status=400, error_code="SERVER_UNAVAILABLE"), sent=True)
    assert d.retry is spec.is_idempotent


@pytest.mark.parametrize("spec", ALL, ids=_ids(ALL))
def test_420_501_never_retried(spec: RequestSpec) -> None:
    assert decide_retry(spec, esf.ServerError("edge", status=420), sent=True).retry is False
    assert decide_retry(spec, esf.ServerError("x", status=501), sent=True).retry is False


@pytest.mark.parametrize("spec", ALL, ids=_ids(ALL))
def test_rate_limit_with_retry_after(spec: RequestSpec) -> None:
    exc = esf.RequestLimitExceeded("x", status=429, retry_after=4.0)
    assert decide_retry(spec, exc, sent=True) == RetryDecision(True, "rate_limited", 4.0)
    exc2 = esf.RequestLimitExceeded("x", status=403, error_code="REQUEST_LIMIT_EXCEEDED", retry_after=0.0)
    assert decide_retry(spec, exc2, sent=True) == RetryDecision(True, "rate_limited", 0.0)


@pytest.mark.parametrize("spec", ALL, ids=_ids(ALL))
def test_rate_limit_without_retry_after_not_retried(spec: RequestSpec) -> None:
    exc = esf.RequestLimitExceeded("x", status=403, error_code="REQUEST_LIMIT_EXCEEDED")
    assert decide_retry(spec, exc, sent=True).retry is False


@pytest.mark.parametrize("spec", ALL, ids=_ids(ALL))
def test_row_locked_always_retried(spec: RequestSpec) -> None:
    exc = esf.RowLocked("x", status=400, error_code="UNABLE_TO_LOCK_ROW")
    assert decide_retry(spec, exc, sent=True) == RetryDecision(True, "row_locked")


@pytest.mark.parametrize(
    "exc",
    [
        esf.InvalidField("x", status=400),
        esf.NotFound("x", status=404),
        esf.Conflict("x", status=409),
        esf.ValidationRuleFailed("x", status=400),
        esf.UnexpectedResponse("x", status=400),
        esf.SalesforceError("x"),
    ],
)
@pytest.mark.parametrize("spec", ALL, ids=_ids(ALL))
def test_other_4xx_never_retried(spec: RequestSpec, exc: esf.SalesforceError) -> None:
    assert decide_retry(spec, exc, sent=True).retry is False


def test_unexpected_html_503_retried_for_get() -> None:
    exc = esf.UnexpectedResponse("html", status=503)
    assert decide_retry(GET, exc, sent=True).retry is True
    assert decide_retry(POST, exc, sent=True).retry is False


def test_is_idempotent_inference() -> None:
    assert RequestSpec("post", "x").is_idempotent is False
    assert RequestSpec("get", "x").is_idempotent is True
    assert RequestSpec("POST", "x", idempotent=True).is_idempotent is True
    assert RequestSpec("GET", "x", idempotent=False).is_idempotent is False


# --- backoff ------------------------------------------------------------------------------


def test_backoff_without_jitter_doubles_and_caps() -> None:
    p = esf.RetryPolicy(base_delay=0.25, max_delay=1.0, jitter=False)
    assert [backoff_delay(p, n) for n in (0, 1, 2, 3, 4, 10)] == [0.25, 0.25, 0.5, 1.0, 1.0, 1.0]


def test_backoff_full_jitter_bounds() -> None:
    p = esf.RetryPolicy(base_delay=0.5, max_delay=3.0, jitter=True)
    rng = random.Random(1)
    for attempt in range(1, 8):
        ceiling = min(3.0, 0.5 * 2 ** (attempt - 1))
        for _ in range(50):
            d = backoff_delay(p, attempt, rng)
            assert 0 <= d <= ceiling
    assert 0 <= backoff_delay(p, 1) <= 0.5  # module-level random


def test_retry_policy_defaults_and_validation() -> None:
    p = esf.RetryPolicy()
    assert (p.max_attempts, p.base_delay, p.max_delay, p.jitter, p.respect_retry_after) == (
        3,
        0.25,
        8.0,
        True,
        True,
    )
    with pytest.raises(ValueError):
        esf.RetryPolicy(max_attempts=0)
