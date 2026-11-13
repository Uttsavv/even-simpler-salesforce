"""Configuration objects. All frozen, slotted dataclasses; no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["Backoff", "Budget", "DescribeCacheConfig", "Priority", "RetryPolicy", "Timeouts"]

Priority = Literal["low", "normal", "high"]


@dataclass(frozen=True, slots=True)
class Timeouts:
    """HTTP timeouts in seconds (§7.2)."""

    connect: float = 5.0
    read: float = 60.0
    write: float = 60.0
    pool: float = 5.0


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry policy (§7.4).

    Attributes:
        max_attempts: Total attempts per logical call, including the first one.
        base_delay: First backoff delay in seconds (doubled per attempt).
        max_delay: Cap for the exponential backoff.
        jitter: Use full jitter (``uniform(0, delay)``).
        respect_retry_after: Honour a ``Retry-After`` header when present.
        max_retry_after: A ``Retry-After`` longer than this is not waited for; the error is
            raised instead (with ``retry_after`` set) so the caller can decide.
    """

    max_attempts: int = 3
    base_delay: float = 0.25
    max_delay: float = 8.0
    jitter: bool = True
    respect_retry_after: bool = True
    max_retry_after: float = 30.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")


@dataclass(frozen=True, slots=True)
class Budget:
    """API usage budget (§10.4).

    Attributes:
        warn_at: Fractions of the daily API limit at which ``sf.usage.threshold`` is emitted
            (once per threshold per UTC day).
        pause_low_priority_above: Raise :class:`BudgetExceeded` for ``priority="low"`` calls
            when the latest known usage fraction is above this value.
        block_all_above: Raise :class:`BudgetExceeded` for every call except ``priority="high"``
            above this value.
    """

    warn_at: tuple[float, ...] = (0.5, 0.8, 0.95)
    pause_low_priority_above: float | None = 0.8
    block_all_above: float | None = None


@dataclass(frozen=True, slots=True)
class DescribeCacheConfig:
    """Describe cache settings (§12).

    Attributes:
        ttl: Seconds a cached describe is used without asking Salesforce.
        max_entries: Maximum entries in the default in-memory cache.
        revalidate: When an entry is stale, revalidate with ``If-None-Match`` (when Salesforce
            sent an ``ETag``) and ``If-Modified-Since``; a 304 keeps the entry.
        stale_ttl: Seconds a stale entry is kept for revalidation.
    """

    ttl: float = 3600.0
    max_entries: int = 500
    revalidate: bool = True
    stale_ttl: float = 86400.0


@dataclass(frozen=True, slots=True)
class Backoff:
    """How often to poll a long-running job, such as a Bulk job in ``job.wait()``.

    The first wait is ``start`` seconds. Each next wait is ``factor`` times longer, up to
    ``max`` seconds. There is no jitter: a status call is cheap and the job runs in Salesforce.

    Attributes:
        start: First delay in seconds.
        max: Longest delay in seconds.
        factor: Growth of the delay per poll.
    """

    start: float = 2.0
    max: float = 30.0
    factor: float = 2.0

    def __post_init__(self) -> None:
        if not self.start > 0:
            raise ValueError("Backoff.start must be > 0")
        if not self.max >= self.start:
            raise ValueError("Backoff.max must be >= start")
        if not self.factor >= 1:
            raise ValueError("Backoff.factor must be >= 1")

    def delay(self, attempt: int) -> float:
        """The wait before poll number ``attempt`` (0-based)."""
        return min(self.max, self.start * self.factor ** min(attempt, 64))
