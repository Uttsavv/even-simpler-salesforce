"""``core/ui.py``: UI API paths, parameters and response checks (§9.13).

Parameter names and values follow the *User Interface API Developer Guide*, *Get a Record*
(https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_record_get.htm)
and *Get List View Records*
(https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_list_views_records.htm).
"""

from __future__ import annotations

from typing import Any

import pytest

from even_simpler_salesforce.core import classify as C
from even_simpler_salesforce.core import ui
from even_simpler_salesforce.core.http import RawResponse, RequestSpec
from even_simpler_salesforce.errors import InvalidType, NotFound, RequestError, UnexpectedResponse

ID = "00Qxx0000000001AAA"
RT = "012000000000000AAA"


def test_paths() -> None:
    assert ui.record_path(ID) == f"ui-api/records/{ID}"
    assert ui.object_info_path("Lead") == "ui-api/object-info/Lead"
    assert ui.picklist_values_path("Lead", RT) == f"ui-api/object-info/Lead/picklist-values/{RT}"
    assert ui.create_defaults_path("Lead") == "ui-api/record-defaults/create/Lead"
    assert ui.clone_defaults_path(ID) == f"ui-api/record-defaults/clone/{ID}"
    assert ui.list_views_path("Lead") == "ui-api/list-info/Lead"
    assert ui.list_records_path("Lead", "__Recent") == "ui-api/list-records/Lead/__Recent"


@pytest.mark.parametrize(
    "call",
    [
        lambda: ui.record_path("x/y"),
        lambda: ui.object_info_path("Lead/describe"),
        lambda: ui.picklist_values_path("Lead", "Master"),
        lambda: ui.clone_defaults_path(""),
        lambda: ui.list_records_path("Lead", "a/b"),
        lambda: ui.check_list_view("1Leads"),
        lambda: ui.check_list_view(""),
        lambda: ui.check_list_view(3),  # type: ignore[arg-type]
    ],
)
def test_path_values_are_validated(call: Any) -> None:
    with pytest.raises(ValueError):
        call()


def test_check_list_view() -> None:
    assert ui.check_list_view("AllOpenLeads") == "AllOpenLeads"
    assert ui.check_list_view("__Recent") == "__Recent"


def test_record_params() -> None:
    assert ui.record_params(["Lead.Name", "Lead.Owner.Name"], None, None) == [
        ("fields", "Lead.Name,Lead.Owner.Name")
    ]
    assert ui.record_params(None, ["Compact"], ["Edit", "View"]) == [
        ("layoutTypes", "Compact"),
        ("modes", "Edit,View"),
    ]
    assert ui.record_params(("Lead.Name",), (), ()) == [("fields", "Lead.Name")]


def test_record_params_refuse_fields_with_layout() -> None:
    """Salesforce answers 400 INVALID_INPUT_COMBINATION to both (org-observed, V63)."""
    with pytest.raises(ValueError, match="not both"):
        ui.record_params(("Lead.Name",), ("Full",), ())


def test_record_params_need_fields_or_layout() -> None:
    with pytest.raises(ValueError, match="fields or layout_types"):
        ui.record_params(None, None, ["View"])
    with pytest.raises(ValueError, match="fields or layout_types"):
        ui.record_params([], [], None)


def test_record_params_reject_bad_values() -> None:
    with pytest.raises(ValueError, match="'Name'"):
        ui.record_params(["Name"], None, None)
    with pytest.raises(ValueError, match="layout_types"):
        ui.record_params(None, ["Tall"], None)  # type: ignore[list-item]
    with pytest.raises(ValueError, match="modes"):
        ui.record_params(None, ["Full"], ["Read"])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="fields"):
        ui.record_params("Lead.Name", None, None)
    with pytest.raises(TypeError, match="layout_types"):
        ui.record_params(None, "Full", None)  # type: ignore[arg-type]


def test_list_records_params() -> None:
    assert ui.list_records_params(None, None) == []
    assert ui.list_records_params(1, "50") == [("pageSize", "1"), ("pageToken", "50")]
    assert ui.list_records_params(2000, None) == [("pageSize", "2000")]
    for bad in (0, 2001, True):
        with pytest.raises(ValueError, match="page_size"):
            ui.list_records_params(bad, None)


def test_ui_body() -> None:
    assert ui.ui_body({"a": 1}, "ui.record") == {"a": 1}
    with pytest.raises(UnexpectedResponse) as ei:
        ui.ui_body([], "ui.record")
    assert ei.value.operation == "ui.record"


def _resp(status: int, body: bytes) -> RawResponse:
    return RawResponse(status=status, headers={"content-type": "application/json"}, content=body)


def test_classify_ui_errors() -> None:
    """UI API errors carry ``errorCode`` and ``message`` like REST (*Status Codes and Error Responses*,
    https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_errors.htm).
    """
    bad_id = (
        b'[{"errorCode":"ILLEGAL_QUERY_PARAMETER_VALUE","message":"recordId should be a valid record ID"}]'
    )
    err = C.classify(_resp(400, bad_id), RequestSpec("GET", f"ui-api/records/{ID}", operation="ui.record"))
    assert type(err) is RequestError
    assert err.error_code == "ILLEGAL_QUERY_PARAMETER_VALUE"
    missing = b'[{"errorCode":"NOT_FOUND","message":"The requested resource does not exist"}]'
    on_type = C.classify(
        _resp(404, missing), RequestSpec("GET", "ui-api/object-info/X", operation="ui.object_info")
    )
    assert type(on_type) is InvalidType
    on_record = C.classify(
        _resp(404, missing), RequestSpec("GET", f"ui-api/records/{ID}", operation="ui.record")
    )
    assert type(on_record) is NotFound
