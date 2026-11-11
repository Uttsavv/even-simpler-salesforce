"""Metadata API (§9.15): deploy bodies, SOAP envelopes, parsers and zip-path checks. No I/O.

Deployments use the REST resource ``metadata/deployRequest``. Retrieve, ``listMetadata`` and
``describeMetadata`` have no REST resource, so they use the SOAP Metadata API at
``/services/Soap/m/{version}`` with the OAuth access token in ``SessionHeader`` (D14: SOAP is
internal only). Everything here is pure; the transport sends the requests.

Sources (Metadata API Developer Guide; developer.salesforce.com returns 403 to non-browser
clients, so these were read from search excerpts on 2026-11-11):

- Deploy with REST: https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_rest_deploy.htm
  (multipart ``POST metadata/deployRequest``: a ``json`` part with ``deployOptions`` and a
  ``file`` part with the zip, ``application/zip``; zip at most 39 MB, 10,000 files)
- Deploy status with REST: https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_rest_deploy_checkstatus.htm
  (``GET metadata/deployRequest/{id}?includeDetails=true``)
- DeployResult: https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_deployresult.htm
- retrieve(): https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_retrieve.htm
- checkRetrieveStatus(): https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_checkretrievestatus.htm
  (the zip is deleted from the server once it was returned; ``includeZip`` from API 34.0)
- listMetadata(): https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_listmetadata.htm
- describeMetadata(): https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_describe.htm
"""

from __future__ import annotations

import base64
import binascii
import re
import stat
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from typing import Any, cast
from xml.sax.saxutils import escape

from .. import errors as E
from ..errors import UnexpectedResponse
from ..models import (
    DeployMessage,
    DeployOptions,
    DeployResult,
    DeployTestFailure,
    FileProperties,
    MetadataDescribe,
    MetadataType,
    RetrieveMessage,
)
from .http import check_record_id
from .soap import NS_ENVELOPE, child_text, children, parse_xml, soap_body

__all__ = [
    "DEPLOY_STATUSES",
    "NS_METADATA",
    "RETRIEVE_STATUSES",
    "PackageTypes",
    "check_retrieve_status_envelope",
    "deploy_body",
    "deploy_multipart",
    "describe_metadata_envelope",
    "is_symlink_entry",
    "list_metadata_envelope",
    "metadata_soap_path",
    "package_types",
    "cancel_deploy_body",
    "cancel_refused",
    "not_cancelable",
    "deploy_request_path",
    "parse_async_id",
    "quick_deploy_body",
    "parse_deploy_result",
    "parse_describe_metadata",
    "parse_list_metadata",
    "parse_retrieve_status",
    "retrieve_envelope",
    "safe_member",
]

NS_METADATA = "http://soap.sforce.com/2006/04/metadata"

#: ``DeployResult.status`` values (newer API versions add the two ``Finalizing`` states).
DEPLOY_STATUSES = frozenset(
    {
        "Pending",
        "InProgress",
        "FinalizingDeploy",
        "FinalizingDeployFailed",
        "Succeeded",
        "SucceededPartial",
        "Failed",
        "Canceling",
        "Canceled",
    }
)
_DEPLOY_FINAL = frozenset({"Succeeded", "SucceededPartial", "Failed", "Canceled"})
#: ``RetrieveResult.status`` values.
RETRIEVE_STATUSES = frozenset({"Pending", "InProgress", "Succeeded", "Failed"})

#: ``((type, (member, ...)), ...)``: the ``types`` of a ``package.xml``.
PackageTypes = tuple[tuple[str, tuple[str, ...]], ...]

_TYPE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ASYNC_ID = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")
_RAW_LIMIT = 4096


def deploy_request_path(deploy_id: str) -> str:
    """``metadata/deployRequest/{id}``: one deployment (status, quick deploy, cancel)."""
    return f"metadata/deployRequest/{check_record_id(deploy_id)}"


def quick_deploy_body(validated_id: str) -> dict[str, Any]:
    """The ``POST metadata/deployRequest/{id}`` body that deploys a validated deployment.

    *Deploy a Recently Validated Component Set Without Tests*:
    https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_rest_deploy_recentvalidation.htm
    """
    return {"validatedDeployRequestId": check_record_id(validated_id)}


def cancel_deploy_body() -> dict[str, Any]:
    """The ``PATCH metadata/deployRequest/{id}`` body that asks Salesforce to cancel a deployment.

    *Cancel a Deployment in Progress Using REST*:
    https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_rest_deploy_cancel.htm
    """
    return {"deployResult": {"status": "Canceling"}}


def cancel_refused(exc: E.SalesforceError) -> bool:
    """Whether a refused cancel request means the deployment is already finalizing.

    From API 65.0 a deployment in the ``FinalizingDeploy`` stage can't be canceled. The SOAP
    ``cancelDeploy()`` call answers ``INVALID_ID_FIELD`` with a message about finalizing; the
    REST body is assumed to carry the same message (V85 in big-bang.md §21).
    """
    if not isinstance(exc, E.RequestError):
        return False
    return any("finaliz" in (m or "").lower() for m in (exc.message, *(e.message for e in exc.errors)))


def not_cancelable(exc: E.SalesforceError, deploy_id: str) -> E.DeployNotCancelable:
    """Turn a refused cancel request into :class:`DeployNotCancelable`, keeping its details."""
    state = dict(exc.__dict__)
    state["retryable"] = False
    state["docs_url"] = E.docs_url_for(E.DeployNotCancelable)
    state["job_id"] = deploy_id
    state["state"] = "FinalizingDeploy"
    new = E._rebuild(E.DeployNotCancelable, state)  # pyright: ignore[reportPrivateUsage]
    new.__cause__ = exc.__cause__
    return cast("E.DeployNotCancelable", new)


def metadata_soap_path(api_version: str) -> str:
    """The SOAP Metadata API endpoint path for ``api_version``."""
    return f"/services/Soap/m/{api_version}"


# --- deploy (REST) -----------------------------------------------------------------------------


def deploy_body(options: DeployOptions) -> dict[str, Any]:
    """The JSON part of a deploy request: ``{"deployOptions": {...}}``."""
    opts: dict[str, Any] = {
        "checkOnly": options.check_only,
        "ignoreWarnings": options.ignore_warnings,
        "purgeOnDelete": options.purge_on_delete,
        "allowMissingFiles": options.allow_missing_files,
        "autoUpdatePackage": options.auto_update_package,
        "singlePackage": options.single_package,
    }
    if options.rollback_on_error is not None:
        opts["rollbackOnError"] = options.rollback_on_error
    if options.test_level is not None:
        opts["testLevel"] = options.test_level
    if options.run_tests:
        opts["runTests"] = list(options.run_tests)
    return {"deployOptions": opts}


def deploy_multipart(options_json: bytes, boundary: str) -> tuple[bytes, bytes, str]:
    """Bytes around the zip of a deploy request.

    The body is ``head + <zip bytes> + tail``, so the zip is streamed, never copied.

    Returns:
        ``(head, tail, content_type)``.
    """
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="json"\r\n'
        "Content-Type: application/json\r\n\r\n"
    ).encode() + options_json
    head += (
        f"\r\n--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="deploy.zip"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head, tail, f'multipart/form-data; boundary="{boundary}"'


def _as_list(value: Any) -> list[Mapping[str, Any]]:
    """A JSON value that may be one object, a list of objects or missing."""
    if isinstance(value, Mapping):
        return [cast("Mapping[str, Any]", value)]
    if isinstance(value, list):
        return [cast("Mapping[str, Any]", v) for v in cast("list[Any]", value) if isinstance(v, Mapping)]
    return []


def _str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return 0


def _opt_int(value: Any) -> int | None:
    return _int(value) if value not in (None, "") else None


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _deploy_message(m: Mapping[str, Any]) -> DeployMessage:
    return DeployMessage(
        component_type=_str(m.get("componentType")),
        full_name=_str(m.get("fullName")),
        file_name=_str(m.get("fileName")),
        problem=_str(m.get("problem")),
        problem_type=_str(m.get("problemType")),
        line=_opt_int(m.get("lineNumber")),
        column=_opt_int(m.get("columnNumber")),
    )


def _test_failure(f: Mapping[str, Any]) -> DeployTestFailure:
    return DeployTestFailure(
        class_name=_str(f.get("name")),
        method_name=_str(f.get("methodName")),
        message=_str(f.get("message")),
        stack_trace=_str(f.get("stackTrace")),
    )


def parse_deploy_result(body: Any) -> DeployResult:
    """Parse the body of ``POST`` or ``GET metadata/deployRequest`` into a :class:`DeployResult`.

    ``details`` lists can arrive as one object instead of a list of one, so both are accepted.

    Raises:
        UnexpectedResponse: no deployment id, or a ``status`` Salesforce doesn't document.
    """
    if not isinstance(body, Mapping):
        raise UnexpectedResponse("deployRequest response is not a JSON object", operation="metadata.deploy")
    top = cast("Mapping[str, Any]", body)
    res_any: Any = top.get("deployResult")
    res: Mapping[str, Any] = cast("Mapping[str, Any]", res_any) if isinstance(res_any, Mapping) else {}
    deploy_id = _str(top.get("id")) or _str(res.get("id"))
    if deploy_id is None:
        raise UnexpectedResponse("deployRequest response has no id", operation="metadata.deploy")
    status = _str(res.get("status")) or "Pending"
    if status not in DEPLOY_STATUSES:
        raise UnexpectedResponse(f"unknown deploy status {status!r}", operation="metadata.deploy")
    details_any: Any = res.get("details")
    details: Mapping[str, Any] = (
        cast("Mapping[str, Any]", details_any) if isinstance(details_any, Mapping) else {}
    )
    tests_any: Any = details.get("runTestResult")
    tests: Mapping[str, Any] = cast("Mapping[str, Any]", tests_any) if isinstance(tests_any, Mapping) else {}
    done = _bool(res["done"]) if "done" in res else status in _DEPLOY_FINAL
    return DeployResult(
        id=deploy_id,
        status=status,
        done=done,
        success=_bool(res.get("success")),
        check_only=_bool(res.get("checkOnly")),
        components_total=_int(res.get("numberComponentsTotal")),
        components_deployed=_int(res.get("numberComponentsDeployed")),
        component_errors=_int(res.get("numberComponentErrors")),
        tests_total=_int(res.get("numberTestsTotal")),
        tests_completed=_int(res.get("numberTestsCompleted")),
        test_errors=_int(res.get("numberTestErrors")),
        error_message=_str(res.get("errorMessage")),
        error_status_code=_str(res.get("errorStatusCode")),
        component_failures=tuple(_deploy_message(m) for m in _as_list(details.get("componentFailures"))),
        component_successes=tuple(_deploy_message(m) for m in _as_list(details.get("componentSuccesses"))),
        test_failures=tuple(_test_failure(f) for f in _as_list(tests.get("failures"))),
        raw=dict(res),
    )


# --- package.xml and member names ----------------------------------------------------------------


def _check_text(value: str, what: str) -> str:
    if not value or _CONTROL.search(value):
        raise ValueError(f"{what} must be non-empty text without control characters, got {value!r}")
    return value


def _check_type(name: str) -> str:
    if not isinstance(name, str) or not _TYPE_NAME.fullmatch(name):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(f"invalid metadata type name: {name!r}")
    return name


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def package_types(package: str | bytes | Mapping[str, Iterable[str]]) -> PackageTypes:
    """The metadata types and members to retrieve.

    Args:
        package: The text of a ``package.xml``, or a mapping of type name to member names,
            e.g. ``{"ApexClass": ["*"], "CustomObject": ["Account"]}``.

    Raises:
        ValueError: not a ``package.xml``, an invalid type name, or a type without members.
        TypeError: members given as one string instead of a list.
    """
    pairs: list[tuple[str, list[str]]] = []
    if isinstance(package, Mapping):
        for name, members in package.items():
            if isinstance(members, (str, bytes)):
                raise TypeError(f"members of {name!r} must be a list of names, not one string")
            pairs.append((name, list(members)))
    else:
        try:
            root = parse_xml(package.encode() if isinstance(package, str) else package)
        except UnexpectedResponse as exc:
            raise ValueError(f"package is not a valid package.xml: {exc.message}") from None
        if _local(root.tag) != "Package":
            raise ValueError("package.xml must have a <Package> root element")
        for t in children(root, "types"):
            members = [(m.text or "").strip() for m in children(t, "members")]
            pairs.append((child_text(t, "name") or "", members))
    if not pairs:
        raise ValueError("package names no metadata types")
    out: list[tuple[str, tuple[str, ...]]] = []
    for name, members in pairs:
        _check_type(name)
        if not members:
            raise ValueError(f"metadata type {name!r} has no members (use ['*'] for all)")
        out.append((name, tuple(_check_text(m, "member name") for m in members)))
    return tuple(out)


def safe_member(name: str) -> str:
    """Validate one path inside a zip file: relative, no ``..``, no drive letter.

    Returns:
        The path with ``/`` separators and no leading ``./``.

    Raises:
        ValueError: an absolute path, a ``..`` part, a backslash, a drive letter or a control
            character. Writing such a member could escape the target folder (zip slip).
    """
    if not name or _CONTROL.search(name) or "\\" in name:
        raise ValueError(f"unsafe path in zip: {name!r}")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise ValueError(f"absolute path in zip: {name!r}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise ValueError(f"unsafe path in zip: {name!r}")
    return "/".join(parts) + ("/" if name.endswith("/") else "")


def is_symlink_entry(external_attr: int) -> bool:
    """Whether a zip entry's ``external_attr`` marks a Unix symbolic link."""
    return stat.S_ISLNK(external_attr >> 16)


# --- SOAP envelopes ---------------------------------------------------------------------------


def _envelope(session_id: str, body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<env:Envelope xmlns:env="{NS_ENVELOPE}" xmlns:m="{NS_METADATA}">'
        f"<env:Header><m:SessionHeader><m:sessionId>{escape(session_id)}</m:sessionId>"
        "</m:SessionHeader></env:Header>"
        f"<env:Body>{body}</env:Body></env:Envelope>"
    ).encode()


def retrieve_envelope(types: PackageTypes, api_version: str, session_id: str) -> bytes:
    """``retrieve`` request for the unpackaged components in ``types`` (``package.xml`` at the zip root)."""
    parts = "".join(
        "<m:types>"
        + "".join(f"<m:members>{escape(m)}</m:members>" for m in members)
        + f"<m:name>{escape(name)}</m:name></m:types>"
        for name, members in types
    )
    v = escape(api_version)
    body = (
        f"<m:retrieve><m:retrieveRequest><m:apiVersion>{v}</m:apiVersion>"
        "<m:singlePackage>true</m:singlePackage>"
        f"<m:unpackaged>{parts}<m:version>{v}</m:version></m:unpackaged>"
        "</m:retrieveRequest></m:retrieve>"
    )
    return _envelope(session_id, body)


def check_retrieve_status_envelope(job_id: str, include_zip: bool, session_id: str) -> bytes:
    """``checkRetrieveStatus`` request. Without ``include_zip`` it only reads the state."""
    body = (
        f"<m:checkRetrieveStatus><m:asyncProcessId>{escape(job_id)}</m:asyncProcessId>"
        f"<m:includeZip>{'true' if include_zip else 'false'}</m:includeZip></m:checkRetrieveStatus>"
    )
    return _envelope(session_id, body)


def list_metadata_envelope(
    metadata_type: str, folder: str | None, api_version: str, session_id: str
) -> bytes:
    """``listMetadata`` request with one ``ListMetadataQuery``."""
    _check_type(metadata_type)
    folder_el = f"<m:folder>{escape(_check_text(folder, 'folder'))}</m:folder>" if folder is not None else ""
    body = (
        f"<m:listMetadata><m:queries>{folder_el}<m:type>{escape(metadata_type)}</m:type></m:queries>"
        f"<m:asOfVersion>{escape(api_version)}</m:asOfVersion></m:listMetadata>"
    )
    return _envelope(session_id, body)


def describe_metadata_envelope(api_version: str, session_id: str) -> bytes:
    """``describeMetadata`` request."""
    body = f"<m:describeMetadata><m:asOfVersion>{escape(api_version)}</m:asOfVersion></m:describeMetadata>"
    return _envelope(session_id, body)


# --- SOAP parsers -----------------------------------------------------------------------------


def _results(content: bytes, response: str) -> list[ET.Element]:
    body = soap_body(parse_xml(content))
    wrapper = next(iter(children(body, response)), None)
    if wrapper is None:
        raise UnexpectedResponse(f"SOAP response has no {response}", raw=content[:_RAW_LIMIT])
    return children(wrapper, "result")


def _one(content: bytes, response: str) -> ET.Element:
    results = _results(content, response)
    if len(results) != 1:
        raise UnexpectedResponse(
            f"expected 1 result in {response}, got {len(results)}", raw=content[:_RAW_LIMIT]
        )
    return results[0]


def _flag(el: ET.Element, name: str) -> bool:
    return child_text(el, name) == "true"


def parse_async_id(content: bytes) -> str:
    """The job id of a ``retrieveResponse``.

    Raises:
        UnexpectedResponse: not a SOAP envelope, or no valid id.
    """
    job_id = child_text(_one(content, "retrieveResponse"), "id")
    if job_id is None or not _ASYNC_ID.fullmatch(job_id):
        raise UnexpectedResponse("retrieve response has no valid job id", raw=content[:_RAW_LIMIT])
    return job_id


def _file_properties(el: ET.Element) -> FileProperties:
    return FileProperties(
        type=child_text(el, "type") or "",
        full_name=child_text(el, "fullName") or "",
        file_name=child_text(el, "fileName"),
        id=child_text(el, "id"),
        namespace_prefix=child_text(el, "namespacePrefix"),
        manageable_state=child_text(el, "manageableState"),
        created_by_name=child_text(el, "createdByName"),
        created_date=child_text(el, "createdDate"),
        last_modified_by_name=child_text(el, "lastModifiedByName"),
        last_modified_date=child_text(el, "lastModifiedDate"),
    )


def parse_retrieve_status(content: bytes) -> dict[str, Any]:
    """Parse a ``checkRetrieveStatusResponse``.

    Returns:
        A dict with ``status``, ``done``, ``error_message``, ``error_status_code``,
        ``zip_file`` (decoded bytes, or ``None``), ``file_properties`` and ``messages``.

    Raises:
        UnexpectedResponse: not a SOAP envelope, an unknown ``status``, or a ``zipFile`` that
            is not base64.
    """
    r = _one(content, "checkRetrieveStatusResponse")
    status = child_text(r, "status") or ""
    if status not in RETRIEVE_STATUSES:
        raise UnexpectedResponse(f"unknown retrieve status {status!r}", raw=content[:_RAW_LIMIT])
    zip_text = child_text(r, "zipFile")
    zip_file: bytes | None = None
    if zip_text is not None:
        try:
            zip_file = base64.b64decode("".join(zip_text.split()), validate=True)
        except (binascii.Error, ValueError):
            raise UnexpectedResponse("retrieve zipFile is not base64", raw=content[:_RAW_LIMIT]) from None
    return {
        "status": status,
        "done": _flag(r, "done"),
        "error_message": child_text(r, "errorMessage"),
        "error_status_code": child_text(r, "errorStatusCode"),
        "zip_file": zip_file,
        "file_properties": tuple(_file_properties(f) for f in children(r, "fileProperties")),
        "messages": tuple(
            RetrieveMessage(file_name=child_text(m, "fileName"), problem=child_text(m, "problem") or "")
            for m in children(r, "messages")
        ),
    }


def parse_list_metadata(content: bytes) -> list[FileProperties]:
    """Parse a ``listMetadataResponse``: one :class:`FileProperties` per component.

    Raises:
        UnexpectedResponse: not a SOAP envelope.
    """
    return [_file_properties(r) for r in _results(content, "listMetadataResponse")]


def _texts(el: ET.Element, name: str) -> tuple[str, ...]:
    return tuple((c.text or "").strip() for c in children(el, name) if (c.text or "").strip())


def parse_describe_metadata(content: bytes) -> MetadataDescribe:
    """Parse a ``describeMetadataResponse``.

    Raises:
        UnexpectedResponse: not a SOAP envelope.
    """
    r = _one(content, "describeMetadataResponse")
    return MetadataDescribe(
        types=tuple(
            MetadataType(
                xml_name=child_text(o, "xmlName") or "",
                directory_name=child_text(o, "directoryName"),
                suffix=child_text(o, "suffix"),
                in_folder=_flag(o, "inFolder"),
                meta_file=_flag(o, "metaFile"),
                child_xml_names=_texts(o, "childXmlNames"),
            )
            for o in children(r, "metadataObjects")
        ),
        organization_namespace=child_text(r, "organizationNamespace"),
        partial_save_allowed=_flag(r, "partialSaveAllowed"),
        test_required=_flag(r, "testRequired"),
    )
