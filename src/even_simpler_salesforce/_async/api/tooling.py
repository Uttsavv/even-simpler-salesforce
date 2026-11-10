"""Tooling API: ``sf.tooling`` (§9.12) and anonymous Apex (§9.7)."""

from __future__ import annotations

import dataclasses
import warnings
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from ...core.http import RequestSpec, check_record_id
from ...core.soql import soql_values
from ...core.tooling import (
    DEBUG_LEVEL_NAME,
    LOG_MATCH_SLACK,
    check_apex_code,
    check_completion_type,
    debug_level_body,
    parse_anonymous,
    parse_job_id,
    pick_log,
    run_tests_body,
    trace_flag_body,
)
from ...errors import SalesforceError, UnexpectedResponse
from ...models import AnonymousResult, QueryResult
from .sobject import AsyncSObject

if TYPE_CHECKING:
    from ..client import AsyncSalesforce

__all__ = ["AsyncTooling"]

M = TypeVar("M")
_BASE = "tooling/"


class AsyncTooling:
    """The Tooling API under ``/services/data/vXX.X/tooling/``.

    ``query``, ``query_iter`` and ``sobject`` work like their REST API twins on the client,
    for Tooling objects such as ``ApexClass``, ``TraceFlag`` or ``ApexLog``.
    ``execute_anonymous`` runs anonymous Apex and is never resent after it may have reached
    Salesforce, because the code could run twice.
    """

    def __init__(self, client: AsyncSalesforce) -> None:
        self._sf = client

    # --- query & records --------------------------------------------------------------

    @overload
    async def query(self, soql: str, *values: Any, model: None = None) -> QueryResult[dict[str, Any]]: ...
    @overload
    async def query(self, soql: str, *values: Any, model: type[M]) -> QueryResult[M]: ...
    async def query(self, soql: str, *values: Any, model: Any = None) -> QueryResult[Any]:
        """``GET tooling/query?q=``: one page plus a cursor.

        Example:
            page = await sf.tooling.query("SELECT Id, Name FROM ApexClass WHERE Name = {}", name)

        Args:
            soql: The query. ``{}`` placeholders are filled with ``values``, quoted safely.
            *values: Values for the ``{}`` placeholders (same quoting as ``esf.soql``).
            model: Optional Pydantic model or dataclass for the rows.

        Returns:
            The first page. Pass its ``cursor`` to ``sf.query_more`` for the next one.
        """
        soql = soql_values(soql, values)
        return self._sf._page(await self._sf._query_body(soql, None, False, _BASE), model)

    def query_iter(self, soql: str, *values: Any, model: Any = None) -> AsyncIterator[Any]:
        """Yield every row of a Tooling query, fetching pages lazily.

        Args:
            soql: The query; ``values`` fill its ``{}`` placeholders as in :meth:`query`.
            *values: Values for the ``{}`` placeholders.
            model: Optional Pydantic model or dataclass for the rows.
        """
        soql = soql_values(soql, values)
        return self._sf._rows(soql, None, False, model, False, _BASE)

    def sobject(self, name: str) -> AsyncSObject:
        """Operations on one Tooling object, with the methods of ``sf.sobject(name)``.

        Example:
            cls = await sf.tooling.sobject("ApexClass").get("01pxx0000000001AAA")
        """
        return AsyncSObject(self._sf, name, base=_BASE)

    # --- anonymous Apex ---------------------------------------------------------------

    async def execute_anonymous(self, code: str, *, capture_log: bool = False) -> AnonymousResult:
        """Run anonymous Apex with ``GET tooling/executeAnonymous``.

        The client never resends this call after it may have reached Salesforce. Anonymous
        Apex can't return values; write to the debug log and use ``capture_log=True``.

        Example:
            result = await sf.tooling.execute_anonymous("System.debug('hi');", capture_log=True)

        Args:
            code: The Apex statements.
            capture_log: Create a short-lived ``TraceFlag`` for the running user, run the code,
                read the new ``ApexLog`` into ``result.log`` and delete the trace flag, also
                when the run fails. This costs about six more API calls, counted in usage.

        Returns:
            Whether the code compiled and ran, with the compile problem or exception.

        Raises:
            RequestTooLarge: the code is too long for the URL (about 16 KB after encoding).
        """
        check_apex_code(code)
        if not capture_log:
            return parse_anonymous(await self._run(code))
        user_id = await self._user_id()
        flag_id = await self._start_trace(user_id)
        started = self._sf._transport.now()
        try:
            result = parse_anonymous(await self._run(code))
            log = await self._read_log(user_id, started)
        except BaseException:
            await self._end_trace(flag_id, failed=True)
            raise
        await self._end_trace(flag_id, failed=False)
        return dataclasses.replace(result, log=log)

    async def _run(self, code: str) -> Any:
        raw = await self._sf._send(
            RequestSpec(
                "GET",
                "tooling/executeAnonymous/",
                params={"anonymousBody": code},
                idempotent=False,  # runs code: never resent after it may have reached Salesforce
                operation="tooling.execute_anonymous",
            )
        )
        return raw.json()

    async def _user_id(self) -> str:
        tok = await self._sf._runtime.token()
        if tok.user_id:
            return check_record_id(tok.user_id)
        info = await self._sf.auth.userinfo()  # StaticToken: no identity URL in the token
        uid: Any = info.get("user_id") if isinstance(info, dict) else None  # pyright: ignore[reportUnnecessaryIsInstance]
        if not isinstance(uid, str):
            raise UnexpectedResponse("userinfo returned no user_id", operation="oauth.userinfo")
        return check_record_id(uid)

    async def _start_trace(self, user_id: str) -> str | None:
        """Create a ``USER_DEBUG`` trace flag; ``None`` when the user already has an active one."""
        now = self._sf._transport.now()
        active = await self.query(
            "SELECT Id FROM TraceFlag WHERE TracedEntityId = {} AND LogType = 'USER_DEBUG' "
            "AND ExpirationDate > {} LIMIT 1",
            user_id,
            now,
        )
        if active.records:
            return None  # Salesforce allows one active flag per user; it isn't ours to delete
        levels = await self.query(
            "SELECT Id FROM DebugLevel WHERE DeveloperName = {} LIMIT 1", DEBUG_LEVEL_NAME
        )
        if levels.records:
            level_id = str(levels.records[0]["Id"])
        else:
            level_id = await self.sobject("DebugLevel").create(debug_level_body())
        return await self.sobject("TraceFlag").create(trace_flag_body(user_id, level_id, now))

    async def _end_trace(self, flag_id: str | None, *, failed: bool) -> None:
        if flag_id is None:
            return
        try:
            await self.sobject("TraceFlag").delete(flag_id)
        except SalesforceError as exc:
            if failed:
                return  # keep the run's own error; the flag expires within minutes anyway
            warnings.warn(
                f"could not delete TraceFlag {flag_id} ({type(exc).__name__}: {exc.message}); "
                "it expires within 10 minutes",
                RuntimeWarning,
                stacklevel=3,
            )

    async def _read_log(self, user_id: str, started: datetime) -> str | None:
        """The body of the run's ``ApexLog``, or ``None`` (with a warning) if it can't be read."""
        try:
            page = await self.query(
                "SELECT Id, Operation, StartTime FROM ApexLog WHERE LogUserId = {} "
                "AND StartTime >= {} ORDER BY StartTime DESC LIMIT 20",
                user_id,
                started - LOG_MATCH_SLACK,
            )
            log_id = pick_log(page.records, started)
            if log_id is None:
                return None
            raw = await self._sf._send(
                RequestSpec(
                    "GET",
                    f"tooling/sobjects/ApexLog/{check_record_id(log_id)}/Body",
                    idempotent=True,
                    operation="tooling.apex_log",
                    sobject="ApexLog",
                    expect_json=False,
                )
            )
        except SalesforceError as exc:
            # The code already ran: raising here would invite a second run.
            warnings.warn(
                f"anonymous Apex ran, but its debug log could not be read "
                f"({type(exc).__name__}: {exc.message})",
                RuntimeWarning,
                stacklevel=3,
            )
            return None
        return raw.content.decode("utf-8", errors="replace")

    # --- tests & completions ----------------------------------------------------------

    async def run_tests_async(self, class_ids: Sequence[str]) -> str:
        """``POST tooling/runTestsAsynchronous``: queue the tests of Apex test classes.

        Args:
            class_ids: Ids of the Apex test classes (``ApexClass`` ids, ``01p...``).

        Returns:
            The ``AsyncApexJob`` id. Query ``ApexTestResult`` with ``AsyncApexJobId`` for the
            outcome.
        """
        content, ctype = self._sf._transport.encode(run_tests_body(class_ids))
        raw = await self._sf._send(
            RequestSpec(
                "POST",
                "tooling/runTestsAsynchronous/",
                content=content,
                content_type=ctype,
                operation="tooling.run_tests_async",
                expect_json=False,
            )
        )
        return parse_job_id(raw.json() if raw.is_json else raw.content)

    async def completions(self, type: Literal["apex", "visualforce"]) -> dict[str, Any]:
        """``GET tooling/completions?type=``: the symbols editors offer for code completion.

        Args:
            type: ``"apex"`` (system classes and methods) or ``"visualforce"`` (components).
        """
        raw = await self._sf._send(
            RequestSpec(
                "GET",
                "tooling/completions",
                params={"type": check_completion_type(type)},
                idempotent=True,
                operation="tooling.completions",
            )
        )
        body: Any = raw.json()
        if not isinstance(body, dict):
            raise UnexpectedResponse("unexpected completions response", operation="tooling.completions")
        return body  # pyright: ignore[reportUnknownVariableType]
