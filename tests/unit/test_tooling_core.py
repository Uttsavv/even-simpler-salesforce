"""``core/tooling.py``: anonymous Apex results, trace flag bodies, log matching. No I/O.

Field names (all responses here are **synthetic**, big-bang.md §21 V17, V23, V45-V47):

- ``ExecuteAnonymousResult``:
  https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_calls_executeanonymous_result.htm
- ``TraceFlag``:
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/tooling_api_objects_traceflag.htm
- ``DebugLevel``:
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/tooling_api_objects_debuglevel.htm
- ``ApexLog``:
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/tooling_api_objects_apexlog.htm
- ``runTestsAsynchronous``:
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/intro_rest_resources_testing_runner_async.htm
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core import tooling as T

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
USER = "005xx0000000001AAA"
LEVEL = "7dlxx0000000001AAA"


def test_check_apex_code() -> None:
    assert T.check_apex_code("System.debug(1);") == "System.debug(1);"
    for bad in ("", "  \n", None, 3):
        with pytest.raises(ValueError, match="non-empty"):
            T.check_apex_code(bad)  # type: ignore[arg-type]


def test_check_completion_type() -> None:
    assert T.check_completion_type("apex") == "apex"
    assert T.check_completion_type("visualforce") == "visualforce"
    with pytest.raises(ValueError, match="apex, visualforce; got 'Apex'"):
        T.check_completion_type("Apex")


def test_parse_anonymous_success_maps_minus_one_to_none() -> None:
    body = {
        "line": -1,
        "column": -1,
        "compiled": True,
        "success": True,
        "compileProblem": None,
        "exceptionStackTrace": None,
        "exceptionMessage": None,
    }
    assert T.parse_anonymous(body) == esf.AnonymousResult(compiled=True, success=True)
    assert T.parse_anonymous(body, log="L").log == "L"


def test_parse_anonymous_failure_fields() -> None:
    result = T.parse_anonymous(
        {
            "line": 3,
            "column": 7,
            "compiled": True,
            "success": False,
            "compileProblem": "",
            "exceptionMessage": "System.NullPointerException: x",
            "exceptionStackTrace": "AnonymousBlock: line 3, column 7",
        }
    )
    assert (result.line, result.column) == (3, 7)
    assert result.compile_problem is None  # empty string → None
    assert result.exception_message == "System.NullPointerException: x"
    assert result.exception_stack_trace == "AnonymousBlock: line 3, column 7"


@pytest.mark.parametrize("value", [True, "3", 2.0, None])
def test_parse_anonymous_ignores_non_int_positions(value: Any) -> None:
    result = T.parse_anonymous({"compiled": False, "success": False, "line": value, "column": value})
    assert result.line is None and result.column is None


@pytest.mark.parametrize(
    "body",
    [[], "x", None, {"compiled": True}, {"compiled": "true", "success": True}, {"compiled": True, "success": 1}],
)
def test_parse_anonymous_rejects_bad_bodies(body: Any) -> None:
    with pytest.raises(esf.UnexpectedResponse) as ei:
        T.parse_anonymous(body)
    assert ei.value.operation == "tooling.execute_anonymous"


def test_debug_level_body() -> None:
    body = T.debug_level_body()
    assert body["DeveloperName"] == body["MasterLabel"] == T.DEBUG_LEVEL_NAME
    assert body["ApexCode"] == "DEBUG"
    assert {k for k, v in body.items() if v not in ("INFO", "DEBUG")} == {"DeveloperName", "MasterLabel"}


def test_trace_flag_body() -> None:
    assert T.trace_flag_body(USER, LEVEL, NOW) == {
        "TracedEntityId": USER,
        "DebugLevelId": LEVEL,
        "LogType": "USER_DEBUG",
        "StartDate": "2030-01-01T12:00:00Z",
        "ExpirationDate": "2030-01-01T12:10:00Z",
    }
    assert T.TRACE_FLAG_TTL < timedelta(hours=24)  # Salesforce caps a flag at 24 hours
    with pytest.raises(ValueError):
        T.trace_flag_body("005'", LEVEL, NOW)


def _log(rid: str, op: Any, start: Any) -> dict[str, Any]:
    return {"Id": rid, "Operation": op, "StartTime": start}


def test_pick_log_takes_the_newest_anonymous_log_after_the_start() -> None:
    rows = [
        _log("07Lxx0000000001AAA", "/services/data/v68.0/tooling/executeAnonymous/", "2030-01-01T11:58:00.000+0000"),
        _log("07Lxx0000000002AAA", "/services/data/v68.0/tooling/executeAnonymous/", "2030-01-01T12:00:01.000+0000"),
        _log("07Lxx0000000003AAA", "/services/data/v68.0/tooling/executeAnonymous/", "2030-01-01T11:59:30.000Z"),
        _log("07Lxx0000000004AAA", "Api", "2030-01-01T12:00:05.000+0000"),
    ]
    assert T.pick_log(rows, NOW) == "07Lxx0000000002AAA"


def test_pick_log_slack_and_no_match() -> None:
    within = _log("07Lxx0000000001AAA", "executeAnonymous", "2030-01-01T11:59:30+00:00")
    assert T.pick_log([within], NOW) == "07Lxx0000000001AAA"  # clock skew up to LOG_MATCH_SLACK
    too_old = _log("07Lxx0000000001AAA", "executeAnonymous", "2030-01-01T11:58:59.000+0000")
    assert T.pick_log([too_old], NOW) is None
    assert T.pick_log([], NOW) is None


@pytest.mark.parametrize(
    "row",
    [
        _log("07Lxx0000000001AAA", None, "2030-01-01T12:00:01Z"),
        _log("07Lxx0000000001AAA", "executeAnonymous", None),
        _log("07Lxx0000000001AAA", "executeAnonymous", "yesterday"),
        _log("07Lxx0000000001AAA", "executeAnonymous", "2030-01-01T12:00:01"),  # no time zone
        _log(None, "executeAnonymous", "2030-01-01T12:00:01Z"),  # type: ignore[arg-type]
    ],
)
def test_pick_log_skips_unusable_rows(row: dict[str, Any]) -> None:
    assert T.pick_log([row], NOW) is None


def test_run_tests_body() -> None:
    assert T.run_tests_body(["01pxx0000000002AAA", "01pxx0000000004"]) == {
        "classids": "01pxx0000000002AAA,01pxx0000000004"
    }
    with pytest.raises(TypeError, match="not one string"):
        T.run_tests_body("01pxx0000000002AAA")
    with pytest.raises(ValueError, match="empty"):
        T.run_tests_body([])
    with pytest.raises(ValueError):
        T.run_tests_body(["01p'x"])


@pytest.mark.parametrize("body", ["707xx0000000001", '"707xx0000000001"', b"707xx0000000001\n", ' "707xx0000000001" '])
def test_parse_job_id(body: Any) -> None:
    assert T.parse_job_id(body) == "707xx0000000001"


@pytest.mark.parametrize("body", ["", b"", None, {"id": "707xx0000000001"}, "not an id"])
def test_parse_job_id_rejects(body: Any) -> None:
    with pytest.raises(esf.UnexpectedResponse) as ei:
        T.parse_job_id(body)
    assert ei.value.operation == "tooling.run_tests_async"
