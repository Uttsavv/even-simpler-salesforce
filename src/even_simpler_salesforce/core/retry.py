"""Retry decisions and backoff (§7.4). No I/O."""

from __future__ import annotations

import random
from dataclasses import dataclass

from .. import errors as E
from ..config import RetryPolicy
from .http import RequestSpec
from .soap import is_soap_path

__all__ = ["RetryDecision", "backoff_delay", "decide_retry", "resend_after_401"]


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retry: bool
    reason: str | None = None
    delay: float | None = None  # explicit delay (Retry-After); None → backoff


_NO = RetryDecision(False)
_SERVER_RETRY_STATUS = frozenset({500, 502, 503, 504})


def decide_retry(spec: RequestSpec, exc: E.SalesforceError, *, sent: bool) -> RetryDecision:
    """Decide whether a failed attempt may be retried.

    Args:
        spec: The request.
        exc: The typed error of this attempt.
        sent: Whether the request may have reached Salesforce. ``False`` only when the
            connection failed before any byte was sent.
    """
    idem = spec.is_idempotent

    if isinstance(exc, E.TransportError):
        if not sent:
            return RetryDecision(True, "connect_error")
        if idem:
            return RetryDecision(True, "timeout" if isinstance(exc, E.RequestTimeout) else "transport_error")
        return _NO

    if isinstance(exc, E.RowLocked):
        return RetryDecision(True, "row_locked")

    if isinstance(exc, E.RequestLimitExceeded):
        if exc.retry_after is not None:
            return RetryDecision(True, "rate_limited", exc.retry_after)
        return _NO

    status = exc.status
    if _is_soap_fault(spec, exc):
        return _NO
    is_server = (status in _SERVER_RETRY_STATUS) or exc.error_code == "SERVER_UNAVAILABLE"
    if is_server and status != 420:
        return RetryDecision(True, f"http_{status}") if idem else _NO

    return _NO


def _is_soap_fault(spec: RequestSpec, exc: E.SalesforceError) -> bool:
    """A SOAP fault other than ``SERVER_UNAVAILABLE``: an answer, not a server failure.

    Salesforce sends every SOAP fault with HTTP 500, so the status doesn't mean the server
    failed. A fault such as ``INVALID_TYPE`` or ``INVALID_LOCATOR`` gets the same answer when
    resent (recorded in ``tests/fixtures/recorded/metadata``, V72/V73).
    """
    return (
        exc.status == 500
        and exc.error_code is not None
        and exc.error_code != "SERVER_UNAVAILABLE"
        and is_soap_path(spec.path)
    )


# Error codes that say Salesforce rejected the access token itself, before running anything.
_TOKEN_REJECTED = frozenset({"INVALID_SESSION_ID", "invalid_token"})


def resend_after_401(spec: RequestSpec, exc: E.SalesforceError) -> bool:
    """Whether a 401 may be answered with a token refresh and one resend (ADR 0001).

    Salesforce rejects an expired or revoked token with ``INVALID_SESSION_ID`` (REST) or
    ``invalid_token`` (OAuth endpoints) before it runs any code, so a resend is safe for every
    method. Any other 401 may come from code that already ran: an Apex REST class can return
    401 itself. Such a request is resent only if it is idempotent.
    """
    return exc.error_code in _TOKEN_REJECTED or spec.is_idempotent


def backoff_delay(policy: RetryPolicy, attempt: int, rng: random.Random | None = None) -> float:
    """Delay before retry number ``attempt`` (1-based): exponential, capped, full jitter."""
    ceiling = min(policy.max_delay, policy.base_delay * (2 ** max(0, attempt - 1)))
    if not policy.jitter:
        return ceiling
    return (rng or random).uniform(0, ceiling)
