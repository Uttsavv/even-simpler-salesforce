"""Pure Metadata API helpers (``core/metadata.py``) and the metadata models. No network.

Response shapes follow the Metadata API Developer Guide (read from search excerpts on
2026-11-11; developer.salesforce.com returns 403 to non-browser clients). Every body here is
**synthetic**:

- DeployResult: https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_deployresult.htm
- checkRetrieveStatus(): https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_checkretrievestatus.htm
"""

from __future__ import annotations

import pickle
import xml.etree.ElementTree as ET
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core import metadata as md

ENV = (
    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns="http://soap.sforce.com/2006/04/metadata"><soapenv:Body>'
)
END = "</soapenv:Body></soapenv:Envelope>"


def soap(inner: str) -> bytes:
    return (ENV + inner + END).encode()


# --- deploy body and result ---------------------------------------------------------------------


def test_deploy_body_defaults() -> None:
    assert md.deploy_body(esf.DeployOptions()) == {
        "deployOptions": {
            "checkOnly": False,
            "ignoreWarnings": False,
            "purgeOnDelete": False,
            "allowMissingFiles": False,
            "autoUpdatePackage": False,
            "singlePackage": True,
        }
    }


def test_deploy_multipart_shape() -> None:
    head, tail, ctype = md.deploy_multipart(b'{"a":1}', "B")
    assert ctype == 'multipart/form-data; boundary="B"'
    assert head.startswith(b'--B\r\nContent-Disposition: form-data; name="json"\r\n')
    assert head.endswith(b"Content-Type: application/zip\r\n\r\n")
    assert b'{"a":1}\r\n--B\r\n' in head
    assert tail == b"\r\n--B--\r\n"


def test_deploy_options_validation() -> None:
    assert esf.DeployOptions(test_level="RunSpecifiedTests", run_tests=["A", "B"]).run_tests == ("A", "B")
    with pytest.raises(TypeError):
        esf.DeployOptions(test_level="RunSpecifiedTests", run_tests="ATest")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="test_level"):
        esf.DeployOptions(test_level="RunSomeTests")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="go together"):
        esf.DeployOptions(run_tests=["ATest"])
    with pytest.raises(ValueError, match="go together"):
        esf.DeployOptions(test_level="RunSpecifiedTests")


@pytest.mark.parametrize("body", [None, [], "x"])
def test_parse_deploy_result_rejects_non_objects(body: Any) -> None:
    with pytest.raises(esf.UnexpectedResponse):
        md.parse_deploy_result(body)


def test_parse_deploy_result_needs_an_id() -> None:
    with pytest.raises(esf.UnexpectedResponse, match="no id"):
        md.parse_deploy_result({"deployResult": {"status": "Pending"}})


def test_parse_deploy_result_tolerates_missing_and_text_values() -> None:
    res = md.parse_deploy_result(
        {
            "deployResult": {
                "id": "0Afxx0000000001AAA",
                "status": "Succeeded",
                "success": "true",
                "numberComponentsTotal": "3",
                "numberComponentErrors": True,
                "numberTestsTotal": "n/a",
                "details": {
                    "componentFailures": ["junk", {"fullName": "A", "lineNumber": "7", "columnNumber": ""}],
                    "runTestResult": "junk",
                },
            }
        }
    )
    assert res.id == "0Afxx0000000001AAA"
    assert res.done is True  # no "done": derived from the final status
    assert res.success is True
    assert (res.components_total, res.component_errors, res.tests_total) == (3, 1, 0)
    assert res.component_failures == (esf.DeployMessage(full_name="A", line=7),)
    assert res.test_failures == ()


def test_parse_deploy_result_accepts_finalizing_as_in_progress() -> None:
    """Org-observed (DE org, API v68.0, 2026-11-12): a REST quick deploy reported ``Finalizing``
    while finishing; ``tests/sandbox/test_tooling_metadata.py::test_quick_deploy`` hit it."""
    res = md.parse_deploy_result({"id": "0Af000000000001AAA", "deployResult": {"status": "Finalizing"}})
    assert res.status == "Finalizing"
    assert not res.done
    assert not res.failed


def test_parse_deploy_result_accepts_one_object_instead_of_a_list() -> None:
    """``details`` lists can arrive as a single object (no recording has one; kept for safety)."""
    res = md.parse_deploy_result(
        {
            "id": "0Af000000000001AAA",
            "deployResult": {
                "status": "Failed",
                "details": {
                    "componentFailures": {
                        "componentType": "ApexClass",
                        "fullName": "Broken",
                        "problem": "boom",
                        "problemType": "Error",
                    }
                },
            },
        }
    )
    assert [f.full_name for f in res.component_failures] == ["Broken"]


def test_parse_deploy_result_without_result_defaults_to_pending() -> None:
    res = md.parse_deploy_result({"id": "0Afxx0000000001AAA"})
    assert (res.status, res.done, res.failed) == ("Pending", False, False)


def test_deploy_result_failed_states() -> None:
    assert esf.DeployResult("0Af", "SucceededPartial", done=True, success=True).failed
    assert esf.DeployResult("0Af", "Canceled", done=True).failed
    assert esf.DeployResult("0Af", "Succeeded", done=True, success=False).failed
    assert not esf.DeployResult("0Af", "InProgress").failed
    esf.DeployResult("0Af", "InProgress").raise_for_errors()  # still running: nothing to raise
    # Recorded (metadata/deploy_test_failure.json): with rollbackOnError off Salesforce says
    # Succeeded and success=true although a test failed.
    assert esf.DeployResult("0Af", "Succeeded", done=True, success=True, test_errors=1).failed
    failure = esf.DeployTestFailure(class_name="T", method_name="m", message="boom")
    assert esf.DeployResult("0Af", "Succeeded", done=True, success=True, test_failures=(failure,)).failed
    assert not esf.DeployResult("0Af", "Succeeded", done=True, success=True).failed
    assert not esf.DeployResult("0Af", "InProgress", test_errors=1).failed  # not done yet


def test_deploy_failed_from_error_message_only() -> None:
    res = esf.DeployResult(
        "0Af", "Failed", done=True, error_message="No package.xml found", error_status_code="X"
    )
    with pytest.raises(esf.DeployFailed, match="No package.xml found") as ei:
        res.raise_for_errors()
    assert ei.value.error_code == "X"
    assert ei.value.operation == "metadata.deploy"


def test_deploy_failed_without_details() -> None:
    with pytest.raises(esf.DeployFailed, match="no details"):
        esf.DeployResult("0Af", "Canceled", done=True).raise_for_errors()


def test_deploy_failed_message_with_missing_parts() -> None:
    res = esf.DeployResult(
        "0Af",
        "SucceededPartial",
        done=True,
        success=True,
        component_failures=(esf.DeployMessage(),),
        test_failures=(esf.DeployTestFailure(),),
    )
    with pytest.raises(esf.DeployFailed) as ei:
        res.raise_for_errors()
    assert [e.message for e in ei.value.errors] == ["? ?", "?.?"]
    assert ei.value.errors[0].fields == ()


def test_deploy_failed_pickles_with_result() -> None:
    res = esf.DeployResult("0Afxx0000000001AAA", "Failed", done=True, error_message="boom")
    with pytest.raises(esf.DeployFailed) as ei:
        res.raise_for_errors()
    back = pickle.loads(pickle.dumps(ei.value))
    assert back.result == res
    assert back.job_id == res.id


# --- package.xml ---------------------------------------------------------------------------------


def test_package_types_from_mapping_and_xml() -> None:
    assert md.package_types({"ApexClass": ("A", "B"), "CustomObject": ["*"]}) == (
        ("ApexClass", ("A", "B")),
        ("CustomObject", ("*",)),
    )
    xml = (
        b'<Package xmlns="http://soap.sforce.com/2006/04/metadata"><types><members> Account </members>'
        b"<name>CustomObject</name></types><version>68.0</version></Package>"
    )
    assert md.package_types(xml) == (("CustomObject", ("Account",)),)


@pytest.mark.parametrize(
    ("package", "error", "match"),
    [
        ({}, ValueError, "no metadata types"),
        ("<Package/>", ValueError, "no metadata types"),
        ("not xml", ValueError, "not a valid package.xml"),
        ("<Other/>", ValueError, "<Package>"),
        ({"ApexClass": b"*"}, TypeError, "list"),
        ({"ApexClass": [""]}, ValueError, "member name"),
        ({"ApexClass": ["a\x01"]}, ValueError, "member name"),
        ({1: ["*"]}, ValueError, "type name"),
        ("<Package><types><members>*</members></types></Package>", ValueError, "type name"),
    ],
)
def test_package_types_rejects(package: Any, error: type[Exception], match: str) -> None:
    with pytest.raises(error, match=match):
        md.package_types(package)


# --- zip paths -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("package.xml", "package.xml"),
        ("./classes//A.cls", "classes/A.cls"),
        ("classes/", "classes/"),
    ],
)
def test_safe_member_accepts(name: str, expected: str) -> None:
    assert md.safe_member(name) == expected


@pytest.mark.parametrize(
    "name", ["", ".", "/abs", "C:evil", "c:/evil", "a/../../b", "..", "a\\b", "a\x00b", "./"]
)
def test_safe_member_rejects(name: str) -> None:
    with pytest.raises(ValueError):
        md.safe_member(name)


def test_is_symlink_entry() -> None:
    assert md.is_symlink_entry(0o120777 << 16)
    assert not md.is_symlink_entry(0o100644 << 16)
    assert not md.is_symlink_entry(0)


# --- SOAP envelopes ------------------------------------------------------------------------------


def test_envelopes_escape_values() -> None:
    env = md.retrieve_envelope((("ApexClass", ("A&B",)),), "68.0", "tok<en>")
    root = ET.fromstring(env)
    ns = "{http://soap.sforce.com/2006/04/metadata}"
    assert root.findtext(f".//{ns}sessionId") == "tok<en>"
    assert root.findtext(f".//{ns}members") == "A&B"
    lm = ET.fromstring(md.list_metadata_envelope("Report", "F&G", "68.0", "t"))
    assert lm.findtext(f".//{ns}folder") == "F&G"
    assert ET.fromstring(md.describe_metadata_envelope("68.0", "t")).findtext(f".//{ns}asOfVersion") == "68.0"
    crs = ET.fromstring(md.check_retrieve_status_envelope("09S", True, "t"))
    assert crs.findtext(f".//{ns}includeZip") == "true"
    assert md.metadata_soap_path("68.0") == "/services/Soap/m/68.0"


# --- SOAP parsers --------------------------------------------------------------------------------


def test_parse_async_id_rejects_bad_ids() -> None:
    with pytest.raises(esf.UnexpectedResponse, match="job id"):
        md.parse_async_id(soap("<retrieveResponse><result><id>../x</id></result></retrieveResponse>"))
    with pytest.raises(esf.UnexpectedResponse, match="job id"):
        md.parse_async_id(soap("<retrieveResponse><result><done>false</done></result></retrieveResponse>"))
    with pytest.raises(esf.UnexpectedResponse, match="expected 1 result"):
        md.parse_async_id(soap("<retrieveResponse/>"))
    with pytest.raises(esf.UnexpectedResponse, match="no retrieveResponse"):
        md.parse_async_id(soap("<other/>"))


def test_parse_retrieve_status_rejects_unknown_status_and_bad_zip() -> None:
    def body(inner: str) -> bytes:
        return soap(f"<checkRetrieveStatusResponse><result>{inner}</result></checkRetrieveStatusResponse>")

    with pytest.raises(esf.UnexpectedResponse, match="unknown retrieve status"):
        md.parse_retrieve_status(body("<status>Queued</status>"))
    with pytest.raises(esf.UnexpectedResponse, match="base64"):
        md.parse_retrieve_status(body("<status>Succeeded</status><zipFile>!!!</zipFile>"))
    ok = md.parse_retrieve_status(
        body("<done>true</done><status>Succeeded</status><zipFile>UEsF\nBg==</zipFile>")
    )
    assert ok["zip_file"] == b"PK\x05\x06"
    assert ok["done"] is True
    assert ok["messages"] == ()


def test_parse_list_and_describe_defaults() -> None:
    items = md.parse_list_metadata(soap("<listMetadataResponse><result/></listMetadataResponse>"))
    assert items == [esf.FileProperties(type="", full_name="")]
    d = md.parse_describe_metadata(
        soap("<describeMetadataResponse><result><metadataObjects/></result></describeMetadataResponse>")
    )
    assert d.types == (esf.MetadataType(xml_name=""),)
    assert d.partial_save_allowed is False


def test_status_sets_are_documented_values() -> None:
    assert "SucceededPartial" in md.DEPLOY_STATUSES
    assert {"Pending", "InProgress", "Succeeded", "Failed"} == md.RETRIEVE_STATUSES


# --- quick deploy and cancel ------------------------------------------------------------------
# Quick deploy: https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_rest_deploy_recentvalidation.htm
# Cancel: https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_rest_deploy_cancel.htm

DID = "0Afxx0000000001AAA"


def test_deploy_request_bodies() -> None:
    assert md.deploy_request_path(DID) == f"metadata/deployRequest/{DID}"
    assert md.quick_deploy_body(DID) == {"validatedDeployRequestId": DID}
    assert md.cancel_deploy_body() == {"deployResult": {"status": "Canceling"}}
    with pytest.raises(ValueError):
        md.deploy_request_path("../limits")
    with pytest.raises(ValueError):
        md.quick_deploy_body("nope")


FINALIZING = "The deployment can't be canceled because it's in the finalizing stage."


@pytest.mark.parametrize(
    ("exc", "refused"),
    [
        (esf.RequestError(FINALIZING, error_code="INVALID_ID_FIELD", status=400), True),
        (esf.Conflict("Finalizing deploy in progress", status=409), True),
        (
            esf.RequestError(
                "two errors", errors=[esf.ErrorDetail("X", "other"), esf.ErrorDetail("Y", FINALIZING)]
            ),
            True,
        ),
        (esf.RequestError("invalid id", error_code="INVALID_ID_FIELD", status=400), False),
        (esf.NotFound("no deployment", status=404), False),
        (esf.ServerError("finalizing maintenance", status=503), False),
    ],
)
def test_cancel_refused(exc: esf.SalesforceError, refused: bool) -> None:
    assert md.cancel_refused(exc) is refused


def test_not_cancelable_keeps_the_details() -> None:
    cause = RuntimeError("inner")
    exc = esf.RequestError(
        FINALIZING, error_code="INVALID_ID_FIELD", status=400, operation="metadata.cancel_deploy"
    )
    exc.__cause__ = cause
    new = md.not_cancelable(exc, DID)
    assert type(new) is esf.DeployNotCancelable
    assert (new.job_id, new.state) == (DID, "FinalizingDeploy")
    assert (new.message, new.error_code, new.status, new.operation) == (
        FINALIZING,
        "INVALID_ID_FIELD",
        400,
        "metadata.cancel_deploy",
    )
    assert new.retryable is False
    assert new.docs_url is not None
    assert new.docs_url.endswith("#deploynotcancelable")
    assert new.__cause__ is cause
    clone = pickle.loads(pickle.dumps(new))
    assert (type(clone), clone.job_id) == (esf.DeployNotCancelable, DID)
