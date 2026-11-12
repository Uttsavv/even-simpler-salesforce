"""Tooling API (``sf.tooling``) and Metadata API (``sf.metadata``) against a real org.

Covers big-bang.md §21 V17, V23, V45-V47, V70-V74, V85 and V86, on both clients.

Org fixtures: ``tests/sandbox/org/tooling`` (the Apex test class ``EsfSandboxActionTest``, which
tests ``EsfSandboxAction`` from ``tests/sandbox/org/core``); ``scripts/sandbox_setup_org.py``
deploys it. Every other deployment here is check-only (it saves nothing) except the quick deploy,
which deploys ``EsfSandboxActionTest`` again, unchanged. Anonymous Apex here only writes to the
debug log. ``capture_log`` creates a ``TraceFlag`` and deletes it; the tests delete any flag they
create themselves. The ``DebugLevel`` ``esf_anonymous_apex`` stays: the client reuses it.

Set ``ESF_SANDBOX_RECORD_DIR=<dir>`` to record each exchange of the sync run into
``<dir>/<name>.json`` with ``esf.testing.record`` (the source of ``tests/fixtures/recorded/tooling``
and ``.../metadata``; names in SOAP XML such as ``createdByName`` are not masked by the recorder
and were replaced by hand).

Docs (developer.salesforce.com refuses non-browser clients; read from search excerpts):

- Tooling API REST resources (``executeAnonymous``, ``runTestsAsynchronous``, ``completions``):
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/intro_rest_resources.htm
- ``ExecuteAnonymousResult``:
  https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_calls_executeanonymous_result.htm
- ``TraceFlag``, ``DebugLevel``, ``ApexLog``:
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/tooling_api_objects_traceflag.htm,
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/tooling_api_objects_debuglevel.htm,
  https://developer.salesforce.com/docs/atlas.en-us.api_tooling.meta/api_tooling/tooling_api_objects_apexlog.htm
- Metadata API: *Deploy Metadata with REST* (``meta_rest_deploy.htm``), *Check the Status of Your
  Deployment Using REST Resources* (``meta_rest_deploy_checkstatus.htm``), *Cancel a Deployment
  in Progress Using REST* (``meta_rest_deploy_cancel.htm``), *Deploy a Recently Validated
  Component Set Without Tests* (``meta_rest_deploy_recentvalidation.htm``), ``retrieve()``,
  ``checkRetrieveStatus()``, ``listMetadata()``, ``describeMetadata()``, all under
  https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/
"""

from __future__ import annotations

import io
import os
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core.tooling import DEBUG_LEVEL_NAME, trace_flag_body
from even_simpler_salesforce.testing import record

from .conftest import Driver

pytestmark = pytest.mark.sandbox

TOOLING_DIR = Path(__file__).parent / "org" / "tooling"
TEST_CLASS = "EsfSandboxActionTest"
POLL = esf.Backoff(start=1, max=5)
# JSON keys holding user names in deploy results (the recorder masks names only in sObjects).
NAME_KEYS = {"createdByName", "lastModifiedByName", "canceledByName"}

_PKG = (
    '<?xml version="1.0" encoding="UTF-8"?><Package xmlns="http://soap.sforce.com/2006/04/metadata">'
    "<types>{members}<name>ApexClass</name></types><version>66.0</version></Package>"
)
_META = (
    '<?xml version="1.0" encoding="UTF-8"?><ApexClass xmlns="http://soap.sforce.com/2006/04/metadata">'
    "<apiVersion>66.0</apiVersion><status>Active</status></ApexClass>"
)


def _client(sb: Driver, name: str | None = None, **kw: Any) -> Any:
    """A client; with ``ESF_SANDBOX_RECORD_DIR`` the sync run records into ``<dir>/<name>.json``."""
    out = os.environ.get("ESF_SANDBOX_RECORD_DIR")
    if out and name and not sb.is_async:
        Path(out).mkdir(parents=True, exist_ok=True)
        kw["http_client"] = httpx.Client(
            transport=record(Path(out) / f"{name}.json", extra_keys=NAME_KEYS), timeout=300
        )
    return sb.client(**kw)


def _zip(classes: dict[str, str]) -> bytes:
    """A deploy zip with ``package.xml`` at the root and one Apex class per entry."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("package.xml", _PKG.format(members="".join(f"<members>{n}</members>" for n in classes)))
        for name, source in classes.items():
            zf.writestr(f"classes/{name}.cls", source)
            zf.writestr(f"classes/{name}.cls-meta.xml", _META)
    return buf.getvalue()


def _check_only(**kw: Any) -> esf.DeployOptions:
    return esf.DeployOptions(check_only=True, **kw)


def _user_id(sb: Driver, sf: Any) -> str:
    return str(sb.run(sf.auth.userinfo())["user_id"])


@pytest.fixture
def test_class_id(sb: Driver) -> str:
    sf = sb.client()
    rows = sb.run(sf.tooling.query("SELECT Id FROM ApexClass WHERE Name = {}", TEST_CLASS)).records
    if not rows:
        pytest.skip(f"sandbox: {TEST_CLASS} missing; run scripts/sandbox_setup_org.py")
    return str(rows[0]["Id"])


# --- anonymous Apex (V17, V45) ------------------------------------------------------------


def test_execute_anonymous_result_fields(sb: Driver) -> None:
    """V17/V45: ``line``/``column`` are -1 and the empty fields null when nothing failed."""
    sf = _client(sb, "execute_ok")
    ok = sb.run(sf.tooling.execute_anonymous("System.debug('esf-sandbox');"))
    assert ok == esf.AnonymousResult(compiled=True, success=True)
    body = sb.run(sf.request("GET", "tooling/executeAnonymous/", params={"anonymousBody": "Integer i = 1;"}))
    assert body == {
        "line": -1,
        "column": -1,
        "compiled": True,
        "success": True,
        "compileProblem": None,
        "exceptionStackTrace": None,
        "exceptionMessage": None,
    }


def test_execute_anonymous_compile_error(sb: Driver) -> None:
    sf = _client(sb, "execute_compile_error")
    r = sb.run(sf.tooling.execute_anonymous("Integer i = 1;\nSystem debug('x');"))
    assert (r.compiled, r.success) == (False, False)
    assert r.compile_problem == "Unexpected token 'debug'."
    assert (r.line, r.column) == (2, 8)
    assert r.exception_message is None


def test_execute_anonymous_runtime_exception(sb: Driver) -> None:
    sf = _client(sb, "execute_exception")
    r = sb.run(sf.tooling.execute_anonymous("Integer a = 0;\nInteger i = 1 / a;"))
    assert (r.compiled, r.success) == (True, False)
    assert r.exception_message == "System.MathException: Divide by 0"
    assert r.exception_stack_trace == "AnonymousBlock: line 2, column 1"
    assert (r.line, r.column) == (2, 1)
    assert r.compile_problem is None


@pytest.mark.parametrize(
    ("size", "name"), [(20_000, "execute_headers_too_large"), (30_000, "execute_uri_too_long")]
)
def test_execute_anonymous_too_long_is_request_too_large(sb: Driver, size: int, name: str) -> None:
    """V45: about 18,500 URL-encoded characters still ran; 20,000 got 431 and 30,000 got 414
    (HTML pages from the web tier). Both are ``RequestTooLarge``; the code didn't run."""
    sf = _client(sb, name)
    sb.run(sf.tooling.execute_anonymous("System.debug(1);//" + "x" * 18_000))
    with pytest.raises(esf.RequestTooLarge) as ei:
        sb.run(sf.tooling.execute_anonymous("System.debug(1);//" + "x" * size))
    assert ei.value.status in (414, 431)


# --- debug-log capture (V23) --------------------------------------------------------------


def test_capture_log(sb: Driver) -> None:
    """V23: the log of the run is queryable at once; its ``Operation`` is the request path."""
    sf = _client(sb, "capture_log")
    uid = _user_id(sb, sf)
    r = sb.run(sf.tooling.execute_anonymous("System.debug('esf-sandbox capture');", capture_log=True))
    assert r.success
    assert r.log is not None
    assert "USER_DEBUG|[1]|DEBUG|esf-sandbox capture" in r.log
    since = datetime.now(timezone.utc) - timedelta(minutes=2)
    rows = sb.run(
        sf.tooling.query(
            "SELECT Operation, Request, Status FROM ApexLog WHERE LogUserId = {} AND StartTime >= {} "
            "AND Operation LIKE '%executeAnonymous%' ORDER BY StartTime DESC LIMIT 1",
            uid,
            since,
        )
    ).records
    assert rows[0]["Operation"] == f"/services/data/v{sf.api_version}/tooling/executeAnonymous/"
    assert rows[0]["Request"] == "Api"
    flags = sb.run(
        sf.tooling.query(
            "SELECT Id FROM TraceFlag WHERE TracedEntityId = {} AND LogType = 'USER_DEBUG' "
            "AND DebugLevel.DeveloperName = {}",
            uid,
            DEBUG_LEVEL_NAME,
        )
    ).records
    assert flags == []  # the client deleted its flag


def test_second_overlapping_trace_flag_is_refused(sb: Driver) -> None:
    """V23: one active ``USER_DEBUG`` flag per user; a second overlapping one is refused, so
    ``capture_log`` reuses an active flag (and doesn't delete it)."""
    sf = _client(sb)
    uid = _user_id(sb, sf)
    levels = sb.run(sf.tooling.query("SELECT Id FROM DebugLevel WHERE DeveloperName = {}", DEBUG_LEVEL_NAME))
    if not levels.records:
        pytest.skip("sandbox: run test_capture_log first (it creates the DebugLevel)")
    level_id = str(levels.records[0]["Id"])
    now = datetime.now(timezone.utc)
    flags = sf.tooling.sobject("TraceFlag")
    first = sb.run(flags.create(trace_flag_body(uid, level_id, now)))
    try:
        with pytest.raises(esf.InvalidReference) as ei:
            sb.run(flags.create(trace_flag_body(uid, level_id, now)))
        assert ei.value.error_code == "FIELD_INTEGRITY_EXCEPTION"
        r = sb.run(sf.tooling.execute_anonymous("System.debug('esf-sandbox reuse');", capture_log=True))
        assert r.log is not None
        assert "esf-sandbox reuse" in r.log
        assert sb.run(flags.get(first))["Id"]  # still there: not the client's to delete
    finally:
        sb.run(flags.delete(first))


# --- tests and completions (V46, V47) -----------------------------------------------------


def test_run_tests_async(sb: Driver, test_class_id: str) -> None:
    """V46: the job id comes back as a JSON string with 15 characters, HTTP 200."""
    sf = _client(sb, "run_tests_async")
    job = sb.run(sf.tooling.run_tests_async([test_class_id]))
    assert job.startswith("707")
    assert len(job) == 15
    deadline = time.monotonic() + 120
    status = None
    while time.monotonic() < deadline:
        status = sb.run(sf.query("SELECT Status FROM AsyncApexJob WHERE Id = {}", job)).records[0]["Status"]
        if status in ("Completed", "Failed", "Aborted"):
            break
        time.sleep(2)
    assert status == "Completed"
    results = sb.run(
        sf.tooling.query("SELECT Outcome, MethodName FROM ApexTestResult WHERE AsyncApexJobId = {}", job)
    )
    assert [(r["MethodName"], r["Outcome"]) for r in results.records] == [("greets", "Pass")]


def test_completions(sb: Driver) -> None:
    """V47: ``visualforce`` answers ``{"completions": {...}}``; ``apex`` answers
    ``{"publicDeclarations": {namespace: {...}}}`` (about 29 MB, so the sync run only)."""
    sf = _client(sb, "completions_visualforce")
    vf = sb.run(sf.tooling.completions("visualforce"))
    assert list(vf) == ["completions"]
    assert "attribs" in vf["completions"]["apex:page"]
    if sb.is_async:
        return
    big = _client(sb, timeout=esf.Timeouts(read=300))
    apex = sb.run(big.tooling.completions("apex"))
    assert list(apex) == ["publicDeclarations"]
    assert set(apex["publicDeclarations"]["System"]["Math"]) == {"constructors", "methods", "properties"}


# --- deploy over REST (V70, V71) ----------------------------------------------------------


def test_deploy_component_failure(sb: Driver, writes: None) -> None:
    sf = _client(sb, "deploy_failed")
    job = sb.run(
        sf.metadata.deploy(
            _zip({"EsfSandboxBroken": "public class EsfSandboxBroken { Integer x = ; }"}),
            options=_check_only(),
        )
    )
    assert job.status == "Pending"
    res = sb.run(sf.metadata.deploy_status(job.id, wait=True, poll=POLL, timeout=900))
    assert (res.status, res.failed) == ("Failed", True)
    (failure,) = res.component_failures
    assert (failure.full_name, failure.problem, failure.line, failure.column) == (
        "EsfSandboxBroken",
        "Unexpected token ';'.",
        1,
        45,
    )
    assert isinstance(res.raw["details"]["componentFailures"], list)


@pytest.mark.parametrize("rollback", [False, True])
def test_deploy_test_failure(sb: Driver, writes: None, rollback: bool) -> None:
    """With ``rollbackOnError`` off, Salesforce says ``Succeeded`` although the test failed."""
    sf = _client(sb, "deploy_test_failure_rollback" if rollback else "deploy_test_failure")
    src = (
        "@IsTest private class EsfSandboxFailingTest { @IsTest static void fails() "
        "{ System.assertEquals(1, 2, 'esf-sandbox failure'); } }"
    )
    opts = _check_only(
        rollback_on_error=rollback, test_level="RunSpecifiedTests", run_tests=["EsfSandboxFailingTest"]
    )
    job = sb.run(sf.metadata.deploy(_zip({"EsfSandboxFailingTest": src}), options=opts))
    res = sb.run(sf.metadata.deploy_status(job.id, wait=True, poll=POLL, timeout=900))
    assert res.status == ("Failed" if rollback else "Succeeded")
    assert res.test_errors == 1
    assert res.failed
    assert [t.method_name for t in res.test_failures] == ["fails"]
    with pytest.raises(esf.DeployFailed):
        res.raise_for_errors()


def test_deploy_zip_without_package_xml(sb: Driver, writes: None) -> None:
    """An empty zip is accepted (201) and then fails with ``No package.xml found``."""
    sf = _client(sb, "deploy_invalid_zip")
    job = sb.run(sf.metadata.deploy(b"PK\x05\x06" + b"\0" * 18, options=_check_only()))
    res = sb.run(sf.metadata.deploy_status(job.id, wait=True, poll=POLL, timeout=900))
    assert res.status == "Failed"
    assert [f.problem for f in res.component_failures] == ["No package.xml found"]


def test_deploy_status_of_unknown_id(sb: Driver) -> None:
    sf = _client(sb, "deploy_unknown_id")
    with pytest.raises(esf.InvalidReference) as ei:
        # A valid id (checksum right) that is no deployment.
        sb.run(sf.metadata.deploy_status("0Afg70000000000CAA"))
    assert (ei.value.status, ei.value.error_code) == (400, "INVALID_CROSS_REFERENCE_KEY")


# --- cancel and quick deploy (V85, V86) ---------------------------------------------------


def test_cancel_deploy(sb: Driver, writes: None) -> None:
    """V85: 202 ``Canceling``, then ``Canceled``; a finished deployment comes back unchanged."""
    sf = _client(sb, "deploy_cancel")
    slow = (
        "@IsTest private class EsfSandboxSlowTest { @IsTest static void slow() { "
        "Long t = System.currentTimeMillis(); "
        "while (Limits.getCpuTime() < 8000 && System.currentTimeMillis() - t < 20000) {} } }"
    )
    opts = _check_only(test_level="RunSpecifiedTests", run_tests=["EsfSandboxSlowTest"])
    job = sb.run(sf.metadata.deploy(_zip({"EsfSandboxSlowTest": slow}), options=opts))
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:  # cancel while its test runs
        st = sb.run(sf.metadata.deploy_status(job.id))
        if st.done or "Running Test" in str(st.raw.get("stateDetail")):
            break
        time.sleep(1)
    res = sb.run(sf.metadata.cancel_deploy(job.id))
    assert res.status == "Canceling"
    final = sb.run(sf.metadata.deploy_status(job.id, wait=True, poll=POLL, timeout=900))
    assert (final.status, final.failed) == ("Canceled", True)
    again = sb.run(sf.metadata.cancel_deploy(job.id))
    assert again.status == "Canceled"


def test_quick_deploy(sb: Driver, writes: None, test_class_id: str) -> None:
    """V86: validate with a test, quick-deploy it, then a second quick deploy is refused.

    Another deployment in the org between the validation and the quick deploy (another test
    run) also gets ``INVALID_ID_FIELD``; rerun the test then.
    """
    sf = _client(sb, "quick_deploy")
    opts = _check_only(test_level="RunSpecifiedTests", run_tests=[TEST_CLASS])
    job = sb.run(sf.metadata.deploy(TOOLING_DIR, options=opts))
    validated = sb.run(sf.metadata.deploy_status(job.id, wait=True, poll=POLL, timeout=900))
    validated.raise_for_errors()
    quick = sb.run(sf.metadata.quick_deploy(validated.id))
    assert quick.status == "Pending"
    assert quick.id != validated.id
    done = sb.run(sf.metadata.deploy_status(quick.id, wait=True, poll=POLL, timeout=900))
    assert (done.status, done.check_only) == ("Succeeded", False)
    with pytest.raises(esf.RequestError) as ei:
        sb.run(sf.metadata.quick_deploy(validated.id))
    assert ei.value.error_code == "INVALID_ID_FIELD"


def test_quick_deploy_without_tests_is_refused(sb: Driver, writes: None) -> None:
    sf = _client(sb, "quick_deploy_no_tests")
    job = sb.run(sf.metadata.deploy(TOOLING_DIR, options=_check_only(test_level="NoTestRun")))
    validated = sb.run(sf.metadata.deploy_status(job.id, wait=True, poll=POLL, timeout=900))
    validated.raise_for_errors()
    with pytest.raises(esf.RequestError) as ei:
        sb.run(sf.metadata.quick_deploy(validated.id))
    assert (ei.value.error_code, ei.value.message) == (
        "INVALID_ID_FIELD",
        "Source validate did not run tests in the org",
    )


# --- SOAP: retrieve, list, describe (V72-V74) ---------------------------------------------


def test_retrieve(sb: Driver, test_class_id: str, tmp_path: Path) -> None:
    """V72-V74: the OAuth token works in ``SessionHeader``; a missing member is a message; the
    zip has relative ``/`` names, ``package.xml`` at the root and no folder or symlink entries;
    once the zip was returned, the job is gone (``INVALID_LOCATOR``)."""
    sf = _client(sb, "retrieve")
    res = sb.run(
        sf.metadata.retrieve(
            {"ApexClass": [TEST_CLASS, "EsfSandboxNoSuchClass"]}, extract_to=tmp_path, poll=POLL
        )
    )
    assert [m.problem for m in res.messages] == [
        "Entity of type 'ApexClass' named 'EsfSandboxNoSuchClass' cannot be found"
    ]
    with zipfile.ZipFile(io.BytesIO(res.zip_file)) as zf:
        infos = zf.infolist()
    assert sorted(i.filename for i in infos) == [
        f"classes/{TEST_CLASS}.cls",
        f"classes/{TEST_CLASS}.cls-meta.xml",
        "package.xml",
    ]
    assert all(not i.is_dir() and i.external_attr >> 16 == 0 for i in infos)
    assert (tmp_path / "classes" / f"{TEST_CLASS}.cls").exists()
    for include_zip in (True, False):
        with pytest.raises(esf.SoapFault) as ei:
            sb.run(sf.metadata._retrieve_status(res.id, include_zip=include_zip))  # pyright: ignore[reportPrivateUsage]
        assert ei.value.error_code == "INVALID_LOCATOR"


def test_retrieve_unknown_type_succeeds_with_a_message(sb: Driver) -> None:
    sf = _client(sb)
    res = sb.run(sf.metadata.retrieve({"NoSuchType": ["*"]}, poll=POLL))
    assert [m.problem for m in res.messages] == ["Entity type: 'NoSuchType' is unknown"]


def test_list_and_describe_metadata(sb: Driver, test_class_id: str) -> None:
    sf = _client(sb, "list_metadata")
    classes = sb.run(sf.metadata.list_metadata("ApexClass"))
    assert TEST_CLASS in {c.full_name for c in classes}
    reports = sb.run(sf.metadata.list_metadata("Report", folder="unfiled$public"))
    assert "unfiled$public/Esf_Sandbox_Leads_Tabular" in {r.full_name for r in reports}
    assert sb.run(sf.metadata.list_metadata("Report", folder="Esf_No_Such_Folder")) == []
    before = len(sb.recorder.requests("metadata.list"))
    with pytest.raises(esf.InvalidType):
        sb.run(sf.metadata.list_metadata("NoSuchType"))
    assert len(sb.recorder.requests("metadata.list")) == before + 1  # a fault is not resent
    d = sb.run(sf.metadata.describe_metadata())
    assert "ApexClass" in {t.xml_name for t in d.types}


def test_bad_session_is_an_invalid_session_fault(sb: Driver) -> None:
    """V72: a rejected session is an ``INVALID_SESSION_ID`` fault with HTTP 500, not a 401."""
    sf = sb.client()
    tok = sb.run(sf.auth.token())
    bad = esf.AsyncSalesforce if sb.is_async else esf.Salesforce
    other: Any = bad(esf.StaticToken("not-a-session", tok.instance_url), api_version=sf.api_version)
    try:
        with pytest.raises(esf.SessionExpired) as ei:
            sb.run(other.metadata.describe_metadata())
        assert (ei.value.status, ei.value.error_code) == (500, "INVALID_SESSION_ID")
    finally:
        sb.run(other.aclose() if sb.is_async else other.close())
