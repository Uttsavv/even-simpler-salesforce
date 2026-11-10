"""API usage tracking, budget thresholds and the budget guard (§10). No network I/O."""

from __future__ import annotations

import threading
from collections import Counter, OrderedDict, deque
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .config import Budget
from .core.limits import LimitInfo
from .errors import BudgetExceeded
from .models import UsageSnapshot

if TYPE_CHECKING:
    from .auth.token import Token

__all__ = ["UsageTracker"]

_WINDOW = timedelta(hours=1)
_MIN_SPAN = timedelta(minutes=1)
_SAMPLE_EVERY = timedelta(seconds=30)
#: Distinct caller tags counted one by one; later new tags count under ``"other"``.
MAX_CALLERS = 1000
#: Orgs whose ``Sforce-Limit-Info`` values are kept; the least recently seen is dropped.
MAX_ORGS = 1000
_DEFAULT_ORG = ""

Emit = Callable[[str, dict[str, Any]], object]
OrgResolver = Callable[[], "str | None"]


def org_key(token: Token | None) -> str | None:
    """The key the tracker files ``token``'s usage under: its org id, else its instance URL."""
    if token is None:
        return None
    return token.org_id or token.instance_url


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _OrgUsage:
    """``Sforce-Limit-Info`` state of one org: the values are org-wide, not per client."""

    __slots__ = ("as_of", "info", "samples", "warned")

    def __init__(self) -> None:
        self.info: LimitInfo | None = None
        self.as_of: datetime | None = None
        self.samples: deque[tuple[datetime, int]] = deque()
        self.warned: set[tuple[date, float]] = set()

    def fraction(self) -> float | None:
        info = self.info
        if info is None or info.api_used is None or not info.api_max:
            return None
        return info.api_used / info.api_max

    def projection(self) -> datetime | None:
        info = self.info
        if info is None or info.api_max is None or info.api_used is None or len(self.samples) < 2:
            return None
        (t0, u0), (t1, u1) = self.samples[0], self.samples[-1]
        span = t1 - t0
        if span < _MIN_SPAN or u1 <= u0:
            return None
        rate = (u1 - u0) / span.total_seconds()
        remaining = info.api_max - info.api_used
        if remaining <= 0:
            return t1
        return t1 + timedelta(seconds=remaining / rate)


class _Shared:
    """State shared by every view of one client family."""

    def __init__(self, budget: Budget | None, clock: Callable[[], datetime]) -> None:
        self.budget = budget
        self.clock = clock
        self.lock = threading.Lock()
        self.orgs: OrderedDict[str, _OrgUsage] = OrderedDict()
        self.last_org: str | None = None
        self.ops: Counter[str] = Counter()
        self.sobjects: Counter[str] = Counter()
        self.callers: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()
        self.retries = 0
        self.refreshes = 0
        self.emit: Emit | None = None


class UsageTracker:
    """Per-client-family usage counters. Thread-safe; shared by child clients.

    Exposed as ``sf.usage``. The call counters are shared by the whole family. The
    ``Sforce-Limit-Info`` values are kept per org, because a ``with_auth()`` child can log in
    to another org: ``sf.usage`` reads, and the budget guard checks, the org of ``sf``.
    """

    def __init__(self, budget: Budget | None = None, *, clock: Callable[[], datetime] = _utcnow) -> None:
        self._s = _Shared(budget, clock)
        self._resolve: OrgResolver | None = None

    def bound(self, resolve: OrgResolver) -> UsageTracker:
        """A view that shares this tracker's state and reads the org that ``resolve()`` names.

        ``resolve`` returns the org key (org id, else instance URL), or ``None`` if not known
        yet; then the view reads the org seen most recently.
        """
        view = object.__new__(UsageTracker)
        view._s = self._s
        view._resolve = resolve
        return view

    @property
    def budget(self) -> Budget | None:
        """The budget for the whole family."""
        return self._s.budget

    @budget.setter
    def budget(self, value: Budget | None) -> None:
        self._s.budget = value

    def bind(self, emit: Emit) -> None:
        self._s.emit = emit

    def _org_locked(self, org: str | None) -> _OrgUsage | None:
        if org is None and self._resolve is not None:
            org = self._resolve()
        if org is None:
            org = self._s.last_org
        return self._s.orgs.get(org) if org is not None else None

    # --- recording -----------------------------------------------------------------

    def observe(self, info: LimitInfo, *, org: str | None = None) -> None:
        """Record a parsed ``Sforce-Limit-Info`` header sent by ``org`` (default: this view's)."""
        s = self._s
        now = s.clock()
        crossed: list[float] = []
        if org is None and self._resolve is not None:
            org = self._resolve()
        key = _DEFAULT_ORG if org is None else org
        with s.lock:
            state = s.orgs.get(key)
            if state is None:
                state = s.orgs[key] = _OrgUsage()
                while len(s.orgs) > MAX_ORGS:
                    s.orgs.popitem(last=False)
            else:
                s.orgs.move_to_end(key)
            s.last_org = key
            prev = state.info
            state.info = LimitInfo(
                info.api_used if info.api_used is not None else (prev.api_used if prev else None),
                info.api_max if info.api_max is not None else (prev.api_max if prev else None),
                info.bursts_used,
                info.bursts_max,
            )
            state.as_of = now
            if info.api_used is not None:
                # At most ~2 samples per minute: the projection only needs the window's ends.
                if len(state.samples) >= 2 and now - state.samples[-2][0] < _SAMPLE_EVERY:
                    state.samples[-1] = (now, info.api_used)
                else:
                    state.samples.append((now, info.api_used))
                while state.samples and now - state.samples[0][0] > _WINDOW:
                    state.samples.popleft()
            frac = state.fraction()
            if s.budget and frac is not None:
                day = now.date()
                state.warned = {w for w in state.warned if w[0] == day}  # keep only today
                for t in sorted(s.budget.warn_at):
                    if frac >= t and (day, t) not in state.warned:
                        state.warned.add((day, t))
                        crossed.append(t)
            used, mx = state.info.api_used, state.info.api_max
        if s.emit is not None:
            for t in crossed:
                s.emit("sf.usage.threshold", {"threshold": t, "fraction": frac, "used": used, "max": mx})

    def record_call(
        self,
        *,
        operation: str,
        sobject: str | None,
        caller: str | None,
        error_code: str | None,
        retries: int,
    ) -> None:
        s = self._s
        with s.lock:
            s.ops[operation] += 1
            if sobject:
                s.sobjects[sobject] += 1
            tag = caller or "-"
            if tag not in s.callers and len(s.callers) >= MAX_CALLERS:
                tag = "other"  # caller tags should have low cardinality; cap the memory
            s.callers[tag] += 1
            if error_code:
                s.errors[error_code] += 1
            s.retries += retries

    def record_refresh(self) -> None:
        with self._s.lock:
            self._s.refreshes += 1

    # --- reading -------------------------------------------------------------------

    @property
    def fraction(self) -> float | None:
        with self._s.lock:
            state = self._org_locked(None)
            return state.fraction() if state else None

    def snapshot(self) -> UsageSnapshot:
        """Current usage: this org's header values plus the family's per-process counters."""
        s = self._s
        with s.lock:
            state = self._org_locked(None)
            info = (state.info if state else None) or LimitInfo()
            return UsageSnapshot(
                api_used=info.api_used,
                api_max=info.api_max,
                api_fraction=state.fraction() if state else None,
                as_of=state.as_of if state else None,
                calls_by_operation=dict(s.ops),
                calls_by_sobject=dict(s.sobjects),
                calls_by_caller=dict(s.callers),
                errors_by_code=dict(s.errors),
                retries=s.retries,
                refreshes=s.refreshes,
                projected_exhaustion=state.projection() if state else None,
                api_bursts_used=info.bursts_used,
                api_bursts_max=info.bursts_max,
            )

    def latest(self, *, org: str | None = None) -> tuple[int | None, int | None]:
        with self._s.lock:
            state = self._org_locked(org)
            info = state.info if state else None
            return (info.api_used, info.api_max) if info else (None, None)

    # --- guard ---------------------------------------------------------------------

    def check(self, priority: str, *, operation: str | None = None, org: str | None = None) -> None:
        """Raise :class:`BudgetExceeded` if the budget blocks a call with ``priority``.

        ``org`` is the org the call goes to (default: this view's org).
        """
        b = self._s.budget
        if b is None:
            return
        with self._s.lock:
            state = self._org_locked(org)
            frac = state.fraction() if state else None
        if frac is None:
            return
        if (b.block_all_above is not None and frac > b.block_all_above and priority != "high") or (
            b.pause_low_priority_above is not None and frac > b.pause_low_priority_above and priority == "low"
        ):
            view = self.bound(lambda: org) if org is not None else self
            raise BudgetExceeded(
                f"API budget: {frac:.1%} of the daily limit used; {priority}-priority call blocked",
                error_code="BUDGET_EXCEEDED",
                operation=operation,
                usage=view.snapshot(),
            )
