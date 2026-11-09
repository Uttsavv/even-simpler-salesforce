"""Retry decisions and backoff (§7.4). No I/O."""

from __future__ import annotations

import random
from dataclasses import dataclass

from .. import errors as E
from ..config import RetryPolicy
from .http import RequestSpec

__all__ = ["RetryDecision", "backoff_delay", "decide_retry"]


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
    is_server = (status in _SERVER_RETRY_STATUS) or exc.error_code == "SERVER_UNAVAILABLE"
    if is_server and status != 420:
        return RetryDecision(True, f"http_{status}") if idem else _NO

    return _NO


def backoff_delay(policy: RetryPolicy, attempt: int, rng: random.Random | None = None) -> float:
    """Delay before retry number ``attempt`` (1-based): exponential, capped, full jitter."""
    ceiling = min(policy.max_delay, policy.base_delay * (2 ** max(0, attempt - 1)))
    if not policy.jitter:
        return ceiling
    return (rng or random).uniform(0, ceiling)
