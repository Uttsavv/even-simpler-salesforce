"""``fake.tooling``: the Tooling API on :class:`FakeSalesforce` (§9.12, §14).

- ``query`` and ``query_iter`` answer from the scripts registered with ``fake.on_query``.
- ``sobject(name)`` reads and writes the fake's record store, like ``fake.sobject(name)``.
- ``execute_anonymous`` answers from ``fake.on_execute_anonymous`` scripts; no Apex runs.
- ``run_tests_async`` returns a new ``AsyncApexJob`` id; ``completions`` returns what
  ``fake.set_completions`` set.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .._async.client import _sobject_of  # pyright: ignore[reportPrivateUsage]
from ..core.records import apply_model
from ..core.soql import soql_values
from ..core.tooling import check_apex_code, check_completion_type, run_tests_body
from ..models import AnonymousResult, QueryResult
from ._state import FakeState, Match, QueryScript, UnscriptedCall, Who, _Locator  # pyright: ignore[reportPrivateUsage]

if TYPE_CHECKING:
    from ._fake import AsyncFakeSObject, FakeSObject

__all__ = ["AsyncFakeTooling", "FakeTooling", "set_anonymous_script", "set_completions"]


@dataclass
class _ToolingState:
    anonymous: list[tuple[QueryScript, Any]] = field(default_factory=lambda: [])
    completions: dict[str, dict[str, Any]] = field(default_factory=lambda: {})


def _tooling(state: FakeState) -> _ToolingState:
    with state.lock:
        if state.tooling is None:
            state.tooling = _ToolingState()
        ts: _ToolingState = state.tooling
        return ts


def set_anonymous_script(state: FakeState, match: Match, result: Any, times: int | None) -> QueryScript:
    """Register an ``execute_anonymous`` answer (newest first)."""
    if result is not None and not (isinstance(result, (AnonymousResult, BaseException)) or callable(result)):
        raise TypeError("result must be an AnonymousResult, a callable or an exception instance")
    script = QueryScript(match, (), None, times, "anonymous")
    ts = _tooling(state)
    with state.lock:
        ts.anonymous.insert(0, (script, result))
    return script


def set_completions(state: FakeState, kind: str, completions: Mapping[str, Any]) -> None:
    """Set the ``completions(kind)`` answer."""
    ts = _tooling(state)
    with state.lock:
        ts.completions[check_completion_type(kind)] = copy.deepcopy(dict(completions))


class FakeTooling:
    """Fake of ``sf.tooling``. Same methods and signatures as the real one."""

    def __init__(self, state: FakeState, who: Who) -> None:
        self._state = state
        self._who = who

    # --- query & records --------------------------------------------------------------

    def query(self, soql: str, *values: Any, model: Any = None) -> QueryResult[Any]:
        """The first page of the rows scripted with ``fake.on_query``.

        Its cursor works with ``fake.query_more``.
        """
        soql = soql_values(soql, values)

        def fn() -> QueryResult[Any]:
            st = self._state
            ps = st.page_size(None)
            rows, total = st.query_rows(soql)
            page = st._slice(_Locator(rows, total, ps, soql, False, tooling=True), None, 0, ps)  # pyright: ignore[reportPrivateUsage]
            return st.to_result(*page, model)

        return self._state.run(
            self._who,
            "tooling.query",
            _sobject_of(soql),
            {"soql": soql},
            fn,
            method="GET",
            path="tooling/query",
            soql=soql,
        )

    def query_iter(self, soql: str, *values: Any, model: Any = None) -> Iterator[Any]:
        """Yield every scripted row, one ``tooling.query_more`` call per later page."""
        page = self.query(soql, *values)
        while True:
            for rec in page.records:
                yield apply_model([rec], model)[0] if model is not None else rec
            if page.cursor is None:
                return
            page = self._more(str(page.cursor))

    def _more(self, cursor: str) -> QueryResult[Any]:
        def fn() -> QueryResult[Any]:
            rows, total, done, nxt, _ = self._state.next_page(self._state.decode(cursor), None)
            return self._state.to_result(rows, total, done, nxt, None)

        return self._state.run(
            self._who,
            "tooling.query_more",
            None,
            {"cursor": cursor},
            fn,
            method="GET",
            path=f"/services/data/v{self._state.api_version}/tooling/query/000000000000000-0",
        )

    def sobject(self, name: str) -> FakeSObject:
        """Operations on one Tooling object. Records live in the fake's one record store."""
        from ._fake import FakeSObject  # _fake imports this module

        obj = FakeSObject(self._state, self._who, name)
        obj._op = "tooling.sobject."  # pyright: ignore[reportPrivateUsage]
        obj._base = "tooling/"  # pyright: ignore[reportPrivateUsage]
        return obj

    # --- anonymous Apex ---------------------------------------------------------------

    def execute_anonymous(self, code: str, *, capture_log: bool = False) -> AnonymousResult:
        """The result scripted with ``fake.on_execute_anonymous``; the code never runs.

        Raises:
            UnscriptedCall: no script matches ``code``.
        """
        check_apex_code(code)

        def fn() -> AnonymousResult:
            ts = _tooling(self._state)
            found = next(((s, r) for s, r in ts.anonymous if s.matches(code)), None)
            if found is None:
                known = ", ".join(s.describe() for s, _ in ts.anonymous) or "none registered"
                raise UnscriptedCall(
                    f"no execute_anonymous script matches {code[:80]!r}. Register one with "
                    f"fake.on_execute_anonymous(...); registered: {known}"
                )
            script, answer = found
            script.hits += 1
            if isinstance(answer, BaseException):
                raise answer
            if callable(answer):
                answer = answer(code)
            if answer is None:
                answer = AnonymousResult(compiled=True, success=True)
            if not isinstance(answer, AnonymousResult):
                raise TypeError(f"execute_anonymous scripts must give an AnonymousResult, got {answer!r}")
            result = answer
            return result if capture_log else dataclasses.replace(result, log=None)

        return self._state.run(
            self._who,
            "tooling.execute_anonymous",
            None,
            {"code": code, "capture_log": capture_log},
            fn,
            method="GET",
            path="tooling/executeAnonymous/",
        )

    # --- tests & completions ----------------------------------------------------------

    def run_tests_async(self, class_ids: Sequence[str]) -> str:
        """Return a new ``AsyncApexJob`` id (prefix ``707``); no tests run."""
        body = run_tests_body(class_ids)
        return self._state.run(
            self._who,
            "tooling.run_tests_async",
            None,
            {"class_ids": body["classids"].split(",")},
            lambda: self._state.new_id("AsyncApexJob"),
            method="POST",
            path="tooling/runTestsAsynchronous/",
        )

    def completions(self, type: str) -> dict[str, Any]:
        """What ``fake.set_completions(type, ...)`` set, else ``{}``."""
        check_completion_type(type)
        return self._state.run(
            self._who,
            "tooling.completions",
            None,
            {"type": type},
            lambda: copy.deepcopy(_tooling(self._state).completions.get(type, {})),
            method="GET",
            path="tooling/completions",
        )


class AsyncFakeTooling:
    """Async twin of :class:`FakeTooling`."""

    def __init__(self, inner: FakeTooling) -> None:
        self._inner = inner

    async def query(self, soql: str, *values: Any, model: Any = None) -> QueryResult[Any]:
        """See :meth:`FakeTooling.query`."""
        return self._inner.query(soql, *values, model=model)

    async def _aiter(self, it: Iterator[Any]) -> AsyncIterator[Any]:
        for row in it:
            yield row

    def query_iter(self, soql: str, *values: Any, model: Any = None) -> AsyncIterator[Any]:
        """See :meth:`FakeTooling.query_iter`."""
        return self._aiter(self._inner.query_iter(soql, *values, model=model))

    def sobject(self, name: str) -> AsyncFakeSObject:
        """See :meth:`FakeTooling.sobject`."""
        from ._fake import AsyncFakeSObject

        return AsyncFakeSObject(self._inner.sobject(name))

    async def execute_anonymous(self, code: str, *, capture_log: bool = False) -> AnonymousResult:
        """See :meth:`FakeTooling.execute_anonymous`."""
        return self._inner.execute_anonymous(code, capture_log=capture_log)

    async def run_tests_async(self, class_ids: Sequence[str]) -> str:
        """See :meth:`FakeTooling.run_tests_async`."""
        return self._inner.run_tests_async(class_ids)

    async def completions(self, type: str) -> dict[str, Any]:
        """See :meth:`FakeTooling.completions`."""
        return self._inner.completions(type)
