"""User Interface API (§9.13): paths, query parameters and response checks. No I/O.

Salesforce facts used here, from the *User Interface API Developer Guide*
(developer.salesforce.com refuses automated fetches, so they were read from search excerpts
on 2026-11-11; open points are V60 to V64 in big-bang.md §21):

- *Get a Record* (``GET ui-api/records/{recordId}``,
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_record_get.htm):
  ``fields``, ``optionalFields``, ``layoutTypes`` (``Compact``, ``Full``) and ``modes``
  (``Create``, ``Edit``, ``View``) are comma-separated lists. From API version 45.0 the
  request needs ``fields``, ``optionalFields`` or ``layoutTypes``. Field names are
  ``ObjectApiName.FieldName``. Org-observed (API 68.0): ``fields`` with ``layoutTypes`` is
  refused unless ``childRelationships`` is also given.
- *Get Object Metadata* (``GET ui-api/object-info/{objectApiName}``).
- *Get Values for All Picklist Fields of a Record Type*
  (``GET ui-api/object-info/{objectApiName}/picklist-values/{recordTypeId}``, API 42.0,
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_picklist_values_collection.htm).
- *Get Default Values to Create a Record* (``GET ui-api/record-defaults/create/{objectApiName}``,
  optional ``recordTypeId``,
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_record_defaults_create.htm)
  and *to Clone a Record* (``GET ui-api/record-defaults/clone/{recordId}``,
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_record_defaults_clone.htm).
- *Get List Views for an Object* (``GET ui-api/list-info/{objectApiName}``,
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_lists_object_get.htm).
- *Get List View Records* (``GET ui-api/list-records/{objectApiName}/{listViewApiName}``,
  ``pageSize`` 1 to 2,000, default 50; ``pageToken`` from ``nextPageToken``;
  ``__Recent`` names the recently viewed list,
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_list_views_records.htm).
- *Status Codes and Error Responses*
  (https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_errors.htm):
  error bodies carry ``errorCode`` and ``message``, the same shape as other REST resources,
  so :func:`~even_simpler_salesforce.core.classify.classify` maps them unchanged.
  Org-observed (API 68.0): an unknown object on ``object-info`` and ``list-info`` is 403
  ``INSUFFICIENT_ACCESS``, an object the UI API doesn't support is 400 ``INVALID_TYPE``, and a
  record id with a wrong case checksum is 400 ``UNKNOWN_EXCEPTION`` ("Record ID is
  malformed").
- ``object-info`` and ``picklist-values`` send an ``ETag`` but no ``Last-Modified`` header,
  and ignore ``If-Modified-Since`` (always 200); ``If-None-Match`` with the ``ETag`` gets 304
  (org-observed, API 68.0).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Literal, cast

from ..errors import UnexpectedResponse
from .http import check_record_id, check_sobject, url_segment

__all__ = [
    "LAYOUT_TYPES",
    "LIST_PAGE_SIZE_MAX",
    "MODES",
    "LayoutType",
    "Mode",
    "check_list_view",
    "clone_defaults_path",
    "create_defaults_path",
    "list_records_params",
    "list_records_path",
    "list_views_path",
    "object_info_path",
    "picklist_values_path",
    "record_params",
    "record_path",
    "ui_body",
]

LayoutType = Literal["Compact", "Full"]
Mode = Literal["Create", "Edit", "View"]
LAYOUT_TYPES: frozenset[str] = frozenset({"Compact", "Full"})
MODES: frozenset[str] = frozenset({"Create", "Edit", "View"})
#: Largest ``pageSize`` that ``list-records`` accepts.
LIST_PAGE_SIZE_MAX = 2000

_PREFIX = "ui-api"
# A list view API name, or a special name such as ``__Recent``.
_LIST_VIEW = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
# ``Object.Field`` or ``Object.Relationship.Field``.
_QUALIFIED_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")


def _names(value: Sequence[str], allowed: frozenset[str], what: str) -> list[str]:
    if isinstance(value, str):
        raise TypeError(f"{what} must be a list of names, not a string")
    out = list(value)
    bad = [v for v in out if v not in allowed]
    if bad:
        raise ValueError(f"{what} must be from {sorted(allowed)}, got {bad[0]!r}")
    return out


def _fields(value: Sequence[str]) -> list[str]:
    if isinstance(value, str):
        raise TypeError("fields must be a list of field names, not a string")
    out = list(value)
    for f in out:
        if not _QUALIFIED_FIELD.fullmatch(f):
            raise ValueError(
                f"UI API field names are qualified with the object, like 'Account.Name'; got {f!r}"
            )
    return out


def record_path(record_id: str) -> str:
    """``ui-api/records/{id}``."""
    return f"{_PREFIX}/records/{check_record_id(record_id)}"


def record_params(
    fields: Sequence[str] | None,
    layout_types: Sequence[str] | None,
    modes: Sequence[str] | None,
) -> list[tuple[str, str]]:
    """Query parameters for ``GET ui-api/records/{id}``.

    Salesforce needs ``fields`` or ``layoutTypes``. It refuses both in one request with HTTP
    400 ``INVALID_INPUT_COMBINATION`` ("May not provide both 'fields' and 'layoutTypes'")
    unless ``childRelationships`` is also given, which this client doesn't send, so the
    combination is refused here. The docs don't say this; it was seen in a Developer Edition
    org on API 68.0 (big-bang.md §21 V63).

    Raises:
        ValueError: neither or both of ``fields`` and ``layout_types`` given, an unqualified
            field name, or an unknown layout type or mode.
        TypeError: a single string instead of a list.
    """
    params: list[tuple[str, str]] = []
    if fields is not None:
        names = _fields(fields)
        if names:
            params.append(("fields", ",".join(names)))
    if layout_types is not None:
        layouts = _names(layout_types, LAYOUT_TYPES, "layout_types")
        if layouts:
            params.append(("layoutTypes", ",".join(layouts)))
    if not params:
        raise ValueError("pass fields or layout_types: Salesforce needs one of them")
    if len(params) == 2:
        raise ValueError("pass fields or layout_types, not both: Salesforce refuses the combination")
    if modes is not None:
        chosen = _names(modes, MODES, "modes")
        if chosen:
            params.append(("modes", ",".join(chosen)))
    return params


def object_info_path(sobject: str) -> str:
    """``ui-api/object-info/{object}``."""
    return f"{_PREFIX}/object-info/{check_sobject(sobject)}"


def picklist_values_path(sobject: str, record_type_id: str) -> str:
    """``ui-api/object-info/{object}/picklist-values/{recordTypeId}``."""
    return f"{object_info_path(sobject)}/picklist-values/{check_record_id(record_type_id)}"


def create_defaults_path(sobject: str) -> str:
    """``ui-api/record-defaults/create/{object}``."""
    return f"{_PREFIX}/record-defaults/create/{check_sobject(sobject)}"


def clone_defaults_path(record_id: str) -> str:
    """``ui-api/record-defaults/clone/{id}``."""
    return f"{_PREFIX}/record-defaults/clone/{check_record_id(record_id)}"


def list_views_path(sobject: str) -> str:
    """``ui-api/list-info/{object}``."""
    return f"{_PREFIX}/list-info/{check_sobject(sobject)}"


def check_list_view(name: str) -> str:
    """Validate a list view API name (``AllOpenLeads``, ``__Recent``) before putting it in a path."""
    if not isinstance(name, str) or not _LIST_VIEW.fullmatch(name):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(f"invalid list view API name: {name!r}")
    return name


def list_records_path(sobject: str, list_view: str) -> str:
    """``ui-api/list-records/{object}/{listViewApiName}``."""
    return f"{_PREFIX}/list-records/{check_sobject(sobject)}/{url_segment(check_list_view(list_view))}"


def list_records_params(page_size: int | None, page_token: str | None) -> list[tuple[str, str]]:
    """Query parameters for ``GET ui-api/list-records/...``.

    Raises:
        ValueError: ``page_size`` outside 1 to 2,000.
    """
    params: list[tuple[str, str]] = []
    if page_size is not None:
        if isinstance(page_size, bool) or not 1 <= page_size <= LIST_PAGE_SIZE_MAX:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError(f"page_size must be between 1 and {LIST_PAGE_SIZE_MAX}, got {page_size!r}")
        params.append(("pageSize", str(page_size)))
    if page_token is not None:
        params.append(("pageToken", str(page_token)))
    return params


def ui_body(body: Any, operation: str) -> dict[str, Any]:
    """The JSON object of a UI API response.

    Raises:
        UnexpectedResponse: the body is not a JSON object.
    """
    if not isinstance(body, dict):
        raise UnexpectedResponse(f"unexpected UI API response for {operation}", operation=operation)
    return cast("dict[str, Any]", body)
