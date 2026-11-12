"""Tooling API: ``sf.tooling`` (§9.12) and anonymous Apex (§9.7), on both clients.

Responses come from ``tests/fixtures/recorded/tooling``. Most are **recorded** from a Developer
Edition org (API v68.0) with ``esf.testing.record`` on 2026-11-11 (``tests/sandbox/
test_tooling_metadata.py``; big-bang.md §21 V17, V23, V45-V47). Still **synthetic**:
``query_page1.json``/``query_page2.json`` (a paged Tooling query needs more than 2,000 rows,
which the org doesn't have) and the inline bodies marked so below. developer.salesforce.com
refuses non-browser clients, so the pages were read through search excerpts:

- REST resources (``query``, ``sobjects``, ``executeAnonymous``, ``completions``):
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/intro_rest_resources.htm
- ``ExecuteAnonymousResult`` fields:
  https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_calls_executeanonymous_result.htm
- ``TraceFlag`` (``USER_DEBUG``, one active flag per traced entity, expiry under 24 hours):
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/tooling_api_objects_traceflag.htm
- ``DebugLevel`` (``DeveloperName``, ``MasterLabel`` and the category levels):
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/tooling_api_objects_debuglevel.htm
- ``ApexLog`` (``LogUserId``, ``Operation``, ``StartTime``; body at ``sobjects/ApexLog/{id}/Body``):
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/tooling_api_objects_apexlog.htm
- ``runTestsAsynchronous`` (POST ``classids``; returns the AsyncApexJob id):
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/intro_rest_resources_testing_runner_async.htm
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import replay

from ..conftest import DATA, INSTANCE, USER_ID, Harness, Recorder
from ..fixtures import RECORDED, load_fixture, load_response

TOOLING = f"{DATA}/tooling"
ANON = f"{TOOLING}/executeAnonymous/"
# ``tooling/capture_log.json``: the client created its TraceFlag at NOW; the run's ApexLog
# started two seconds later (StartTime has whole seconds only).
NOW = datetime(2026, 11, 11, 22, 48, 30, tzinfo=timezone.utc)
CAPTURE = "tooling/capture_log.json"
LEVEL_ID = "7dl000000000001AAA"
FLAG_ID = "7tf000000000003AAA"
LOG_ID = "07L000000000004EAA"
PAGE1 = load_fixture("tooling/query_page1.json")  # synthetic


def _empty(entity: str) -> dict[str, Any]:
    return {"size": 0, "totalSize": 0, "done": True, "entityTypeName": entity, "records": []}


def _client(h: Harness, rec: Recorder | None = None, **kw: Any) -> Any:
    sf = h.client(recorder=rec, **kw)
    sf._transport.now = lambda: NOW
    return sf


# --- query & records -------------------------------------------------------------------


def test_query_uses_the_tooling_path_and_query_more_follows_its_cursor(  # synthetic pages
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    route = mock.get(f"{TOOLING}/query").respond(json=PAGE1)
    more = mock.get(f"{TOOLING}/query/01gxx0000000001AAA-2").respond(
        json=load_fixture("tooling/query_page2.json")
    )
    sf = _client(h, rec)
    page = h.run(sf.tooling.query("SELECT Id, Name FROM ApexClass WHERE Name LIKE {}", "Order%"))
    assert [r["Name"] for r in page] == ["OrderService", "OrderServiceTest"]
    assert page.total_size == 3
    assert not page.done
    assert page.cursor is not None
    assert route.calls[0].request.url.params["q"] == "SELECT Id, Name FROM ApexClass WHERE Name LIKE 'Order%'"
    assert "sforce-query-options" not in route.calls[0].request.headers
    nxt = h.run(sf.query_more(page.cursor))
    assert [r["Name"] for r in nxt] == ["LeadScoring"]
    assert nxt.cursor is None
    assert more.called
    ops = [(e["operation"], e["sobject"], e["path_template"]) for e in rec.requests]
    assert ops == [
        ("tooling.query", "ApexClass", "/tooling/query"),
        ("tooling.query_more", None, "/tooling/query/:locator"),
    ]


def test_query_iter_reads_every_page(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{TOOLING}/query").respond(json=PAGE1)
    mock.get(f"{TOOLING}/query/01gxx0000000001AAA-2").respond(json=load_fixture("tooling/query_page2.json"))
    sf = _client(h)
    rows = h.collect(sf.tooling.query_iter("SELECT Id, Name FROM ApexClass"))
    assert [r["Id"] for r in rows] == ["01pxx0000000001AAA", "01pxx0000000002AAA", "01pxx0000000003AAA"]


def test_query_model(h: Harness, mock: respx.MockRouter) -> None:
    from dataclasses import dataclass

    @dataclass
    class Cls:
        Id: str
        Name: str

    mock.get(f"{TOOLING}/query").respond(json=load_fixture("tooling/query_page2.json"))
    sf = _client(h)
    assert h.run(sf.tooling.query("SELECT Id, Name FROM ApexClass", model=Cls)).records == [
        Cls("01pxx0000000003AAA", "LeadScoring")
    ]
    assert (
        h.collect(sf.tooling.query_iter("SELECT Id, Name FROM ApexClass", model=Cls))[0].Name == "LeadScoring"
    )


def test_tooling_cursor_shape_is_validated(h: Harness) -> None:
    sf = _client(h)
    forged = esf.Cursor("L3NlcnZpY2VzL2RhdGEvdjY4LjAvdG9vbGluZy9zb2JqZWN0cy9Vc2Vy")  # tooling/sobjects/User
    with pytest.raises(esf.MalformedQuery):
        h.run(sf.query_more(forged))


def test_sobject_methods_use_tooling_paths(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    base = f"{TOOLING}/sobjects/ApexClass"
    get = mock.get(f"{base}/01pxx0000000001AAA").respond(
        json={"Id": "01pxx0000000001AAA", "Name": "OrderService"}
    )
    create = mock.post(f"{base}/").respond(
        201, json={"id": "01pxx0000000009AAA", "success": True, "errors": []}
    )
    update = mock.patch(f"{base}/01pxx0000000009AAA").respond(204)
    delete = mock.delete(f"{base}/01pxx0000000009AAA").respond(204)
    describe = mock.get(f"{base}/describe").respond(json={"name": "ApexClass", "fields": [{"name": "Body"}]})
    sf = _client(h, rec)
    cls = sf.tooling.sobject("ApexClass")
    assert h.run(cls.get("01pxx0000000001AAA"))["Name"] == "OrderService"
    assert h.run(cls.create({"Name": "Tmp", "Body": "public class Tmp {}"})) == "01pxx0000000009AAA"
    h.run(cls.update("01pxx0000000009AAA", {"Body": "public class Tmp { }"}))
    h.run(cls.delete("01pxx0000000009AAA"))
    assert h.run(cls.describe())["name"] == "ApexClass"
    assert h.run(cls.describe())["name"] == "ApexClass"  # cached
    assert describe.call_count == 1
    assert all(r.called for r in (get, create, update, delete))
    assert [e["operation"] for e in rec.requests] == [
        "tooling.sobject.get",
        "tooling.sobject.create",
        "tooling.sobject.update",
        "tooling.sobject.delete",
        "tooling.sobject.describe",
    ]
    assert rec.requests[0]["path_template"] == "/tooling/sobjects/ApexClass/:id"


def test_tooling_describe_does_not_share_the_rest_cache(h: Harness, mock: respx.MockRouter) -> None:
    rest = mock.get(f"{DATA}/sobjects/User/describe").respond(json={"name": "User", "api": "rest"})
    tool = mock.get(f"{TOOLING}/sobjects/User/describe").respond(json={"name": "User", "api": "tooling"})
    sf = _client(h)
    assert h.run(sf.sobject("User").describe())["api"] == "rest"
    assert h.run(sf.tooling.sobject("User").describe())["api"] == "tooling"
    assert rest.called
    assert tool.called


def test_describe_global_is_cached_apart_from_the_rest_one(
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    """``GET tooling/sobjects`` lists the Tooling objects (*Tooling API REST resources*,
    ``intro_rest_resources.htm``). Synthetic body in the shape of the REST ``sobjects`` list."""
    body = {
        "encoding": "UTF-8",
        "maxBatchSize": 200,
        "sobjects": [{"name": "ApexClass"}, {"name": "TraceFlag"}],
    }
    tool = mock.get(f"{TOOLING}/sobjects").respond(json=body)
    rest = mock.get(f"{DATA}/sobjects").respond(json={"sobjects": [{"name": "Lead"}]})
    sf = _client(h, rec)
    got = h.run(sf.tooling.describe_global())
    assert [o["name"] for o in got["sobjects"]] == ["ApexClass", "TraceFlag"]
    got["sobjects"].clear()  # callers get a copy, not the cached dict
    assert len(h.run(sf.tooling.describe_global())["sobjects"]) == 2
    assert tool.call_count == 1  # second call served from the cache
    assert h.run(sf.describe_global())["sobjects"] == [{"name": "Lead"}]
    assert rest.call_count == 1
    h.run(sf.tooling.describe_global(cached=False))
    assert tool.call_count == 2
    ops = [e.fields["operation"] for e in rec.requests]
    assert ops == ["tooling.describe_global", "describe_global", "tooling.describe_global"]


def test_sobject_external_id_path_template(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(f"{TOOLING}/sobjects/ApexClass/Name/Tmp").respond(json={"Id": "01pxx0000000009AAA"})
    sf = _client(h, rec)
    assert h.run(sf.tooling.sobject("ApexClass").get_by_external_id("Name", "Tmp"))["Id"]
    assert rec.requests[0]["path_template"] == "/tooling/sobjects/ApexClass/Name/:value"
    assert rec.requests[0]["operation"] == "tooling.sobject.get_by_external_id"


def test_create_without_id_names_the_tooling_operation(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(f"{TOOLING}/sobjects/ApexClass/").respond(201, json={"success": True})
    sf = _client(h)
    with pytest.raises(esf.UnexpectedResponse) as ei:
        h.run(sf.tooling.sobject("ApexClass").create({"Name": "Tmp"}))
    assert ei.value.operation == "tooling.sobject.create"


# --- anonymous Apex --------------------------------------------------------------------


def test_execute_anonymous_success(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.get(ANON).respond(json=load_fixture("tooling/execute_ok.json"))
    sf = _client(h, rec)
    code = "Account a = new Account(Name = 'x & y');\ninsert a;"
    result = h.run(sf.tooling.execute_anonymous(code))
    # Recorded: line and column are -1 and the empty fields are null when nothing failed.
    assert load_fixture("tooling/execute_ok.json")["line"] == -1
    assert result == esf.AnonymousResult(compiled=True, success=True)
    assert route.calls[0].request.url.params["anonymousBody"] == code
    (event,) = rec.requests
    assert event["operation"] == "tooling.execute_anonymous"


def test_execute_anonymous_compile_error(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(ANON).respond(json=load_fixture("tooling/execute_compile_error.json"))
    result = h.run(_client(h).tooling.execute_anonymous("Integer i = 1;\nSystem debug('x');"))
    assert not result.compiled
    assert not result.success
    assert result.compile_problem == "Unexpected token 'debug'."
    assert (result.line, result.column) == (2, 8)
    assert result.exception_message is None
    assert result.exception_stack_trace is None


def test_execute_anonymous_exception(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(ANON).respond(json=load_fixture("tooling/execute_exception.json"))
    result = h.run(_client(h).tooling.execute_anonymous("Integer a = 0;\nInteger i = 1 / a;"))
    assert result.compiled
    assert not result.success
    assert result.exception_message == "System.MathException: Divide by 0"
    assert result.exception_stack_trace == "AnonymousBlock: line 2, column 1"
    assert (result.line, result.column) == (2, 1)
    assert result.compile_problem is None


@pytest.mark.parametrize(
    ("fixture", "status"),
    [("tooling/execute_uri_too_long.json", 414), ("tooling/execute_headers_too_large.json", 431)],
)
def test_execute_anonymous_too_long_is_request_too_large(
    h: Harness, mock: respx.MockRouter, fixture: str, status: int
) -> None:
    """Recorded (V45): 30,000 and 20,000 characters of code got an HTML 414 / 431 page."""
    recorded = load_response(fixture)
    assert recorded["status"] == status
    route = mock.get(ANON).respond(
        status, text=recorded["text"], headers={"content-type": recorded["headers"]["content-type"]}
    )
    with pytest.raises(esf.RequestTooLarge) as ei:
        h.run(_client(h).tooling.execute_anonymous("System.debug(1);//" + "x" * 30_000))
    assert ei.value.status == status
    assert ei.value.operation == "tooling.execute_anonymous"
    assert route.call_count == 1


@pytest.mark.parametrize("failure", ["timeout", "503", "500"])
def test_execute_anonymous_is_never_resent(h: Harness, mock: respx.MockRouter, failure: str) -> None:
    """ADR 0001: ``executeAnonymous`` runs code, so a call that may have reached Salesforce is not resent."""
    if failure == "timeout":
        route = mock.get(ANON).mock(side_effect=httpx.ReadTimeout("slow"))
        expected: type[Exception] = esf.RequestTimeout
    else:
        route = mock.get(ANON).respond(
            int(failure), json=[{"errorCode": "SERVER_UNAVAILABLE", "message": "busy"}]
        )
        expected = esf.ServerError
    sf = _client(h)
    with pytest.raises(expected):
        h.run(sf.tooling.execute_anonymous("System.debug(1);"))
    assert route.call_count == 1


def test_execute_anonymous_rejects_blank_code(h: Harness) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        h.run(_client(h).tooling.execute_anonymous("  "))


def test_execute_anonymous_unexpected_body(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(ANON).respond(json=["nope"])
    with pytest.raises(esf.UnexpectedResponse):
        h.run(_client(h).tooling.execute_anonymous("System.debug(1);"))


# --- capture_log -----------------------------------------------------------------------


class _Org:
    """A respx handler for the capture_log flow; records the SOQL it was asked."""

    def __init__(self, *, active_flag: bool = False, level: bool = True) -> None:
        self.active_flag = active_flag
        self.level = level
        self.soql: list[str] = []
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[str] = []
        self.order: list[str] = []

    def query(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params["q"]
        self.soql.append(q)
        if "FROM TraceFlag" in q:
            self.order.append("query TraceFlag")
            recs = (
                [{"attributes": {"type": "TraceFlag"}, "Id": "7tfxx0000000077AAA"}]
                if self.active_flag
                else []
            )
            return httpx.Response(200, json={**_empty("TraceFlag"), "records": recs, "totalSize": len(recs)})
        if "FROM DebugLevel" in q:
            self.order.append("query DebugLevel")
            recs = [{"attributes": {"type": "DebugLevel"}, "Id": LEVEL_ID}] if self.level else []
            return httpx.Response(200, json={**_empty("DebugLevel"), "records": recs, "totalSize": len(recs)})
        self.order.append("query ApexLog")
        return httpx.Response(200, json=load_fixture(CAPTURE, 4))

    def create(self, name: str, new_id: str) -> Callable[[httpx.Request], httpx.Response]:
        def fn(request: httpx.Request) -> httpx.Response:
            self.order.append(f"create {name}")
            self.created.append((name, json.loads(request.content)))
            return httpx.Response(201, json={"id": new_id, "success": True, "errors": []})

        return fn

    def delete(self, request: httpx.Request) -> httpx.Response:
        self.order.append("delete TraceFlag")
        self.deleted.append(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(204)

    def install(self, mock: respx.MockRouter, anon: Any = None) -> None:
        mock.get(f"{TOOLING}/query").mock(side_effect=self.query)
        mock.post(f"{TOOLING}/sobjects/TraceFlag/").mock(side_effect=self.create("TraceFlag", FLAG_ID))
        mock.post(f"{TOOLING}/sobjects/DebugLevel/").mock(side_effect=self.create("DebugLevel", LEVEL_ID))
        mock.delete(f"{TOOLING}/sobjects/TraceFlag/{FLAG_ID}").mock(side_effect=self.delete)
        body = load_response(CAPTURE, 5)
        mock.get(f"{TOOLING}/sobjects/ApexLog/{LOG_ID}/Body").respond(
            text=body["text"], headers={"content-type": body["headers"]["content-type"]}
        )

        def run(request: httpx.Request) -> httpx.Response:
            self.order.append("executeAnonymous")
            if isinstance(anon, BaseException):
                raise anon
            return httpx.Response(200, json=anon or load_fixture("tooling/execute_ok.json"))

        mock.get(ANON).mock(side_effect=run)


def test_capture_log_full_flow(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    org = _Org(level=False)
    org.install(mock)
    sf = _client(h, rec)
    result = h.run(sf.tooling.execute_anonymous("System.debug('hello');", capture_log=True))
    assert result.success
    assert result.log is not None
    assert "USER_DEBUG|[1]|DEBUG|esf-sandbox capture" in result.log
    assert org.order == [
        "query TraceFlag",
        "query DebugLevel",
        "create DebugLevel",
        "create TraceFlag",
        "executeAnonymous",
        "query ApexLog",
        "delete TraceFlag",
    ]
    (level_name, level), (flag_name, flag) = org.created
    assert level_name == "DebugLevel"
    assert level["DeveloperName"] == level["MasterLabel"] == "esf_anonymous_apex"
    assert level["ApexCode"] == "DEBUG"
    assert flag_name == "TraceFlag"
    assert flag == {
        "TracedEntityId": USER_ID,
        "DebugLevelId": LEVEL_ID,
        "LogType": "USER_DEBUG",
        "StartDate": "2026-11-11T22:48:30Z",
        "ExpirationDate": "2026-11-11T22:58:30Z",
    }
    assert org.deleted == [FLAG_ID]
    assert f"LogUserId = '{USER_ID}'" in org.soql[-1]
    assert "StartTime >= 2026-11-11T22:47:30Z" in org.soql[-1]
    # Every extra call goes through the pipeline and is counted in usage.
    ops = [e["operation"] for e in rec.requests]
    assert ops.count("tooling.query") == 3
    assert "tooling.apex_log" in ops
    assert len(ops) == 8
    by_op = sf.usage.snapshot().calls_by_operation
    assert sum(by_op.values()) == 8
    assert by_op["tooling.query"] == 3


def test_capture_log_reuses_the_debug_level(h: Harness, mock: respx.MockRouter) -> None:
    org = _Org(level=True)
    org.install(mock)
    h.run(_client(h).tooling.execute_anonymous("System.debug(1);", capture_log=True))
    assert [n for n, _ in org.created] == ["TraceFlag"]


def test_capture_log_with_an_active_flag_neither_creates_nor_deletes(
    h: Harness, mock: respx.MockRouter
) -> None:
    org = _Org(active_flag=True)
    org.install(mock)
    result = h.run(_client(h).tooling.execute_anonymous("System.debug(1);", capture_log=True))
    assert result.log is not None
    assert org.created == []
    assert org.deleted == []


def test_capture_log_deletes_the_flag_when_the_run_fails(h: Harness, mock: respx.MockRouter) -> None:
    org = _Org()
    org.install(mock, anon=httpx.ReadTimeout("slow"))
    with pytest.raises(esf.RequestTimeout):
        h.run(_client(h).tooling.execute_anonymous("System.debug(1);", capture_log=True))
    assert org.deleted == [FLAG_ID]
    assert org.order.count("executeAnonymous") == 1


def test_capture_log_keeps_the_run_error_when_the_delete_fails(h: Harness, mock: respx.MockRouter) -> None:
    org = _Org()
    org.install(mock, anon=httpx.ReadTimeout("slow"))
    mock.delete(f"{TOOLING}/sobjects/TraceFlag/{FLAG_ID}").respond(
        403, json=[{"errorCode": "INSUFFICIENT_ACCESS", "message": "no"}]
    )
    with pytest.raises(esf.RequestTimeout):
        h.run(_client(h).tooling.execute_anonymous("System.debug(1);", capture_log=True))


def test_capture_log_warns_when_the_delete_fails_after_a_run(h: Harness, mock: respx.MockRouter) -> None:
    org = _Org()
    org.install(mock)
    mock.delete(f"{TOOLING}/sobjects/TraceFlag/{FLAG_ID}").respond(
        403, json=[{"errorCode": "INSUFFICIENT_ACCESS", "message": "no"}]
    )
    with pytest.warns(RuntimeWarning, match="could not delete TraceFlag"):
        result = h.run(_client(h).tooling.execute_anonymous("System.debug(1);", capture_log=True))
    assert result.success
    assert result.log


def test_capture_log_returns_the_result_when_the_log_cant_be_read(h: Harness, mock: respx.MockRouter) -> None:
    org = _Org()
    org.install(mock)
    mock.get(f"{TOOLING}/sobjects/ApexLog/{LOG_ID}/Body").respond(
        404, json=[{"errorCode": "NOT_FOUND", "message": "gone"}]
    )
    with pytest.warns(RuntimeWarning, match="debug log could not be read"):
        result = h.run(_client(h).tooling.execute_anonymous("System.debug(1);", capture_log=True))
    assert result.success
    assert result.log is None
    assert org.deleted == [FLAG_ID]
    assert org.order.count("executeAnonymous") == 1


def test_capture_log_without_a_matching_log(h: Harness, mock: respx.MockRouter) -> None:
    org = _Org()
    org.install(mock)
    sf = _client(h)
    sf._transport.now = lambda: datetime(2027, 1, 1, tzinfo=timezone.utc)  # every log is older
    result = h.run(sf.tooling.execute_anonymous("System.debug(1);", capture_log=True))
    assert result.success
    assert result.log is None
    assert org.deleted == [FLAG_ID]


def test_capture_log_with_a_static_token_asks_userinfo(h: Harness, mock: respx.MockRouter) -> None:
    org = _Org()
    org.install(mock)
    info = mock.get(f"{INSTANCE}/services/oauth2/userinfo").respond(json={"user_id": USER_ID})
    sf = _client(h, auth=esf.StaticToken("AT-static", INSTANCE), token=False)
    h.run(sf.tooling.execute_anonymous("System.debug(1);", capture_log=True))
    assert info.called
    assert org.created[-1][1]["TracedEntityId"] == USER_ID


def test_capture_log_userinfo_without_user_id(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{INSTANCE}/services/oauth2/userinfo").respond(json={})
    anon = mock.get(ANON).respond(json=load_fixture("tooling/execute_ok.json"))
    sf = _client(h, auth=esf.StaticToken("AT-static", INSTANCE), token=False)
    with pytest.raises(esf.UnexpectedResponse, match="user_id"):
        h.run(sf.tooling.execute_anonymous("System.debug(1);", capture_log=True))
    assert not anon.called


# --- tests & completions ---------------------------------------------------------------


def test_run_tests_async(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Recorded (V46): the 15-character job id comes back as a JSON string, HTTP 200."""
    recorded = load_response("tooling/run_tests_async.json")
    assert recorded["status"] == 200
    route = mock.post(f"{TOOLING}/runTestsAsynchronous/").respond(200, json=recorded["json"])
    sf = _client(h, rec)
    job = h.run(sf.tooling.run_tests_async(["01pxx0000000002AAA", "01pxx0000000004AAA"]))
    assert job == "707000000000002"
    assert json.loads(route.calls[0].request.content) == {"classids": "01pxx0000000002AAA,01pxx0000000004AAA"}
    assert rec.requests[0]["operation"] == "tooling.run_tests_async"


def test_run_tests_async_plain_text_and_not_resent(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(f"{TOOLING}/runTestsAsynchronous/").respond(
        200, text="707xx0000000002", headers={"content-type": "text/plain"}
    )
    assert h.run(_client(h).tooling.run_tests_async(["01pxx0000000002AAA"])) == "707xx0000000002"
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(esf.RequestTimeout):
        h.run(_client(h).tooling.run_tests_async(["01pxx0000000002AAA"]))
    assert route.call_count == 2  # one per call: the POST is never resent


def test_run_tests_async_validates_ids(h: Harness) -> None:
    sf = _client(h)
    with pytest.raises(TypeError):
        h.run(sf.tooling.run_tests_async("01pxx0000000002AAA"))
    with pytest.raises(ValueError):
        h.run(sf.tooling.run_tests_async([]))
    with pytest.raises(ValueError):
        h.run(sf.tooling.run_tests_async(["nope"]))


def test_completions(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Recorded (V47), trimmed to two classes: ``publicDeclarations`` keyed by namespace."""
    route = mock.get(f"{TOOLING}/completions").respond(json=load_fixture("tooling/completions_apex.json"))
    sf = _client(h, rec)
    out = h.run(sf.tooling.completions("apex"))
    math = out["publicDeclarations"]["System"]["Math"]
    assert set(math) == {"constructors", "methods", "properties"}
    assert any(m["name"] == "abs" for m in math["methods"])
    assert route.calls[0].request.url.params["type"] == "apex"
    assert rec.requests[0]["operation"] == "tooling.completions"
    with pytest.raises(ValueError, match="apex, visualforce"):
        h.run(sf.tooling.completions("java"))  # type: ignore[arg-type]
    # Recorded, trimmed: Visualforce answers under "completions", keyed by component.
    route.respond(json=load_fixture("tooling/completions_visualforce.json"))
    page = h.run(sf.tooling.completions("visualforce"))["completions"]["apex:page"]
    assert "attribs" in page
    assert route.calls[-1].request.url.params["type"] == "visualforce"
    route.respond(json=[])
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.tooling.completions("visualforce"))


def test_capture_log_replays_the_recorded_flow(h: Harness) -> None:
    """The recorded ``capture_log=True`` flow (V23), replayed request by request.

    The run's ``ApexLog`` has ``Operation`` ``/services/data/v68.0/tooling/executeAnonymous/``
    and a ``StartTime`` truncated to the second; it was queryable at once.
    """
    transport = replay(RECORDED / CAPTURE)
    http = httpx.AsyncClient(transport=transport) if h.is_async else httpx.Client(transport=transport)
    sf = _client(h, http_client=http)
    result = h.run(sf.tooling.execute_anonymous("System.debug('esf-sandbox capture');", capture_log=True))
    assert result.success
    assert result.log is not None
    assert "USER_DEBUG|[1]|DEBUG|esf-sandbox capture" in result.log
    assert transport.handler.remaining == 0  # type: ignore[attr-defined]
    log_row = load_fixture(CAPTURE, 4)["records"][0]
    assert log_row["Operation"] == "/services/data/v68.0/tooling/executeAnonymous/"


def test_a_second_active_trace_flag_is_refused(h: Harness, mock: respx.MockRouter) -> None:
    """Recorded (V23): Salesforce refuses an overlapping USER_DEBUG flag for the same user.

    That's why ``capture_log`` reuses an active flag instead of creating one.
    """
    recorded = load_response("tooling/trace_flag_overlap.json")
    mock.post(f"{TOOLING}/sobjects/TraceFlag/").respond(recorded["status"], json=recorded["json"])
    sf = _client(h)
    with pytest.raises(esf.InvalidReference) as ei:
        h.run(sf.tooling.sobject("TraceFlag").create({"TracedEntityId": USER_ID, "LogType": "USER_DEBUG"}))
    assert ei.value.error_code == "FIELD_INTEGRITY_EXCEPTION"
    assert "already being traced" in ei.value.message
