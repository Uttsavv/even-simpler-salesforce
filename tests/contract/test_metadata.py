"""Metadata API (``sf.metadata``, §9.15) for both clients.

Every response here is **synthetic** (``tests/fixtures/recorded/metadata``), hand-written from
the Metadata API Developer Guide because no sandbox recording exists yet (V70-V74 in
big-bang.md §21). developer.salesforce.com returns 403 to non-browser clients, so the pages
were read from search excerpts on 2026-11-11:

- Deploy with REST (multipart ``json`` + ``file`` parts, ``deployOptions``):
  https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_rest_deploy.htm
- Deploy status with REST (``GET metadata/deployRequest/{id}?includeDetails=true``):
  https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_rest_deploy_checkstatus.htm
- Deploy a recently validated component set without tests ("quick deploy", ``POST
  metadata/deployRequest/{validatedId}`` with ``validatedDeployRequestId``; the page warns that
  ``PATCH`` there starts a new deployment):
  https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_rest_deploy_recentvalidation.htm
- Cancel a deployment with REST (``PATCH metadata/deployRequest/{id}`` with
  ``{"deployResult": {"status": "Canceling"}}``; from API 65.0 a deployment in the
  ``FinalizingDeploy`` stage can't be canceled):
  https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_rest_deploy_cancel.htm
- DeployResult (status values, ``details.componentFailures``):
  https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_deployresult.htm
- retrieve(): https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_retrieve.htm
- checkRetrieveStatus() (zip deleted from the server once returned; ``includeZip`` from 34.0):
  https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_checkretrievestatus.htm
- listMetadata(): https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_listmetadata.htm
- describeMetadata(): https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_describe.htm
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import API, DATA, INSTANCE, TOKEN_URL, Harness, Recorder, cc_auth, token_body
from ..fixtures import load_fixture

SOAP = f"{INSTANCE}/services/Soap/m/{API}"
DEPLOY = f"{DATA}/metadata/deployRequest"
DID = "0Afxx0000000001AAA"
RID = "09Sxx0000000001AAA"
M = "{http://soap.sforce.com/2006/04/metadata}"
ENV = "{http://schemas.xmlsoap.org/soap/envelope/}"
STATUS = re.compile(re.escape(DEPLOY) + r"/0Afxx0000000001AAA(\?.*)?$")

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")


def xml(name: str, status: int = 200) -> httpx.Response:
    """A synthetic SOAP response from ``tests/fixtures/recorded/metadata``."""
    return httpx.Response(
        status, content=load_fixture(f"metadata/{name}").encode(), headers={"Content-Type": "text/xml"}
    )


def js(name: str, status: int = 200) -> httpx.Response:
    """A synthetic JSON response from ``tests/fixtures/recorded/metadata``."""
    return httpx.Response(status, json=load_fixture(f"metadata/{name}"))


def body_of(request: httpx.Request) -> ET.Element:
    el = ET.fromstring(request.content).find(f"{ENV}Body")
    assert el is not None
    return el[0]


def session_of(request: httpx.Request) -> str | None:
    el = ET.fromstring(request.content).find(f"{ENV}Header/{M}SessionHeader/{M}sessionId")
    return el.text if el is not None else None


def parts(request: httpx.Request) -> dict[str, tuple[str, bytes]]:
    """The multipart parts of a deploy request: name -> (headers, body)."""
    m = re.search(r'boundary="([^"]+)"', request.headers["content-type"])
    assert m is not None
    boundary = m.group(1).encode()
    out: dict[str, tuple[str, bytes]] = {}
    for chunk in request.content.split(b"--" + boundary)[1:-1]:
        head, _, data = chunk.strip(b"\r\n").partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head)
        assert name is not None
        out[name.group(1).decode()] = (head.decode(), data)
    return out


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


PACKAGE = (
    b'<?xml version="1.0" encoding="UTF-8"?><Package xmlns="http://soap.sforce.com/2006/04/metadata">'
    b"<types><members>Invoice</members><name>ApexClass</name></types><version>68.0</version></Package>"
)
ZIP = make_zip({"package.xml": PACKAGE, "classes/Invoice.cls": b"public class Invoice {}"})


def write_project(root: Path) -> Path:
    (root / "classes").mkdir(parents=True)
    (root / "package.xml").write_bytes(PACKAGE)
    (root / "classes" / "Invoice.cls").write_bytes(b"public class Invoice {}")
    (root / "classes" / "Invoice.cls-meta.xml").write_bytes(b"<ApexClass/>")
    return root


# --- deploy ---------------------------------------------------------------------------------


def test_deploy_bytes_sends_multipart_and_returns_pending(
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    """Synthetic fixture ``metadata/deploy_pending.json``: a ``json`` part, then the zip as ``file``."""
    route = mock.post(DEPLOY).mock(return_value=js("deploy_pending.json", 201))
    sf = h.client(recorder=rec)
    opts = esf.DeployOptions(check_only=True, test_level="RunSpecifiedTests", run_tests=["InvoiceTest"])
    res = h.run(sf.metadata.deploy(ZIP, options=opts))
    assert res.id == DID
    assert res.status == "Pending"
    assert not res.done
    req = route.calls[0].request
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert int(req.headers["content-length"]) == len(req.content)
    got = parts(req)
    assert list(got) == ["json", "file"]
    assert "application/json" in got["json"][0]
    assert json.loads(got["json"][1]) == {
        "deployOptions": {
            "checkOnly": True,
            "ignoreWarnings": False,
            "purgeOnDelete": False,
            "allowMissingFiles": False,
            "autoUpdatePackage": False,
            "singlePackage": True,
            "testLevel": "RunSpecifiedTests",
            "runTests": ["InvoiceTest"],
        }
    }
    assert 'filename="deploy.zip"' in got["file"][0]
    assert "application/zip" in got["file"][0]
    assert got["file"][1] == ZIP
    (event,) = rec.requests
    assert event["operation"] == "metadata.deploy"
    assert event["outcome"] == "ok"


def test_deploy_rollback_option_is_sent_when_set(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(DEPLOY).mock(return_value=js("deploy_pending.json", 201))
    sf = h.client()
    h.run(
        sf.metadata.deploy(ZIP, options=esf.DeployOptions(rollback_on_error=True, test_level="RunLocalTests"))
    )
    opts = json.loads(parts(route.calls[0].request)["json"][1])["deployOptions"]
    assert opts["rollbackOnError"] is True
    assert opts["testLevel"] == "RunLocalTests"
    assert "runTests" not in opts


def test_deploy_directory_is_zipped_with_package_xml_at_root(
    h: Harness, mock: respx.MockRouter, tmp_path: Path
) -> None:
    route = mock.post(DEPLOY).mock(return_value=js("deploy_pending.json", 201))
    sf = h.client()
    h.run(sf.metadata.deploy(write_project(tmp_path / "src")))
    data = parts(route.calls[0].request)["file"][1]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(zf.namelist()) == ["classes/Invoice.cls", "classes/Invoice.cls-meta.xml", "package.xml"]
        assert zf.read("package.xml") == PACKAGE


def test_deploy_zip_file_path_is_streamed(h: Harness, mock: respx.MockRouter, tmp_path: Path) -> None:
    route = mock.post(DEPLOY).mock(return_value=js("deploy_pending.json", 201))
    path = tmp_path / "deploy.zip"
    path.write_bytes(ZIP)
    sf = h.client()
    h.run(sf.metadata.deploy(str(path)))
    assert parts(route.calls[0].request)["file"][1] == ZIP


def test_deploy_rejects_bad_sources_before_sending(
    h: Harness, mock: respx.MockRouter, tmp_path: Path
) -> None:
    route = mock.post(DEPLOY).mock(return_value=js("deploy_pending.json", 201))
    sf = h.client()
    with pytest.raises(ValueError, match="not a zip"):
        h.run(sf.metadata.deploy(b"<Package/>"))
    (tmp_path / "notzip.txt").write_bytes(b"hello")
    with pytest.raises(ValueError, match="not a zip"):
        h.run(sf.metadata.deploy(tmp_path / "notzip.txt"))
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match=r"package\.xml"):
        h.run(sf.metadata.deploy(tmp_path / "empty"))
    with pytest.raises(FileNotFoundError):
        h.run(sf.metadata.deploy(tmp_path / "missing.zip"))
    with pytest.raises(TypeError):
        h.run(sf.metadata.deploy(123))  # type: ignore[arg-type]
    assert not route.called


def test_deploy_folder_without_package_xml_is_allowed_for_multi_package_zips(
    h: Harness, mock: respx.MockRouter, tmp_path: Path
) -> None:
    route = mock.post(DEPLOY).mock(return_value=js("deploy_pending.json", 201))
    write_project(tmp_path / "root" / "pkg1")
    sf = h.client()
    h.run(sf.metadata.deploy(tmp_path / "root", options=esf.DeployOptions(single_package=False)))
    with zipfile.ZipFile(io.BytesIO(parts(route.calls[0].request)["file"][1])) as zf:
        assert "pkg1/package.xml" in zf.namelist()


@posix_only
def test_deploy_refuses_symlink_out_of_the_folder(h: Harness, mock: respx.MockRouter, tmp_path: Path) -> None:
    route = mock.post(DEPLOY).mock(return_value=js("deploy_pending.json", 201))
    src = write_project(tmp_path / "src")
    secret = tmp_path / "secret.txt"
    secret.write_text("password")
    os.symlink(secret, src / "classes" / "Leak.cls")
    sf = h.client()
    with pytest.raises(ValueError, match="outside"):
        h.run(sf.metadata.deploy(src))
    os.unlink(src / "classes" / "Leak.cls")
    os.symlink(tmp_path, src / "escape")
    with pytest.raises(ValueError, match="outside"):
        h.run(sf.metadata.deploy(src))
    assert not route.called


@posix_only
def test_deploy_follows_symlinks_inside_the_folder(
    h: Harness, mock: respx.MockRouter, tmp_path: Path
) -> None:
    route = mock.post(DEPLOY).mock(return_value=js("deploy_pending.json", 201))
    src = write_project(tmp_path / "src")
    os.symlink(src / "classes" / "Invoice.cls", src / "classes" / "Alias.cls")
    os.symlink(src / "classes", src / "linked")  # a symlinked folder is not followed
    sf = h.client()
    h.run(sf.metadata.deploy(src))
    with zipfile.ZipFile(io.BytesIO(parts(route.calls[0].request)["file"][1])) as zf:
        assert zf.read("classes/Alias.cls") == b"public class Invoice {}"
        assert not any(n.startswith("linked/") for n in zf.namelist())


def test_deploy_is_never_resent_after_it_may_have_reached_salesforce(
    h: Harness, mock: respx.MockRouter
) -> None:
    route = mock.post(DEPLOY).mock(
        return_value=httpx.Response(503, json=[{"errorCode": "SERVER_UNAVAILABLE"}])
    )
    sf = h.client()
    with pytest.raises(esf.ServerError):
        h.run(sf.metadata.deploy(ZIP))
    assert route.call_count == 1


def test_deploy_resends_whole_body_after_token_refresh(h: Harness, mock: respx.MockRouter) -> None:
    """A rejected token (401 ``INVALID_SESSION_ID``) means nothing ran: refresh once and resend."""
    route = mock.post(DEPLOY).mock(
        side_effect=[
            httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "Session expired"}]),
            js("deploy_pending.json", 201),
        ]
    )
    mock.post(TOKEN_URL).respond(json=token_body("AT-refreshed-0002"))
    sf = h.client(cc_auth())
    res = h.run(sf.metadata.deploy(ZIP))
    assert res.id == DID
    assert route.call_count == 2
    assert parts(route.calls[1].request)["file"][1] == ZIP
    assert route.calls[1].request.headers["authorization"] == "Bearer AT-refreshed-0002"


def test_deploy_error_is_typed(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic fixture ``metadata/deploy_invalid_zip.json``."""
    mock.post(DEPLOY).mock(return_value=js("deploy_invalid_zip.json", 400))
    sf = h.client()
    with pytest.raises(esf.RequestError) as ei:
        h.run(sf.metadata.deploy(ZIP))
    assert ei.value.error_code == "INVALID_INPUT"


# --- deploy_status --------------------------------------------------------------------------


def test_deploy_status_parses_success(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic fixture ``metadata/deploy_succeeded.json``."""
    route = mock.get(STATUS).mock(return_value=js("deploy_succeeded.json"))
    sf = h.client()
    res = h.run(sf.metadata.deploy_status(DID))
    assert route.calls[0].request.url.params["includeDetails"] == "true"
    assert (res.status, res.done, res.success, res.failed) == ("Succeeded", True, True, False)
    assert (res.components_total, res.components_deployed) == (2, 2)
    assert res.component_successes[0] == esf.DeployMessage(
        component_type="ApexClass", full_name="Invoice", file_name="classes/Invoice.cls"
    )
    assert res.raw["createdByName"] == "Ada Admin"
    res.raise_for_errors()  # no error


def test_deploy_status_returns_failures_as_a_typed_result(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic fixture ``metadata/deploy_failed.json``: ``componentFailures`` as one object."""
    mock.get(STATUS).mock(return_value=js("deploy_failed.json"))
    sf = h.client()
    res = h.run(sf.metadata.deploy_status(DID))
    assert res.failed
    assert res.component_failures == (
        esf.DeployMessage(
            component_type="ApexClass",
            full_name="Invoice",
            file_name="classes/Invoice.cls",
            problem="Variable does not exist: total",
            problem_type="Error",
            line=12,
            column=9,
        ),
    )
    assert res.test_failures == (
        esf.DeployTestFailure(
            class_name="InvoiceTest",
            method_name="testTotal",
            message="System.AssertException: Assertion Failed",
            stack_trace="Class.InvoiceTest.testTotal: line 5, column 1",
        ),
    )
    with pytest.raises(esf.DeployFailed) as ei:
        res.raise_for_errors()
    err = ei.value
    assert err.job_id == DID
    assert err.state == "Failed"
    assert err.result is res
    assert [e.message for e in err.errors] == [
        "ApexClass Invoice: Variable does not exist: total",
        "InvoiceTest.testTotal: System.AssertException: Assertion Failed",
    ]
    assert err.errors[0].extended == {"file_name": "classes/Invoice.cls", "line": 12, "column": 9}
    assert "Variable does not exist" in str(err)


def test_deploy_status_wait_polls_until_done(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.get(STATUS).mock(
        side_effect=[js("deploy_pending.json"), js("deploy_in_progress.json"), js("deploy_succeeded.json")]
    )
    sf = h.client(recorder=rec)
    res = h.run(sf.metadata.deploy_status(DID, wait=True))
    assert res.status == "Succeeded"
    assert route.call_count == 3
    assert sf.sleeps == [2.0, 4.0]  # esf.Backoff() by default
    assert {e["operation"] for e in rec.requests} == {"metadata.deploy_status"}


def test_deploy_status_wait_times_out(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(STATUS).mock(return_value=js("deploy_in_progress.json"))
    sf = h.client()
    with pytest.raises(esf.MetadataTimeout) as ei:
        h.run(sf.metadata.deploy_status(DID, wait=True, timeout=5))
    assert ei.value.job_id == DID
    assert ei.value.state == "InProgress"
    assert sum(sf.sleeps) == pytest.approx(5)
    assert sf.sleeps == [2.0, 3.0]


def test_deploy_status_wait_uses_poll(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(STATUS).mock(
        side_effect=[js("deploy_pending.json"), js("deploy_in_progress.json"), js("deploy_succeeded.json")]
    )
    sf = h.client()
    h.run(sf.metadata.deploy_status(DID, wait=True, poll=esf.Backoff(start=5, max=7)))
    assert sf.sleeps == [5.0, 7.0]


def test_deploy_status_default_timeout_is_an_hour(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(STATUS).mock(return_value=js("deploy_in_progress.json"))
    sf = h.client()
    with pytest.raises(esf.MetadataTimeout, match="after 3600 s"):
        h.run(sf.metadata.deploy_status(DID, wait=True, poll=esf.Backoff(start=900, max=900)))
    assert sf.sleeps == [900.0] * 4


# --- quick deploy and cancel ------------------------------------------------------------------


def test_quick_deploy_posts_the_validated_id(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Synthetic fixture ``metadata/quick_deploy_pending.json``."""
    route = mock.post(f"{DEPLOY}/{DID}").mock(return_value=js("quick_deploy_pending.json", 201))
    sf = h.client(recorder=rec)
    res = h.run(sf.metadata.quick_deploy(DID))
    assert (res.id, res.status) == ("0Afxx0000000002AAA", "Pending")
    request = route.calls[0].request
    assert request.method == "POST"
    assert json.loads(request.content) == {"validatedDeployRequestId": DID}
    assert request.headers["content-type"] == "application/json"
    (event,) = rec.requests
    assert event["operation"] == "metadata.quick_deploy"
    assert event["path_template"] == "/metadata/deployRequest/:id"


def test_quick_deploy_is_never_resent(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(f"{DEPLOY}/{DID}").respond(503, json=[{"errorCode": "SERVER_UNAVAILABLE"}])
    sf = h.client()
    with pytest.raises(esf.ServerError):
        h.run(sf.metadata.quick_deploy(DID))
    assert route.call_count == 1


def test_quick_deploy_validates_the_id(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(url__startswith=DEPLOY)
    sf = h.client()
    with pytest.raises(ValueError):
        h.run(sf.metadata.quick_deploy("../limits"))
    assert not route.called


def test_cancel_deploy_patches_canceling(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Synthetic fixture ``metadata/deploy_canceling.json``."""
    route = mock.patch(f"{DEPLOY}/{DID}").mock(return_value=js("deploy_canceling.json", 202))
    sf = h.client(recorder=rec)
    res = h.run(sf.metadata.cancel_deploy(DID))
    assert (res.id, res.status, res.done) == (DID, "Canceling", False)
    assert json.loads(route.calls[0].request.content) == {"deployResult": {"status": "Canceling"}}
    (event,) = rec.requests
    assert event["operation"] == "metadata.cancel_deploy"
    assert event["method"] == "PATCH"


def test_cancel_deploy_while_finalizing_raises(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic fixture ``metadata/cancel_finalizing.json``: the refusal from API 65.0 (V85)."""
    route = mock.patch(f"{DEPLOY}/{DID}").mock(return_value=js("cancel_finalizing.json", 400))
    sf = h.client()
    with pytest.raises(esf.DeployNotCancelable) as ei:
        h.run(sf.metadata.cancel_deploy(DID))
    err = ei.value
    assert isinstance(err, esf.RequestError)
    assert (err.job_id, err.state) == (DID, "FinalizingDeploy")
    assert err.error_code == "INVALID_ID_FIELD"
    assert err.status == 400
    assert err.operation == "metadata.cancel_deploy"
    assert err.retryable is False
    assert err.docs_url is not None
    assert err.docs_url.endswith("#deploynotcancelable")
    assert route.call_count == 1


def test_cancel_deploy_finalizing_status_raises(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic fixture ``metadata/deploy_finalizing.json``: a 2xx answer that is still finalizing."""
    mock.patch(f"{DEPLOY}/{DID}").mock(return_value=js("deploy_finalizing.json", 202))
    sf = h.client()
    with pytest.raises(esf.DeployNotCancelable) as ei:
        h.run(sf.metadata.cancel_deploy(DID))
    assert (ei.value.job_id, ei.value.state, ei.value.status) == (DID, "FinalizingDeploy", 202)


def test_cancel_deploy_other_errors_are_unchanged(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.patch(f"{DEPLOY}/{DID}").respond(
        404, json=[{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]
    )
    sf = h.client()
    with pytest.raises(esf.NotFound):
        h.run(sf.metadata.cancel_deploy(DID))
    route.respond(503, json=[{"errorCode": "SERVER_UNAVAILABLE", "message": "finalizing maintenance"}])
    with pytest.raises(esf.ServerError):  # not a RequestError: never DeployNotCancelable
        h.run(sf.metadata.cancel_deploy(DID))
    assert route.call_count == 2  # the cancel is never resent


def test_deploy_status_read_is_retried(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(STATUS).mock(
        side_effect=[
            httpx.Response(503, json=[{"errorCode": "SERVER_UNAVAILABLE"}]),
            js("deploy_succeeded.json"),
        ]
    )
    sf = h.client()
    assert h.run(sf.metadata.deploy_status(DID)).success
    assert route.call_count == 2


def test_deploy_status_unknown_status_is_unexpected(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic fixture ``metadata/deploy_unknown_status.json``."""
    mock.get(STATUS).mock(return_value=js("deploy_unknown_status.json"))
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="Teleporting"):
        h.run(sf.metadata.deploy_status(DID))


def test_deploy_status_validates_arguments(h: Harness) -> None:
    sf = h.client()
    with pytest.raises(ValueError):
        h.run(sf.metadata.deploy_status("../limits"))
    with pytest.raises(ValueError, match="timeout"):
        h.run(sf.metadata.deploy_status(DID, wait=True, timeout=0))


# --- retrieve -------------------------------------------------------------------------------


def retrieve_routes(mock: respx.MockRouter, final: str = "retrieve_succeeded.json") -> respx.Route:
    def answer(request: httpx.Request) -> httpx.Response:
        call = body_of(request)
        if call.tag == f"{M}retrieve":
            return xml("retrieve_started.json")
        assert call.tag == f"{M}checkRetrieveStatus"
        if call.findtext(f"{M}includeZip") == "true":
            return xml(final)
        answer.polls += 1  # type: ignore[attr-defined]
        return xml("retrieve_in_progress.json" if answer.polls == 1 else "retrieve_done.json")  # type: ignore[attr-defined]

    answer.polls = 0  # type: ignore[attr-defined]
    return mock.post(SOAP).mock(side_effect=answer)


def test_retrieve_polls_then_fetches_the_zip_once(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Synthetic fixtures ``metadata/retrieve_*.json``."""
    route = retrieve_routes(mock)
    sf = h.client(recorder=rec)
    res = h.run(sf.metadata.retrieve({"ApexClass": ["Invoice", "Missing"], "CustomObject": ["*"]}))
    calls = [body_of(c.request) for c in route.calls]
    assert [c.tag.removeprefix(M) for c in calls] == [
        "retrieve",
        "checkRetrieveStatus",
        "checkRetrieveStatus",
        "checkRetrieveStatus",
    ]
    assert [c.findtext(f"{M}includeZip") for c in calls[1:]] == ["false", "false", "true"]
    assert all(c.findtext(f"{M}asyncProcessId") == RID for c in calls[1:])
    req = calls[0].find(f"{M}retrieveRequest")
    assert req is not None
    assert req.findtext(f"{M}apiVersion") == API
    assert req.findtext(f"{M}singlePackage") == "true"
    types = [
        (t.findtext(f"{M}name"), [m.text for m in t.findall(f"{M}members")])
        for t in req.iterfind(f"{M}unpackaged/{M}types")
    ]
    assert types == [("ApexClass", ["Invoice", "Missing"]), ("CustomObject", ["*"])]
    assert "authorization" not in route.calls[0].request.headers
    assert session_of(route.calls[0].request) == "AT-initial-0001"
    assert sf.sleeps == [2.0]
    with zipfile.ZipFile(io.BytesIO(res.zip_file)) as zf:
        assert "classes/Invoice.cls" in zf.namelist()
    assert res.id == RID
    assert [f.full_name for f in res.file_properties] == ["Invoice", "package.xml"]
    assert res.file_properties[0].type == "ApexClass"
    assert res.file_properties[0].last_modified_by_name == "Ada Admin"
    assert res.file_properties[1].id is None
    assert res.messages == (
        esf.RetrieveMessage(
            "unpackaged/package.xml", "Entity of type 'ApexClass' named 'Missing' cannot be found"
        ),
    )
    ops = [e["operation"] for e in rec.requests]
    assert ops[0] == "metadata.retrieve"
    assert set(ops[1:]) == {"metadata.retrieve_status"}
    assert rec.requests[0]["path_template"] == f"/services/Soap/m/{API}"


def test_retrieve_accepts_package_xml_text(h: Harness, mock: respx.MockRouter) -> None:
    route = retrieve_routes(mock)
    sf = h.client()
    h.run(sf.metadata.retrieve(PACKAGE.decode()))
    t = body_of(route.calls[0].request).find(f"{M}retrieveRequest/{M}unpackaged/{M}types")
    assert t is not None
    assert t.findtext(f"{M}name") == "ApexClass"
    assert t.findtext(f"{M}members") == "Invoice"


def test_retrieve_extracts_files(h: Harness, mock: respx.MockRouter, tmp_path: Path) -> None:
    retrieve_routes(mock)
    sf = h.client()
    h.run(sf.metadata.retrieve({"ApexClass": ["Invoice"]}, extract_to=tmp_path / "out"))
    assert (tmp_path / "out" / "classes" / "Invoice.cls").read_bytes() == b"public class Invoice {}\n"
    assert (tmp_path / "out" / "package.xml").exists()


def zip_response(data: bytes) -> str:
    body = load_fixture("metadata/retrieve_succeeded.json")
    return re.sub(r"<zipFile>[^<]*</zipFile>", f"<zipFile>{base64.b64encode(data).decode()}</zipFile>", body)


@pytest.mark.parametrize(
    "member",
    ["../evil.txt", "/etc/evil.txt", "classes/../../evil.txt", "C:/evil.txt", "classes\\..\\evil.txt"],
)
def test_retrieve_refuses_zip_slip(h: Harness, mock: respx.MockRouter, tmp_path: Path, member: str) -> None:
    """A zip entry that would land outside ``extract_to`` stops the extraction before any write."""
    evil = make_zip({"package.xml": PACKAGE, member: b"pwned"})
    mock.post(SOAP).mock(
        side_effect=[
            xml("retrieve_started.json"),
            xml("retrieve_done.json"),
            httpx.Response(200, content=zip_response(evil).encode(), headers={"Content-Type": "text/xml"}),
        ]
    )
    sf = h.client()
    out = tmp_path / "a" / "b"
    with pytest.raises(esf.UnexpectedResponse, match="refused"):
        h.run(sf.metadata.retrieve({"ApexClass": ["*"]}, extract_to=out))
    assert not (out / "package.xml").exists()
    assert not list(tmp_path.rglob("evil.txt"))


def test_retrieve_refuses_symlink_entries(h: Harness, mock: respx.MockRouter, tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("classes/link.cls")
        info.external_attr = 0o120777 << 16  # S_IFLNK
        zf.writestr(info, "/etc/passwd")
    mock.post(SOAP).mock(
        side_effect=[
            xml("retrieve_started.json"),
            xml("retrieve_done.json"),
            httpx.Response(
                200, content=zip_response(buf.getvalue()).encode(), headers={"Content-Type": "text/xml"}
            ),
        ]
    )
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="symlink"):
        h.run(sf.metadata.retrieve({"ApexClass": ["*"]}, extract_to=tmp_path / "out"))
    assert not (tmp_path / "out" / "classes").exists()


@posix_only
def test_retrieve_refuses_existing_symlink_in_target(
    h: Harness, mock: respx.MockRouter, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", out / "classes")
    retrieve_routes(mock)
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="outside"):
        h.run(sf.metadata.retrieve({"ApexClass": ["*"]}, extract_to=out))
    assert not list((tmp_path / "elsewhere").iterdir())


def test_retrieve_bad_zip_is_unexpected(h: Harness, mock: respx.MockRouter, tmp_path: Path) -> None:
    mock.post(SOAP).mock(
        side_effect=[
            xml("retrieve_started.json"),
            xml("retrieve_done.json"),
            httpx.Response(
                200, content=zip_response(b"not a zip").encode(), headers={"Content-Type": "text/xml"}
            ),
        ]
    )
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="not a zip"):
        h.run(sf.metadata.retrieve({"ApexClass": ["*"]}, extract_to=tmp_path / "out"))


def test_retrieve_failed_raises(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic fixture ``metadata/retrieve_failed.json``: no zip fetch after ``Failed``."""
    route = mock.post(SOAP).mock(side_effect=[xml("retrieve_started.json"), xml("retrieve_failed.json")])
    sf = h.client()
    with pytest.raises(esf.RetrieveFailed) as ei:
        h.run(sf.metadata.retrieve({"ApexKlass": ["*"]}))
    assert ei.value.job_id == RID
    assert ei.value.state == "Failed"
    assert ei.value.error_code == "UNKNOWN_EXCEPTION"
    assert "ApexKlass" in ei.value.message
    assert route.call_count == 2


def test_retrieve_without_zip_raises(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(SOAP).mock(
        side_effect=[xml("retrieve_started.json"), xml("retrieve_done.json"), xml("retrieve_done.json")]
    )
    sf = h.client()
    with pytest.raises(esf.RetrieveFailed, match="without a zip"):
        h.run(sf.metadata.retrieve({"ApexClass": ["*"]}))


def test_retrieve_times_out(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(SOAP).mock(side_effect=[xml("retrieve_started.json")] + [xml("retrieve_in_progress.json")] * 10)
    sf = h.client()
    with pytest.raises(esf.MetadataTimeout) as ei:
        h.run(sf.metadata.retrieve({"ApexClass": ["*"]}, timeout=3))
    assert (ei.value.job_id, ei.value.state) == (RID, "InProgress")


def test_retrieve_uses_poll(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(SOAP).mock(side_effect=[xml("retrieve_started.json")] + [xml("retrieve_in_progress.json")] * 10)
    sf = h.client()
    with pytest.raises(esf.MetadataTimeout):
        h.run(sf.metadata.retrieve({"ApexClass": ["*"]}, poll=esf.Backoff(start=4, max=4), timeout=10))
    assert sf.sleeps == [4.0, 4.0, 2.0]


def test_retrieve_start_is_not_resent_but_polls_are(h: Harness, mock: respx.MockRouter) -> None:
    unavailable = httpx.Response(503, json=[{"errorCode": "SERVER_UNAVAILABLE"}])
    route = mock.post(SOAP).mock(return_value=unavailable)
    sf = h.client()
    with pytest.raises(esf.ServerError):
        h.run(sf.metadata.retrieve({"ApexClass": ["*"]}))
    assert route.call_count == 1

    route.mock(
        side_effect=[
            xml("retrieve_started.json"),
            unavailable,
            xml("retrieve_done.json"),
            unavailable,  # the call that carries the zip is never resent
        ]
    )
    with pytest.raises(esf.ServerError):
        h.run(sf.metadata.retrieve({"ApexClass": ["*"]}))
    assert route.call_count == 5


def test_retrieve_validates_package(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(SOAP)
    sf = h.client()
    with pytest.raises(TypeError, match="list"):
        h.run(sf.metadata.retrieve({"ApexClass": "*"}))  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="no members"):
        h.run(sf.metadata.retrieve({"ApexClass": []}))
    with pytest.raises(ValueError, match="type name"):
        h.run(sf.metadata.retrieve({"Apex Class": ["*"]}))
    with pytest.raises(ValueError, match="package.xml"):
        h.run(sf.metadata.retrieve("<NotAPackage/>"))
    with pytest.raises(ValueError, match="timeout"):
        h.run(sf.metadata.retrieve({"ApexClass": ["*"]}, timeout=-1))
    assert not route.called


def test_retrieve_rejects_package_xml_with_entities(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(SOAP)
    sf = h.client()
    bomb = (
        '<!DOCTYPE p [<!ENTITY a "aaaa">]>'
        "<Package><types><members>&a;</members><name>ApexClass</name></types></Package>"
    )
    with pytest.raises(ValueError, match="DTD"):
        h.run(sf.metadata.retrieve(bomb))
    assert not route.called


# --- list and describe ----------------------------------------------------------------------


def test_list_metadata(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Synthetic fixture ``metadata/list_metadata.json``."""
    route = mock.post(SOAP).mock(return_value=xml("list_metadata.json"))
    sf = h.client(recorder=rec)
    items = h.run(sf.metadata.list_metadata("ApexClass"))
    q = body_of(route.calls[0].request)
    assert q.tag == f"{M}listMetadata"
    assert q.findtext(f"{M}queries/{M}type") == "ApexClass"
    assert q.find(f"{M}queries/{M}folder") is None
    assert q.findtext(f"{M}asOfVersion") == API
    assert [(i.full_name, i.namespace_prefix, i.manageable_state) for i in items] == [
        ("Invoice", None, "unmanaged"),
        ("acme__Billing", "acme", "installed"),
    ]
    assert items[0] == esf.FileProperties(
        type="ApexClass",
        full_name="Invoice",
        file_name="classes/Invoice.cls",
        id="01pxx0000000001AAA",
        manageable_state="unmanaged",
        created_by_name="Ada Admin",
        created_date="2026-11-01T09:00:00.000Z",
        last_modified_by_name="Ada Admin",
        last_modified_date="2026-11-02T09:00:00.000Z",
    )
    assert rec.requests[0]["operation"] == "metadata.list"


def test_list_metadata_with_folder_and_empty_result(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(SOAP).mock(return_value=xml("list_metadata_empty.json"))
    sf = h.client()
    assert h.run(sf.metadata.list_metadata("Report", folder="Sales_Reports")) == []
    assert body_of(route.calls[0].request).findtext(f"{M}queries/{M}folder") == "Sales_Reports"


def test_list_metadata_is_a_read_and_is_retried(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(SOAP).mock(
        side_effect=[
            httpx.Response(503, json=[{"errorCode": "SERVER_UNAVAILABLE"}]),
            xml("list_metadata.json"),
        ]
    )
    sf = h.client()
    assert len(h.run(sf.metadata.list_metadata("ApexClass"))) == 2
    assert route.call_count == 2


def test_list_metadata_fault_is_typed(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic fixture ``metadata/fault_invalid_type.json`` (metadata namespace fault)."""
    mock.post(SOAP).mock(return_value=xml("fault_invalid_type.json", 500))
    sf = h.client()
    with pytest.raises(esf.InvalidType) as ei:
        h.run(sf.metadata.list_metadata("ApexKlass"))
    assert ei.value.error_code == "INVALID_TYPE"
    assert ei.value.message == "Unknown type: ApexKlass"


def test_list_metadata_validates_before_sending(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(SOAP)
    sf = h.client()
    with pytest.raises(ValueError):
        h.run(sf.metadata.list_metadata("Apex<Class"))
    with pytest.raises(ValueError):
        h.run(sf.metadata.list_metadata("Report", folder="bad\x00folder"))
    assert not route.called


def test_invalid_session_fault_refreshes_and_resends(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic fixture ``metadata/fault_invalid_session.json``."""
    route = mock.post(SOAP).mock(
        side_effect=[xml("fault_invalid_session.json", 500), xml("describe_metadata.json")]
    )
    mock.post(TOKEN_URL).respond(json=token_body("AT-refreshed-0002"))
    sf = h.client(cc_auth())
    h.run(sf.metadata.describe_metadata())
    assert [session_of(c.request) for c in route.calls] == ["AT-initial-0001", "AT-refreshed-0002"]


def test_describe_metadata(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic fixture ``metadata/describe_metadata.json``."""
    route = mock.post(SOAP).mock(return_value=xml("describe_metadata.json"))
    sf = h.client()
    d = h.run(sf.metadata.describe_metadata())
    assert body_of(route.calls[0].request).findtext(f"{M}asOfVersion") == API
    assert d.partial_save_allowed is True
    assert d.test_required is False
    assert d.organization_namespace is None
    assert d.types[0] == esf.MetadataType(
        xml_name="ApexClass", directory_name="classes", suffix="cls", in_folder=False, meta_file=True
    )
    assert d.types[1].child_xml_names == ("CustomField", "RecordType")
    assert d.types[2].in_folder is True


def test_soap_response_without_wrapper_is_unexpected(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(SOAP).mock(return_value=xml("list_metadata.json"))
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="describeMetadataResponse"):
        h.run(sf.metadata.describe_metadata())
