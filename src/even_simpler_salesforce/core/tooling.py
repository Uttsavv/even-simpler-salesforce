"""Tooling API (§9.12): anonymous Apex results, debug-log capture, test runs. No I/O.

Sources (Tooling API Developer Guide; developer.salesforce.com refuses non-browser clients,
so the field lists come from search excerpts and are tracked in big-bang.md §21, V17/V23/V45):

- ``executeAnonymous`` REST resource and the ``ExecuteAnonymousResult`` fields
  (``compiled``, ``compileProblem``, ``success``, ``exceptionMessage``,
  ``exceptionStackTrace``, ``line``, ``column``):
  https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_calls_executeanonymous_result.htm
- ``TraceFlag`` (``LogType`` ``USER_DEBUG``, ``TracedEntityId``, ``DebugLevelId``,
  ``StartDate``, ``ExpirationDate`` less than 24 hours after the start; one active flag per
  traced entity):
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/tooling_api_objects_traceflag.htm
- ``DebugLevel`` (``DeveloperName``, ``MasterLabel`` required, one level per log category):
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/tooling_api_objects_debuglevel.htm
- ``ApexLog`` (``LogUserId``, ``Operation``, ``StartTime``; body at
  ``sobjects/ApexLog/{id}/Body``):
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/tooling_api_objects_apexlog.htm
- ``runTestsAsynchronous`` (POST, ``classids`` comma-separated):
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/intro_rest_resources_testing_runner_async.htm
- ``completions?type=apex|visualforce``:
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/intro_rest_resources.htm
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, cast

from ..errors import UnexpectedResponse
from ..models import AnonymousResult
from .http import check_record_id
from .records import format_sf_datetime

__all__ = [
    "COMPLETION_TYPES",
    "DEBUG_LEVEL_NAME",
    "LOG_MATCH_SLACK",
    "TRACE_FLAG_TTL",
    "check_apex_code",
    "check_completion_type",
    "debug_level_body",
    "parse_anonymous",
    "parse_job_id",
    "pick_log",
    "run_tests_body",
    "trace_flag_body",
]

COMPLETION_TYPES = ("apex", "visualforce")
"""Values Salesforce documents for ``completions?type=``."""

DEBUG_LEVEL_NAME = "esf_anonymous_apex"
"""``DeveloperName`` of the ``DebugLevel`` the client creates once and then reuses."""

TRACE_FLAG_TTL = timedelta(minutes=10)
"""Lifetime of the client's ``TraceFlag``. The client deletes it at once; this is the fallback."""

LOG_MATCH_SLACK = timedelta(seconds=60)
"""How far before the local start time a log may start (clock difference with Salesforce)."""

# Category levels of the client's DebugLevel. ``ApexCode=DEBUG`` keeps ``System.debug`` lines
# (whose default level is DEBUG); the other categories stay quiet so the log stays small.
_LEVELS = {
    "ApexCode": "DEBUG",
    "ApexProfiling": "INFO",
    "Callout": "INFO",
    "Database": "INFO",
    "System": "INFO",
    "Validation": "INFO",
    "Visualforce": "INFO",
    "Workflow": "INFO",
}


def check_apex_code(code: str) -> str:
    """Validate the anonymous Apex body: a non-blank string."""
    if not isinstance(code, str) or not code.strip():  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("code must be a non-empty string of Apex")
    return code


def check_completion_type(kind: str) -> str:
    """Validate ``completions(type)``: ``"apex"`` or ``"visualforce"``."""
    if kind not in COMPLETION_TYPES:
        raise ValueError(f"type must be one of {', '.join(COMPLETION_TYPES)}; got {kind!r}")
    return kind


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None  # Salesforce sends -1 when there is no position


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_anonymous(body: Any, log: str | None = None) -> AnonymousResult:
    """Build an :class:`AnonymousResult` from an ``executeAnonymous`` response body.

    Raises:
        UnexpectedResponse: the body is not an object with boolean ``compiled`` and ``success``.
    """
    if not isinstance(body, dict):
        raise UnexpectedResponse(
            "unexpected executeAnonymous response",
            raw=repr(body)[:4096],
            operation="tooling.execute_anonymous",
        )
    data = cast("dict[str, Any]", body)
    compiled, success = data.get("compiled"), data.get("success")
    if not isinstance(compiled, bool) or not isinstance(success, bool):
        raise UnexpectedResponse(
            "executeAnonymous response has no boolean 'compiled' and 'success'",
            raw=repr(data)[:4096],
            operation="tooling.execute_anonymous",
        )
    return AnonymousResult(
        compiled=compiled,
        success=success,
        compile_problem=_text(data.get("compileProblem")),
        exception_message=_text(data.get("exceptionMessage")),
        exception_stack_trace=_text(data.get("exceptionStackTrace")),
        line=_int(data.get("line")),
        column=_int(data.get("column")),
        log=log,
    )


def debug_level_body() -> dict[str, Any]:
    """Fields of the client's ``DebugLevel``."""
    return {"DeveloperName": DEBUG_LEVEL_NAME, "MasterLabel": DEBUG_LEVEL_NAME, **_LEVELS}


def trace_flag_body(user_id: str, debug_level_id: str, now: datetime) -> dict[str, Any]:
    """Fields of a short-lived ``USER_DEBUG`` trace flag for ``user_id``."""
    return {
        "TracedEntityId": check_record_id(user_id),
        "DebugLevelId": check_record_id(debug_level_id),
        "LogType": "USER_DEBUG",
        "StartDate": format_sf_datetime(now),
        "ExpirationDate": format_sf_datetime(now + TRACE_FLAG_TTL),
    }


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = f"{text[:-2]}:{text[-2:]}"  # +0000 -> +00:00 (Python 3.10 fromisoformat)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def pick_log(rows: Sequence[Mapping[str, Any]], started: datetime) -> str | None:
    """Choose the ``ApexLog`` of one anonymous run from the user's recent logs.

    A log matches when its ``Operation`` names ``executeAnonymous`` and its ``StartTime`` is
    not earlier than ``started`` minus :data:`LOG_MATCH_SLACK`. The newest match wins: the
    run started after the client created the trace flag, and older anonymous runs started
    before it.

    Returns:
        The log id, or ``None`` when no log matches.
    """
    best: tuple[datetime, str] | None = None
    floor = started - LOG_MATCH_SLACK
    for row in rows:
        op = row.get("Operation")
        when = _parse_time(row.get("StartTime"))
        rid = row.get("Id")
        if not isinstance(op, str) or "executeanonymous" not in op.lower():
            continue
        if when is None or when < floor or not isinstance(rid, str):
            continue
        if best is None or when > best[0]:
            best = (when, rid)
    return best[1] if best else None


def run_tests_body(class_ids: Sequence[str]) -> dict[str, Any]:
    """``runTestsAsynchronous`` body for a list of Apex test class ids."""
    if isinstance(class_ids, str):
        raise TypeError("class_ids must be a list of ids, not one string")
    ids = [check_record_id(i) for i in class_ids]
    if not ids:
        raise ValueError("class_ids must not be empty")
    return {"classids": ",".join(ids)}


def parse_job_id(body: Any) -> str:
    """The ``AsyncApexJob`` id from a ``runTestsAsynchronous`` response.

    Salesforce returns the id as a JSON string. Plain text is accepted too.

    Raises:
        UnexpectedResponse: the body holds no job id.
    """
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    text = body.strip().strip('"') if isinstance(body, str) else ""
    try:
        return check_record_id(text)
    except ValueError:
        raise UnexpectedResponse(
            "runTestsAsynchronous returned no job id",
            raw=repr(body)[:4096],
            operation="tooling.run_tests_async",
        ) from None
