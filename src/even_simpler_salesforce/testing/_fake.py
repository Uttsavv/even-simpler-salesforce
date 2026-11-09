"""``FakeSalesforce`` / ``AsyncFakeSalesforce``: in-memory stand-ins for the real clients (§14).

Both facades share one :class:`~._state.FakeState` implementation, so their behaviour cannot
drift apart. The async facade simply awaits nothing: every method completes synchronously.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping, Sequence
from datetime import datetime
from types import TracebackType
from typing import Any

from .._async.client import DEFAULT_API_VERSION
from ..auth.flows import Auth, WebServerFlow
from ..auth.token import Token
from ..config import Budget, Priority
from ..core.cursor import Cursor
from ..core.http import check_instance_url, check_sobject
from ..core.limits import LimitInfo
from ..core.records import apply_model
from ..core.soql import soql_identifier, soql_values
from ..errors import MultipleMatches, NotFound, SalesforceError
from ..models import BatchResult, Limit, QueryPlan, QueryResult, RecordResult, UpsertResult
from ..observability.events import Observer, ObserverHub
from ..usage import UsageTracker
from ._state import FakeCall, FakeRequest, FakeState, Match, QueryScript, RowsSpec, Who, _sobject_of

__all__ = [
    "AsyncFakeAuthApi",
    "AsyncFakeCollections",
    "AsyncFakeSObject",
    "AsyncFakeSalesforce",
    "FakeAuthApi",
    "FakeCollections",
    "FakeSObject",
    "FakeSalesforce",
]


_DEFAULT_INSTANCE = "https://example.my.salesforce.com"


def _instance_of(auth: Auth | None) -> str:
    """The instance a real client with ``auth`` would end up on, without any I/O."""
    if auth is None:
        return _DEFAULT_INSTANCE
    static: Any = getattr(auth, "instance_url", None)
    if isinstance(static, str) and static:
        return static
    base: Any = getattr(auth, "base_url", None)
    if isinstance(base, str) and base.endswith(".my.salesforce.com"):
        return base
    return _DEFAULT_INSTANCE


class _FakeControls:
    """Test-side controls shared by both fakes (scripting, seeding, failures, assertions)."""

    _state: FakeState
    _who: Who

    # --- scripting ------------------------------------------------------------------

    def on_query(
        self,
        match: Match,
        rows: RowsSpec = (),
        *,
        total_size: int | None = None,
        times: int | None = None,
    ) -> QueryScript:
        """Script the answer to a SOQL query (used by ``query``, ``query_iter`` and ``count``).

        Queries are never executed: the fake has no SOQL engine. The newest matching script wins.

        Args:
            match: The SOQL text (compared with whitespace collapsed), a compiled regex
                (``re.search``), or a predicate ``(soql) -> bool``.
            rows: Rows to return, or a callable ``(soql) -> rows`` evaluated per call (e.g.
                ``lambda q: fake.records("Lead")``). Rows lacking ``attributes`` get them.
            total_size: ``totalSize`` to report; defaults to ``len(rows)``. ``count()`` returns it.
            times: Match at most this many times (``None`` = unlimited).

        Returns:
            The script; its ``hits`` attribute counts how often it matched.

        Example:
            fake.on_query("SELECT Id FROM Lead WHERE Status = 'Open'", rows=[{"Id": lead_id}])
        """
        return self._state.add_script(QueryScript(match, rows, total_size, times, "query"))

    def on_search(self, match: Match, rows: RowsSpec = (), *, times: int | None = None) -> QueryScript:
        """Script the answer to ``search`` (SOSL) or ``parameterized_search`` (matched on ``q``).

        Args:
            match: SOSL text, compiled regex or predicate (see :meth:`on_query`).
            rows: Records to return, or a callable ``(sosl) -> rows``.
            times: Match at most this many times (``None`` = unlimited).

        Returns:
            The script.
        """
        return self._state.add_script(QueryScript(match, rows, None, times, "search"))

    def on_request(
        self,
        method: str,
        path: str | re.Pattern[str],
        response: Any = None,
        *,
        times: int | None = None,
    ) -> None:
        """Register a handler for ``sf.request(method, path, ...)``.

        Args:
            method: HTTP method, or ``"*"`` for any.
            path: Path as passed to ``request`` (relative or absolute ``/services/...``; a
                versioned ``/services/data/vXX.X/`` prefix is ignored), or a regex matched
                against the normalised path with ``fullmatch``.
            response: Value returned to the caller (deep-copied), a callable receiving a
                :class:`FakeRequest`, or an exception instance to raise.
            times: Match at most this many times (``None`` = unlimited).
        """
        self._state.add_handler(method, path, response, times)

    def fail_next(
        self,
        operation: str,
        error: BaseException | type[BaseException],
        *,
        sobject: str | None = None,
        times: int = 1,
    ) -> None:
        """Make the next call(s) of ``operation`` raise ``error``.

        Args:
            operation: Real operation name (``"sobject.update"``, ``"query"``, ``"query_more"``,
                ``"collections.create"``, ``"limits"``, ``"raw"``...), or ``"*"`` for any.
            error: Exception instance or class, e.g. ``esf.ValidationRuleFailed("Street is required")``.
            sobject: Only fail calls for this sObject.
            times: Number of consecutive matching calls to fail.

        Example:
            fake.fail_next("sobject.update", esf.ValidationRuleFailed("[ADDR] Street is required"))
        """
        self._state.add_failure(operation, error, sobject, times)

    def reject(
        self,
        sobject: str | None,
        error: SalesforceError,
        *,
        when: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> None:
        """Reject record writes (create/update/upsert, single and collections) with ``error``.

        Unlike :meth:`fail_next` this acts per record, so collection calls return a
        ``BatchResult`` with ``RecordResult(ok=False, errors=...)`` for the rejected rows.

        Args:
            sobject: Restrict to this sObject (``None`` = any).
            error: The error to raise / report, e.g. ``esf.ValidationRuleFailed(...)``.
            when: Predicate on the written field values (updates include ``Id``).
        """
        self._state.add_rejection(sobject, error, when)

    def set_limits(self, limits: Mapping[str, Limit]) -> None:
        """Replace what ``limits()`` returns."""
        with self._state.lock:
            self._state.limits = dict(limits)

    def set_api_usage(self, used: int, max: int) -> None:
        """Pretend a ``Sforce-Limit-Info: api-usage=used/max`` header was seen (drives budgets)."""
        self._state.usage.observe(LimitInfo(used, max))

    def set_explain(self, plans: Iterable[QueryPlan]) -> None:
        """Plans returned by every later ``explain()`` call."""
        self._state.explain_plans = list(plans)

    def set_describe(self, sobject: str, describe: Mapping[str, Any]) -> None:
        """Set what ``sobject(name).describe()`` returns (otherwise a minimal one is synthesised)."""
        with self._state.lock:
            self._state.describes[check_sobject(sobject)] = dict(describe)

    # --- data -----------------------------------------------------------------------

    def add(self, sobject: str, data: Mapping[str, Any]) -> str:
        """Seed one record without recording a call. Returns its id (``data["Id"]`` if given)."""
        return self._state.seed(sobject, data)

    def add_many(self, sobject: str, rows: Iterable[Mapping[str, Any]]) -> list[str]:
        """Seed several records. Returns their ids."""
        return [self._state.seed(sobject, r) for r in rows]

    def records(self, sobject: str) -> list[dict[str, Any]]:
        """Copies of every stored record of ``sobject`` (with ``attributes`` and ``Id``)."""
        return self._state.snapshot_records(sobject)

    def record(self, sobject: str, record_id: str) -> dict[str, Any] | None:
        """A copy of one stored record, or ``None``."""
        return self._state.snapshot_record(sobject, record_id)

    def new_id(self, sobject: str) -> str:
        """A fresh, valid-looking 18-character id with ``sobject``'s key prefix."""
        return self._state.new_id(check_sobject(sobject))

    # --- assertions -----------------------------------------------------------------

    @property
    def calls(self) -> list[FakeCall]:
        """Every call made through this fake family, in order."""
        with self._state.lock:
            return list(self._state.calls)

    def calls_for(self, operation: str, *, sobject: str | None = None) -> list[FakeCall]:
        """Calls of one operation (optionally for one sObject)."""
        return [
            c for c in self.calls if c.operation == operation and (sobject is None or c.sobject == sobject)
        ]

    def reset_calls(self) -> None:
        """Forget recorded calls."""
        with self._state.lock:
            self._state.calls.clear()

    # --- client-compatible properties -------------------------------------------------

    @property
    def usage(self) -> UsageTracker:
        """Usage tracker; ``usage.snapshot()`` counts the fake's calls like the real client."""
        return self._state.usage

    @property
    def api_version(self) -> str:
        """The API version the fake pretends to use."""
        return self._state.api_version

    @property
    def observers(self) -> ObserverHub:
        """Observers receiving the fake's ``sf.request`` events."""
        return self._state.hub


# --- sync -----------------------------------------------------------------------------


class FakeSObject:
    """Fake of ``sf.sobject(name)``. Same methods and signatures as the real one."""

    def __init__(self, state: FakeState, who: Who, name: str) -> None:
        self._state = state
        self._who = who
        self.name = check_sobject(name)

    def __repr__(self) -> str:
        return f"<FakeSObject {self.name}>"

    def _path(self, *parts: str) -> str:
        return "/".join(["sobjects", self.name, *parts])

    def get(
        self,
        record_id: str,
        fields: Sequence[str] | None = None,
        *,
        model: Any = None,
        if_modified_since: datetime | None = None,
    ) -> Any:
        """Return a stored record. Raises :class:`~even_simpler_salesforce.NotFound` when missing."""
        return self._state.run(
            self._who,
            "sobject.get",
            self.name,
            {"id": record_id, "fields": fields, "if_modified_since": if_modified_since},
            lambda: self._state.get(self.name, record_id, fields, model, if_modified_since),
            method="GET",
            path=self._path(record_id),
        )

    def get_by_external_id(
        self, field: str, value: str, fields: Sequence[str] | None = None, *, model: Any = None
    ) -> Any:
        """Return the record whose ``field`` equals ``value``."""
        return self._state.run(
            self._who,
            "sobject.get_by_external_id",
            self.name,
            {"field": field, "value": value, "fields": fields},
            lambda: self._state.get_by_external_id(self.name, field, value, fields, model),
            method="GET",
            path=self._path(field, ":value"),
        )

    def describe(self, *, cached: bool = True) -> dict[str, Any]:
        """The describe set with ``fake.set_describe`` or a minimal synthesised one."""
        return self._state.run(
            self._who,
            "sobject.describe",
            self.name,
            {"cached": cached},
            lambda: self._state.describe(self.name),
            method="GET",
            path=self._path("describe"),
        )

    def metadata(self) -> dict[str, Any]:
        """Basic sObject info (``objectDescribe`` + empty ``recentItems``)."""
        return self._state.run(
            self._who,
            "sobject.metadata",
            self.name,
            {},
            lambda: self._state.metadata(self.name),
            method="GET",
            path=self._path(),
        )

    def updated(self, start: datetime, end: datetime) -> dict[str, Any]:
        """Ids of records created or changed in ``[start, end]``."""
        return self._state.run(
            self._who,
            "sobject.updated",
            self.name,
            {"start": start, "end": end},
            lambda: self._state.updated(self.name, start, end),
            method="GET",
            path=self._path("updated"),
        )

    def deleted(self, start: datetime, end: datetime) -> dict[str, Any]:
        """Records deleted in ``[start, end]``."""
        return self._state.run(
            self._who,
            "sobject.deleted",
            self.name,
            {"start": start, "end": end},
            lambda: self._state.deleted_between(self.name, start, end),
            method="GET",
            path=self._path("deleted"),
        )

    def create(
        self,
        data: Mapping[str, Any],
        *,
        assignment_rule_id: str | bool | None = None,
        allow_save: bool | None = None,
    ) -> str:
        """Store a new record and return its 18-character id."""
        return self._state.run(
            self._who,
            "sobject.create",
            self.name,
            {"data": dict(data), "assignment_rule_id": assignment_rule_id, "allow_save": allow_save},
            lambda: self._state.create(self.name, data),
            method="POST",
            path=self._path(),
            records=1,
        )

    def update(
        self,
        record_id: str,
        data: Mapping[str, Any],
        *,
        assignment_rule_id: str | bool | None = None,
        allow_save: bool | None = None,
        if_unmodified_since: datetime | None = None,
    ) -> None:
        """Merge ``data`` into a stored record. :class:`NotFound` / :class:`Conflict` like the real API."""
        self._state.run(
            self._who,
            "sobject.update",
            self.name,
            {
                "id": record_id,
                "data": dict(data),
                "assignment_rule_id": assignment_rule_id,
                "allow_save": allow_save,
                "if_unmodified_since": if_unmodified_since,
            },
            lambda: self._state.update(self.name, record_id, data, if_unmodified_since),
            method="PATCH",
            path=self._path(record_id),
            records=1,
        )

    def upsert(
        self,
        field: str,
        value: str,
        data: Mapping[str, Any],
        *,
        assignment_rule_id: str | bool | None = None,
        allow_save: bool | None = None,
    ) -> UpsertResult:
        """Update the record whose ``field == value``, or create it. :class:`MultipleMatches` on >1."""
        soql_identifier(field)
        return self._state.run(
            self._who,
            "sobject.upsert",
            self.name,
            {
                "field": field,
                "value": value,
                "data": dict(data),
                "assignment_rule_id": assignment_rule_id,
                "allow_save": allow_save,
            },
            lambda: self._state.upsert(self.name, field, value, data),
            method="PATCH",
            path=self._path(field, ":value"),
            records=1,
        )

    def delete(self, record_id: str, *, if_unmodified_since: datetime | None = None) -> None:
        """Delete a stored record (a second delete raises ``NotFound(ENTITY_IS_DELETED)``)."""
        self._state.run(
            self._who,
            "sobject.delete",
            self.name,
            {"id": record_id, "if_unmodified_since": if_unmodified_since},
            lambda: self._state.delete(self.name, record_id, if_unmodified_since),
            method="DELETE",
            path=self._path(record_id),
            records=1,
        )


class FakeCollections:
    """Fake of ``sf.collections``: real :class:`BatchResult` / :class:`RecordResult` values.

    With ``all_or_none=True`` each chunk of 200 is rolled back and the first error raised;
    earlier chunks stay committed, exactly like the real API.
    """

    def __init__(self, state: FakeState, who: Who, concurrency: int = 4) -> None:
        self._state = state
        self._who = who
        self.concurrency = concurrency

    def create(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        sobject: str | None = None,
        all_or_none: bool = False,
        concurrency: int | None = None,
    ) -> BatchResult[RecordResult]:
        """Create records. Rows need ``attributes.type`` or ``sobject=``."""
        row_list = [dict(r) for r in rows]
        return self._state.run(
            self._who,
            "collections.create",
            sobject,
            {"rows": row_list, "sobject": sobject, "all_or_none": all_or_none},
            lambda: self._state.coll_create(row_list, sobject, all_or_none),
            method="POST",
            path="composite/sobjects",
            records=len(row_list),
        )

    def update(
        self,
        sobject: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        all_or_none: bool = False,
        concurrency: int | None = None,
    ) -> BatchResult[RecordResult]:
        """Update records; each row needs ``Id``."""
        row_list = [dict(r) for r in rows]
        return self._state.run(
            self._who,
            "collections.update",
            sobject,
            {"rows": row_list, "all_or_none": all_or_none},
            lambda: self._state.coll_update(sobject, row_list, all_or_none),
            method="PATCH",
            path="composite/sobjects",
            records=len(row_list),
        )

    def upsert(
        self,
        sobject: str,
        field: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        all_or_none: bool = False,
        concurrency: int | None = None,
    ) -> BatchResult[RecordResult]:
        """Upsert records by external id ``field``."""
        row_list = [dict(r) for r in rows]
        return self._state.run(
            self._who,
            "collections.upsert",
            sobject,
            {"field": field, "rows": row_list, "all_or_none": all_or_none},
            lambda: self._state.coll_upsert(sobject, field, row_list, all_or_none),
            method="PATCH",
            path=f"composite/sobjects/{sobject}/{field}",
            records=len(row_list),
        )

    def delete(
        self,
        ids: Iterable[str],
        *,
        all_or_none: bool = False,
        concurrency: int | None = None,
    ) -> BatchResult[RecordResult]:
        """Delete records by id (any sObject)."""
        id_list = list(ids)
        return self._state.run(
            self._who,
            "collections.delete",
            None,
            {"ids": id_list, "all_or_none": all_or_none},
            lambda: self._state.coll_delete(id_list, all_or_none),
            method="DELETE",
            path="composite/sobjects",
            records=len(id_list),
        )

    def retrieve(
        self,
        sobject: str,
        ids: Iterable[str],
        fields: Sequence[str],
        *,
        concurrency: int | None = None,
    ) -> list[dict[str, Any] | None]:
        """Records in input order, ``None`` for unknown ids."""
        id_list = list(ids)
        return self._state.run(
            self._who,
            "collections.retrieve",
            sobject,
            {"ids": id_list, "fields": list(fields)},
            lambda: self._state.coll_retrieve(sobject, id_list, fields),
            method="POST",
            path=f"composite/sobjects/{sobject}",
        )


class FakeAuthApi:
    """``fake.auth``: the real ``sf.auth`` surface, answered in memory (no OAuth server).

    ``token()`` and ``exchange_code()`` return a fake :class:`Token` on the fake's instance;
    ``revoke()`` records the call; ``userinfo()`` / ``introspect()`` return minimal bodies.
    Every call is recorded in ``fake.calls`` like any other operation.
    """

    def __init__(self, client: FakeSalesforce) -> None:
        self._sf = client

    def _token(self) -> Token:
        return Token(
            access_token="fake-access-token",  # noqa: S106 - not a credential
            instance_url=self._sf.instance_url,
            issued_at=self._sf._state.clock(),
        )

    def _run(
        self, operation: str, args: Mapping[str, Any], fn: Callable[[], Any], method: str, path: str
    ) -> Any:
        return self._sf._state.run(self._sf._who, operation, None, args, fn, method=method, path=path)

    def exchange_code(self, flow: WebServerFlow, code: str, *, code_verifier: str | None = None) -> Token:
        """A fake token, as if ``code`` had been exchanged."""
        return self._run("oauth.exchange_code", {"code": code}, self._token, "POST", "/services/oauth2/token")

    def revoke(self, token_key: str | None = None, *, auth: Auth | None = None) -> None:
        """Records the revoke call."""
        self._run("oauth.revoke", {"token_key": token_key}, lambda: None, "POST", "/services/oauth2/revoke")

    def userinfo(self) -> dict[str, Any]:
        """A minimal ``userinfo`` body."""
        return self._run(
            "oauth.userinfo",
            {},
            lambda: {"user_id": "005000000000001AAA", "organization_id": "00D000000000001AAA"},
            "GET",
            "/services/oauth2/userinfo",
        )

    def introspect(self, token_key: str | None = None, *, auth: Auth | None = None) -> dict[str, Any]:
        """``{"active": True}``."""
        return self._run(
            "oauth.introspect",
            {"token_key": token_key},
            lambda: {"active": True},
            "POST",
            "/services/oauth2/introspect",
        )

    def token(self) -> Token:
        """A fake token (no call is recorded: the real method does no I/O when a token is cached)."""
        return self._token()


class AsyncFakeAuthApi:
    """Async ``fake.auth``; see :class:`FakeAuthApi`."""

    def __init__(self, inner: FakeAuthApi) -> None:
        self._inner = inner

    async def exchange_code(
        self, flow: WebServerFlow, code: str, *, code_verifier: str | None = None
    ) -> Token:
        """See :meth:`FakeAuthApi.exchange_code`."""
        return self._inner.exchange_code(flow, code, code_verifier=code_verifier)

    async def revoke(self, token_key: str | None = None, *, auth: Auth | None = None) -> None:
        """See :meth:`FakeAuthApi.revoke`."""
        self._inner.revoke(token_key, auth=auth)

    async def userinfo(self) -> dict[str, Any]:
        """See :meth:`FakeAuthApi.userinfo`."""
        return self._inner.userinfo()

    async def introspect(self, token_key: str | None = None, *, auth: Auth | None = None) -> dict[str, Any]:
        """See :meth:`FakeAuthApi.introspect`."""
        return self._inner.introspect(token_key, auth=auth)

    async def token(self) -> Token:
        """See :meth:`FakeAuthApi.token`."""
        return self._inner.token()


class FakeSalesforce(_FakeControls):
    """In-memory stand-in for :class:`even_simpler_salesforce.Salesforce` in unit tests.

    It is duck-typed: it has the real client's methods for records, query/search,
    collections, ``request``, ``limits``, ``usage``, ``with_auth`` and ``tagged``, but it is not
    a subclass. Use ``typing.cast`` where your code is type-checked against the real class.

    Example:
        fake = FakeSalesforce()
        lead_id = fake.sobject("Lead").create({"LastName": "Doe", "Company": "Acme"})
        fake.on_query("SELECT Id FROM Lead", rows=fake.records("Lead"))
        fake.fail_next("sobject.update", esf.ValidationRuleFailed("Street is required"))

    Every call emits a ``sf.request`` event (so ``esf.capture()`` and observers work) and
    is recorded in :attr:`calls`.

    Args:
        auth: Ignored; accepted so the fake can be built like the real client.
        api_version: API version used in record URLs and cursors.
        cursor_key: Sign cursors like the real client's ``cursor_key``.
        observers: Event observers.
        budget: Budget guard, applied to the fake's own usage numbers.
        key_prefixes: Override the 3-character id prefix per sObject.
        clock: Returns "now" (timezone-aware) for modification timestamps.
    """

    def __init__(
        self,
        auth: Auth | None = None,
        *,
        api_version: str = DEFAULT_API_VERSION,
        cursor_key: bytes | None = None,
        observers: Iterable[Observer] = (),
        budget: Budget | None = None,
        key_prefixes: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._state = FakeState(
            api_version=api_version,
            cursor_key=cursor_key,
            observers=observers,
            budget=budget,
            key_prefixes=key_prefixes,
            clock=clock,
        )
        self._who = Who()
        self._root = True
        self.auth_config = auth
        self._instance_url = _instance_of(auth)
        self.collections = FakeCollections(self._state, self._who)
        self.auth = FakeAuthApi(self)

    @classmethod
    def _view(
        cls, state: FakeState, who: Who, auth: Auth | None, instance_url: str | None = None
    ) -> FakeSalesforce:
        view = cls.__new__(cls)
        view._state = state
        view._who = who
        view._root = False
        view.auth_config = auth
        view._instance_url = instance_url or _instance_of(auth)
        view.collections = FakeCollections(state, who)
        view.auth = FakeAuthApi(view)
        return view

    def __repr__(self) -> str:
        return f"<FakeSalesforce v{self._state.api_version}>"

    # --- lifecycle --------------------------------------------------------------------

    def __enter__(self) -> FakeSalesforce:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the fake family (later calls raise ``RuntimeError``). No-op on children."""
        if self._root:
            self._state.closed = True

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` was called on the root fake."""
        return self._state.closed

    # --- family -----------------------------------------------------------------------

    def with_auth(self, auth: Auth, *, store: Any = None, instance_url: str | None = None) -> FakeSalesforce:
        """A child sharing the same records, scripts and call log."""
        if instance_url is not None:
            instance_url = check_instance_url(instance_url)
        return type(self)._view(self._state, self._who, auth, instance_url)

    def tagged(self, caller: str | None = None, priority: Priority | None = None) -> FakeSalesforce:
        """A child whose calls carry ``caller`` / ``priority`` (visible in :attr:`calls`)."""
        who = Who(caller or self._who.caller, priority or self._who.priority)
        return type(self)._view(self._state, who, self.auth_config, self._instance_url)

    @property
    def instance_url(self) -> str:
        """``StaticToken.instance_url`` or ``https://`` + the auth's My Domain host, else
        ``https://example.my.salesforce.com``."""
        return self._instance_url

    # --- records ----------------------------------------------------------------------

    def sobject(self, name: str) -> FakeSObject:
        """Operations on one sObject type."""
        return FakeSObject(self._state, self._who, name)

    def describe_global(self, *, cached: bool = True) -> dict[str, Any]:
        """Lists every sObject the fake knows about."""
        return self._state.run(
            self._who,
            "describe_global",
            None,
            {"cached": cached},
            self._state.describe_global,
            method="GET",
            path="sobjects",
        )

    # --- query & search ---------------------------------------------------------------

    def query(
        self,
        soql: str,
        *values: Any,
        page_size: int | None = None,
        include_deleted: bool = False,
        model: Any = None,
    ) -> QueryResult[Any]:
        """Return the first page of the scripted rows (``page_size`` honoured, default 2000).

        ``values`` fill ``{}`` placeholders exactly like the real client; scripts registered
        with :meth:`on_query` match the resulting SOQL.
        """
        soql = soql_values(soql, values)

        def fn() -> QueryResult[Any]:
            rows, total, done, nxt = self._state.first_page(soql, page_size, include_deleted)
            return self._state.to_result(rows, total, done, nxt, model)

        return self._state.run(
            self._who,
            "query",
            _sobject_of(soql),
            {"soql": soql, "page_size": page_size, "include_deleted": include_deleted},
            fn,
            method="GET",
            path="queryAll" if include_deleted else "query",
            soql=soql,
        )

    def query_one(self, soql: str, *values: Any, include_deleted: bool = False, model: Any = None) -> Any:
        """Exactly one scripted row; ``NotFound`` for none, ``MultipleMatches`` for several."""
        page = self.query(soql, *values, include_deleted=include_deleted)
        sobject = _sobject_of(soql_values(soql, values))
        if page.total_size == 0 or not page.records:
            raise NotFound("query returned no rows; expected exactly one", operation="query", sobject=sobject)
        if page.total_size > 1:
            ids = tuple(str(r["Id"]) for r in page.records if r.get("Id"))
            raise MultipleMatches(
                f"query returned {page.total_size} rows; expected exactly one",
                matches=ids,
                operation="query",
                sobject=sobject,
            )
        return apply_model(page.records[:1], model)[0]

    def query_first(self, soql: str, *values: Any, include_deleted: bool = False, model: Any = None) -> Any:
        """The first scripted row, or ``None``."""
        page = self.query(soql, *values, include_deleted=include_deleted)
        return apply_model(page.records[:1], model)[0] if page.records else None

    def explain(self, soql: str, *values: Any) -> list[QueryPlan]:
        """Plans set with :meth:`set_explain` (an empty list otherwise)."""
        soql = soql_values(soql, values)
        return self._state.run(
            self._who,
            "query.explain",
            _sobject_of(soql),
            {"soql": soql},
            lambda: list(self._state.explain_plans),
            method="GET",
            path="query",
            soql=soql,
        )

    def query_more(
        self, cursor: Cursor | str, *, page_size: int | None = None, model: Any = None
    ) -> QueryResult[Any]:
        """The page after ``cursor``. Tampered/foreign cursors raise :class:`MalformedQuery`."""

        def fn() -> QueryResult[Any]:
            path = self._state.decode(cursor)
            rows, total, done, nxt, _ = self._state.next_page(path, page_size)
            return self._state.to_result(rows, total, done, nxt, model)

        return self._state.run(
            self._who,
            "query_more",
            None,
            {"cursor": str(cursor), "page_size": page_size},
            fn,
            method="GET",
            path=f"/services/data/v{self._state.api_version}/query/000000000000000-0",
        )

    def query_iter(
        self,
        soql: str,
        *values: Any,
        page_size: int | None = None,
        include_deleted: bool = False,
        model: Any = None,
        expand_children: bool = False,
    ) -> Iterator[Any]:
        """Yield every scripted row, fetching "pages" lazily (one ``query_more`` call per page)."""
        page = self.query(soql, *values, page_size=page_size, include_deleted=include_deleted)
        while True:
            for rec in page.records:
                yield apply_model([rec], model)[0] if model is not None else rec
            if page.done or page.cursor is None:
                return
            page = self.query_more(page.cursor, page_size=page_size)

    def count(self, soql: str, *values: Any) -> int:
        """``totalSize`` of the scripted answer (``total_size=`` or the number of rows)."""
        soql = soql_values(soql, values)

        def fn() -> int:
            return self._state.query_rows(soql)[1]

        return self._state.run(
            self._who,
            "query",
            _sobject_of(soql),
            {"soql": soql},
            fn,
            method="GET",
            path="query",
            soql=soql,
        )

    def search(self, sosl: str) -> list[dict[str, Any]]:
        """Rows scripted with :meth:`on_search`."""
        return self._state.run(
            self._who,
            "search",
            None,
            {"sosl": sosl},
            lambda: self._state.search(sosl),
            method="GET",
            path="search",
            soql=sosl,
        )

    def parameterized_search(
        self,
        q: str,
        *,
        sobjects: Sequence[str | Mapping[str, Any]] | None = None,
        fields: Sequence[str] | None = None,
        limit: int | None = None,
        **extra: Any,
    ) -> list[dict[str, Any]]:
        """Rows scripted with :meth:`on_search`, matched against ``q``."""
        return self._state.run(
            self._who,
            "parameterized_search",
            None,
            {"q": q, "sobjects": sobjects, "fields": fields, "limit": limit, **extra},
            lambda: self._state.search(q),
            method="POST",
            path="parameterizedSearch",
        )

    # --- raw & limits -----------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool | None = None,
        operation: str = "raw",
    ) -> Any:
        """Dispatch to a handler registered with :meth:`on_request`."""
        if json is not None and content is not None:
            raise ValueError("pass json= or content=, not both")
        req = FakeRequest(
            method.upper(),
            self._state.normalise_path(path),
            dict(params or {}),
            json,
            content,
            dict(headers or {}),
        )
        return self._state.run(
            self._who,
            operation,
            None,
            {"method": req.method, "path": path, "params": req.params, "json": json, "content": content},
            lambda: self._state.handle(req),
            method=req.method,
            path=path,
        )

    def limits(self) -> dict[str, Limit]:
        """Limits set with :meth:`set_limits` (a small default set otherwise)."""
        return self._state.run(
            self._who, "limits", None, {}, self._state.get_limits, method="GET", path="limits"
        )

    def start_limits_poller(self, interval: float = 300.0) -> None:
        """No-op (the fake never polls)."""
        if interval < 300.0:
            raise ValueError("interval must be at least 300 seconds")


# --- async ----------------------------------------------------------------------------


class AsyncFakeSObject:
    """Async twin of :class:`FakeSObject`."""

    def __init__(self, inner: FakeSObject) -> None:
        self._inner = inner
        self.name = inner.name

    def __repr__(self) -> str:
        return f"<AsyncFakeSObject {self.name}>"

    async def get(
        self,
        record_id: str,
        fields: Sequence[str] | None = None,
        *,
        model: Any = None,
        if_modified_since: datetime | None = None,
    ) -> Any:
        """See :meth:`FakeSObject.get`."""
        return self._inner.get(record_id, fields, model=model, if_modified_since=if_modified_since)

    async def get_by_external_id(
        self, field: str, value: str, fields: Sequence[str] | None = None, *, model: Any = None
    ) -> Any:
        """See :meth:`FakeSObject.get_by_external_id`."""
        return self._inner.get_by_external_id(field, value, fields, model=model)

    async def describe(self, *, cached: bool = True) -> dict[str, Any]:
        """See :meth:`FakeSObject.describe`."""
        return self._inner.describe(cached=cached)

    async def metadata(self) -> dict[str, Any]:
        """See :meth:`FakeSObject.metadata`."""
        return self._inner.metadata()

    async def updated(self, start: datetime, end: datetime) -> dict[str, Any]:
        """See :meth:`FakeSObject.updated`."""
        return self._inner.updated(start, end)

    async def deleted(self, start: datetime, end: datetime) -> dict[str, Any]:
        """See :meth:`FakeSObject.deleted`."""
        return self._inner.deleted(start, end)

    async def create(
        self,
        data: Mapping[str, Any],
        *,
        assignment_rule_id: str | bool | None = None,
        allow_save: bool | None = None,
    ) -> str:
        """See :meth:`FakeSObject.create`."""
        return self._inner.create(data, assignment_rule_id=assignment_rule_id, allow_save=allow_save)

    async def update(
        self,
        record_id: str,
        data: Mapping[str, Any],
        *,
        assignment_rule_id: str | bool | None = None,
        allow_save: bool | None = None,
        if_unmodified_since: datetime | None = None,
    ) -> None:
        """See :meth:`FakeSObject.update`."""
        self._inner.update(
            record_id,
            data,
            assignment_rule_id=assignment_rule_id,
            allow_save=allow_save,
            if_unmodified_since=if_unmodified_since,
        )

    async def upsert(
        self,
        field: str,
        value: str,
        data: Mapping[str, Any],
        *,
        assignment_rule_id: str | bool | None = None,
        allow_save: bool | None = None,
    ) -> UpsertResult:
        """See :meth:`FakeSObject.upsert`."""
        return self._inner.upsert(
            field, value, data, assignment_rule_id=assignment_rule_id, allow_save=allow_save
        )

    async def delete(self, record_id: str, *, if_unmodified_since: datetime | None = None) -> None:
        """See :meth:`FakeSObject.delete`."""
        self._inner.delete(record_id, if_unmodified_since=if_unmodified_since)


class AsyncFakeCollections:
    """Async twin of :class:`FakeCollections`."""

    def __init__(self, inner: FakeCollections) -> None:
        self._inner = inner
        self.concurrency = inner.concurrency

    async def create(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        sobject: str | None = None,
        all_or_none: bool = False,
        concurrency: int | None = None,
    ) -> BatchResult[RecordResult]:
        """See :meth:`FakeCollections.create`."""
        return self._inner.create(rows, sobject=sobject, all_or_none=all_or_none, concurrency=concurrency)

    async def update(
        self,
        sobject: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        all_or_none: bool = False,
        concurrency: int | None = None,
    ) -> BatchResult[RecordResult]:
        """See :meth:`FakeCollections.update`."""
        return self._inner.update(sobject, rows, all_or_none=all_or_none, concurrency=concurrency)

    async def upsert(
        self,
        sobject: str,
        field: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        all_or_none: bool = False,
        concurrency: int | None = None,
    ) -> BatchResult[RecordResult]:
        """See :meth:`FakeCollections.upsert`."""
        return self._inner.upsert(sobject, field, rows, all_or_none=all_or_none, concurrency=concurrency)

    async def delete(
        self,
        ids: Iterable[str],
        *,
        all_or_none: bool = False,
        concurrency: int | None = None,
    ) -> BatchResult[RecordResult]:
        """See :meth:`FakeCollections.delete`."""
        return self._inner.delete(ids, all_or_none=all_or_none, concurrency=concurrency)

    async def retrieve(
        self,
        sobject: str,
        ids: Iterable[str],
        fields: Sequence[str],
        *,
        concurrency: int | None = None,
    ) -> list[dict[str, Any] | None]:
        """See :meth:`FakeCollections.retrieve`."""
        return self._inner.retrieve(sobject, ids, fields, concurrency=concurrency)


class AsyncFakeSalesforce(_FakeControls):
    """In-memory stand-in for :class:`even_simpler_salesforce.AsyncSalesforce`.

    Same controls and behaviour as :class:`FakeSalesforce` (they share one implementation);
    the client methods are coroutines and ``query_iter`` is an async iterator.

    Example:
        async with AsyncFakeSalesforce() as fake:
            fake.on_query("SELECT Id FROM Lead", rows=[{"Id": fake.new_id("Lead")}])
            page = await fake.query("SELECT Id FROM Lead")

    Args:
        auth: Ignored; accepted so the fake can be built like the real client.
        api_version: API version used in record URLs and cursors.
        cursor_key: Sign cursors like the real client's ``cursor_key``.
        observers: Event observers.
        budget: Budget guard, applied to the fake's own usage numbers.
        key_prefixes: Override the 3-character id prefix per sObject.
        clock: Returns "now" (timezone-aware) for modification timestamps.
    """

    def __init__(
        self,
        auth: Auth | None = None,
        *,
        api_version: str = DEFAULT_API_VERSION,
        cursor_key: bytes | None = None,
        observers: Iterable[Observer] = (),
        budget: Budget | None = None,
        key_prefixes: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._bind(
            FakeSalesforce(
                auth,
                api_version=api_version,
                cursor_key=cursor_key,
                observers=observers,
                budget=budget,
                key_prefixes=key_prefixes,
                clock=clock,
            )
        )

    def _bind(self, inner: FakeSalesforce) -> None:
        self._sync = inner
        self._state = inner._state
        self._who = inner._who
        self.auth_config = inner.auth_config
        self.collections = AsyncFakeCollections(inner.collections)
        self.auth = AsyncFakeAuthApi(inner.auth)

    @property
    def instance_url(self) -> str:
        """See :attr:`FakeSalesforce.instance_url`."""
        return self._sync.instance_url

    @classmethod
    def _wrap(cls, inner: FakeSalesforce) -> AsyncFakeSalesforce:
        out = cls.__new__(cls)
        out._bind(inner)
        return out

    @property
    def sync(self) -> FakeSalesforce:
        """A sync view over the same records, scripts and call log."""
        return self._sync

    def __repr__(self) -> str:
        return f"<AsyncFakeSalesforce v{self._state.api_version}>"

    async def __aenter__(self) -> AsyncFakeSalesforce:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the fake family (later calls raise ``RuntimeError``). No-op on children."""
        self._sync.close()

    @property
    def closed(self) -> bool:
        """Whether :meth:`aclose` was called on the root fake."""
        return self._state.closed

    def with_auth(
        self, auth: Auth, *, store: Any = None, instance_url: str | None = None
    ) -> AsyncFakeSalesforce:
        """A child sharing the same records, scripts and call log."""
        return type(self)._wrap(self._sync.with_auth(auth, store=store, instance_url=instance_url))

    def tagged(self, caller: str | None = None, priority: Priority | None = None) -> AsyncFakeSalesforce:
        """A child whose calls carry ``caller`` / ``priority``."""
        return type(self)._wrap(self._sync.tagged(caller, priority))

    def sobject(self, name: str) -> AsyncFakeSObject:
        """Operations on one sObject type."""
        return AsyncFakeSObject(self._sync.sobject(name))

    async def describe_global(self, *, cached: bool = True) -> dict[str, Any]:
        """See :meth:`FakeSalesforce.describe_global`."""
        return self._sync.describe_global(cached=cached)

    async def query(
        self,
        soql: str,
        *values: Any,
        page_size: int | None = None,
        include_deleted: bool = False,
        model: Any = None,
    ) -> QueryResult[Any]:
        """See :meth:`FakeSalesforce.query`."""
        return self._sync.query(
            soql, *values, page_size=page_size, include_deleted=include_deleted, model=model
        )

    async def query_one(
        self, soql: str, *values: Any, include_deleted: bool = False, model: Any = None
    ) -> Any:
        """See :meth:`FakeSalesforce.query_one`."""
        return self._sync.query_one(soql, *values, include_deleted=include_deleted, model=model)

    async def query_first(
        self, soql: str, *values: Any, include_deleted: bool = False, model: Any = None
    ) -> Any:
        """See :meth:`FakeSalesforce.query_first`."""
        return self._sync.query_first(soql, *values, include_deleted=include_deleted, model=model)

    async def explain(self, soql: str, *values: Any) -> list[QueryPlan]:
        """See :meth:`FakeSalesforce.explain`."""
        return self._sync.explain(soql, *values)

    async def query_more(
        self, cursor: Cursor | str, *, page_size: int | None = None, model: Any = None
    ) -> QueryResult[Any]:
        """See :meth:`FakeSalesforce.query_more`."""
        return self._sync.query_more(cursor, page_size=page_size, model=model)

    async def query_iter(
        self,
        soql: str,
        *values: Any,
        page_size: int | None = None,
        include_deleted: bool = False,
        model: Any = None,
        expand_children: bool = False,
    ) -> AsyncIterator[Any]:
        """See :meth:`FakeSalesforce.query_iter`."""
        for row in self._sync.query_iter(
            soql,
            *values,
            page_size=page_size,
            include_deleted=include_deleted,
            model=model,
            expand_children=expand_children,
        ):
            yield row

    async def count(self, soql: str, *values: Any) -> int:
        """See :meth:`FakeSalesforce.count`."""
        return self._sync.count(soql, *values)

    async def search(self, sosl: str) -> list[dict[str, Any]]:
        """See :meth:`FakeSalesforce.search`."""
        return self._sync.search(sosl)

    async def parameterized_search(
        self,
        q: str,
        *,
        sobjects: Sequence[str | Mapping[str, Any]] | None = None,
        fields: Sequence[str] | None = None,
        limit: int | None = None,
        **extra: Any,
    ) -> list[dict[str, Any]]:
        """See :meth:`FakeSalesforce.parameterized_search`."""
        return self._sync.parameterized_search(q, sobjects=sobjects, fields=fields, limit=limit, **extra)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool | None = None,
        operation: str = "raw",
    ) -> Any:
        """See :meth:`FakeSalesforce.request`."""
        return self._sync.request(
            method,
            path,
            params=params,
            json=json,
            content=content,
            headers=headers,
            idempotent=idempotent,
            operation=operation,
        )

    async def limits(self) -> dict[str, Limit]:
        """See :meth:`FakeSalesforce.limits`."""
        return self._sync.limits()

    def start_limits_poller(self, interval: float = 300.0) -> None:
        """No-op (the fake never polls)."""
        self._sync.start_limits_poller(interval)
