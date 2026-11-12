"""``fake.ui``: the User Interface API of :class:`FakeSalesforce` (§9.13, §14).

The fake builds UI API shapes from its in-memory records and describes, so code that reads
``sf.ui`` works in unit tests without scripting each response:

- ``record`` and ``list_records`` turn stored records into ``{"fields": {"Name": {"value": ...,
  "displayValue": None}}}``. The fake has no formatting rules, so ``displayValue`` is ``None``.
- ``object_info``, ``picklist_values`` and ``create_defaults`` come from the describe set with
  ``fake.set_describe`` (or the minimal one synthesised from the records).
- list views come from ``fake.set_list_view``; ``__Recent`` always exists.
- errors follow what a real org (API 68.0) answers: an unknown object is 403
  ``INSUFFICIENT_ACCESS`` (:class:`~even_simpler_salesforce.Forbidden`), a field of another
  object in ``fields`` 400 ``INVALID_INPUT``, an unknown field 400 ``INVALID_FIELD``.

There is no layout engine: ``layout_types`` returns every field, and ``layout`` in the
defaults is an empty ``Full`` layout.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from ..core.http import check_record_id, check_sobject
from ..core.ui import (
    LayoutType,
    Mode,
    check_list_view,
    clone_defaults_path,
    create_defaults_path,
    list_records_params,
    list_records_path,
    list_views_path,
    object_info_path,
    picklist_values_path,
    record_params,
    record_path,
)
from ..errors import Forbidden, InvalidField, NotFound, RequestError
from ._ids import make_id
from ._state import FakeState, Who, _get_field

__all__ = ["AsyncFakeUi", "FakeUi", "ListView"]

#: The master record type id; every object has it.
MASTER_RECORD_TYPE = "012000000000000AAA"
_RECENT = "__Recent"
_DEFAULT_PAGE = 50
_SYSTEM = frozenset(
    {
        "Id",
        "IsDeleted",
        "CreatedDate",
        "CreatedById",
        "LastModifiedDate",
        "LastModifiedById",
        "SystemModstamp",
        "LastViewedDate",
        "LastReferencedDate",
    }
)
_DATA_TYPES = {
    "id": "String",
    "multipicklist": "MultiPicklist",
    "datetime": "DateTime",
    "textarea": "TextArea",
    "int": "Int",
    "anytype": "AnyType",
    "encryptedstring": "EncryptedString",
}


@dataclass(frozen=True, slots=True)
class ListView:
    """A list view registered with ``fake.set_list_view``."""

    id: str
    api_name: str
    label: str
    where: Callable[[dict[str, Any]], bool] | None
    fields: tuple[str, ...] | None


def add_list_view(
    state: FakeState,
    sobject: str,
    api_name: str,
    label: str | None,
    where: Callable[[dict[str, Any]], bool] | None,
    fields: Sequence[str] | None,
) -> ListView:
    """Register (or replace) a list view of ``sobject``."""
    check_sobject(sobject)
    check_list_view(api_name)
    with state.lock:
        views: dict[str, ListView] = state.list_views.setdefault(sobject, {})
        old = views.get(api_name)
        if old is not None:
            view_id = old.id
        else:
            state.counter += 1
            view_id = make_id("00B", state.counter)
        view = ListView(
            view_id, api_name, label or api_name, where, None if fields is None else tuple(fields)
        )
        views[api_name] = view
        return view


def _not_found(operation: str, sobject: str | None) -> NotFound:
    return NotFound(
        "The requested resource does not exist",
        error_code="NOT_FOUND",
        status=404,
        operation=operation,
        sobject=sobject,
    )


def _no_access(operation: str, sobject: str) -> Forbidden:
    # An unknown object: Salesforce answers 403 INSUFFICIENT_ACCESS, not 404 (V61).
    return Forbidden(
        "You don't have access to this record. Ask your administrator for help or to request access.",
        error_code="INSUFFICIENT_ACCESS",
        status=403,
        operation=operation,
        sobject=sobject,
    )


def _dicts(raw: Any) -> list[dict[str, Any]]:
    items: list[Any] = list(raw) if isinstance(raw, (list, tuple)) else []  # pyright: ignore[reportUnknownArgumentType]
    return [cast("dict[str, Any]", i) for i in items if isinstance(i, dict)]


def _ui_value(value: Any) -> Any:
    if isinstance(value, Mapping) and "attributes" in value:
        nested = cast("Mapping[str, Any]", value)
        attrs: Any = nested["attributes"]
        api_name: Any = cast("Mapping[str, Any]", attrs).get("type") if isinstance(attrs, Mapping) else None
        return {
            "apiName": api_name,
            "id": nested.get("Id"),
            "fields": {
                k: {"value": _ui_value(v), "displayValue": None}
                for k, v in nested.items()
                if k != "attributes"
            },
        }
    return copy.deepcopy(value)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]


def _ui_record(sobject: str, rec: Mapping[str, Any], names: Sequence[str] | None) -> dict[str, Any]:
    keys = [k for k in rec if k != "attributes"] if names is None else list(names)
    return {
        "apiName": sobject,
        "id": rec.get("Id"),
        "childRelationships": {},
        "eTag": None,
        "fields": {k: {"value": _ui_value(_get_field(rec, k)), "displayValue": None} for k in keys},
        "lastModifiedById": rec.get("LastModifiedById"),
        "lastModifiedDate": rec.get("LastModifiedDate"),
        "recordTypeId": rec.get("RecordTypeId") or MASTER_RECORD_TYPE,
        "recordTypeInfo": None,
        "systemModstamp": rec.get("SystemModstamp"),
        "weakEtag": 0,
    }


def _data_type(rest_type: Any) -> str:
    t = str(rest_type or "string").lower()
    return _DATA_TYPES.get(t, t[:1].upper() + t[1:])


def _picklist_entry(v: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "attributes": None,
        "label": v.get("label") or v.get("value"),
        "validFor": [],
        "value": v.get("value"),
    }


class FakeUi:
    """Fake of ``sf.ui``. Same arguments, validation and operation names as the real one."""

    def __init__(self, state: FakeState, who: Who) -> None:
        self._state = state
        self._who = who

    def _run(
        self, operation: str, sobject: str | None, args: Mapping[str, Any], fn: Callable[[], Any], path: str
    ) -> Any:
        return self._state.run(self._who, operation, sobject, args, fn, method="GET", path=path)

    # --- reads ----------------------------------------------------------------------------

    def record(
        self,
        record_id: str,
        fields: Sequence[str] | None = None,
        layout_types: Sequence[LayoutType] | None = None,
        modes: Sequence[Mode] | None = None,
    ) -> dict[str, Any]:
        """The stored record in UI API form; ``fields`` must exist on the object."""
        record_params(fields, layout_types, modes)  # same checks as the real client
        path = record_path(record_id)
        args = {"id": record_id, "fields": fields, "layout_types": layout_types, "modes": modes}
        return self._run("ui.record", None, args, lambda: self._record(record_id, fields), path)

    def _record(self, record_id: str, fields: Sequence[str] | None) -> dict[str, Any]:
        sobject = self._state.sobject_of_id(record_id)
        if sobject is None:
            raise _not_found("ui.record", None)
        rec = self._state.find(sobject, record_id, "ui.record")
        if not fields:  # layout_types: the fake has no layouts, so every stored field
            return _ui_record(sobject, rec, None)
        known = {str(f["name"]).lower() for f in self._fields(sobject)} | {k.lower() for k in rec}
        tops: dict[str, None] = {}
        others = sorted({q.partition(".")[0] for q in fields} - {sobject})
        if others:
            raise RequestError(
                'The "fields" query string parameter contained object api names that do not '
                "correspond to the api names of any of the requested record ids. The requested "
                f"object api names were: [{', '.join(others)}], while the requested records had "
                f"object types: [{sobject}]",
                error_code="INVALID_INPUT",
                status=400,
                operation="ui.record",
                sobject=sobject,
            )
        for qualified in fields:
            obj, _, rest = qualified.partition(".")
            top = rest.split(".", 1)[0]
            if top.lower() not in known:
                raise InvalidField(
                    f"No such column '{rest}' on entity '{obj}'",
                    error_code="INVALID_FIELD",
                    fields=(qualified,),
                    status=400,
                    operation="ui.record",
                    sobject=sobject,
                )
            tops[top] = None
        return _ui_record(sobject, rec, list(tops))

    def object_info(self, name: str, *, cached: bool = True) -> dict[str, Any]:
        """Object metadata built from the describe."""
        path = object_info_path(name)
        return self._run("ui.object_info", name, {"cached": cached}, lambda: self._object_info(name), path)

    def _fields(self, sobject: str) -> list[dict[str, Any]]:
        return [f for f in _dicts(self._state.describe(sobject).get("fields")) if f.get("name")]

    def _object_info(self, sobject: str) -> dict[str, Any]:
        d = self._state.describe(sobject)
        if sobject not in self._state.describes and sobject not in self._state.store:
            raise _no_access("ui.object_info", sobject)
        fields = {
            str(f["name"]): {
                "apiName": f["name"],
                "dataType": _data_type(f.get("type")),
                "label": f.get("label") or f["name"],
                "required": f.get("nillable") is False
                and f.get("createable", True)
                and not f.get("defaultedOnCreate"),
                "updateable": bool(f.get("updateable", True)),
            }
            for f in self._fields(sobject)
        }
        return {
            "apiName": sobject,
            "label": d.get("label", sobject),
            "labelPlural": d.get("labelPlural", d.get("label", sobject)),
            "keyPrefix": d.get("keyPrefix"),
            "custom": bool(d.get("custom")),
            "createable": bool(d.get("createable", True)),
            "updateable": bool(d.get("updateable", True)),
            "deletable": bool(d.get("deletable", True)),
            "queryable": bool(d.get("queryable", True)),
            "defaultRecordTypeId": MASTER_RECORD_TYPE,
            "recordTypeInfos": {
                MASTER_RECORD_TYPE: {
                    "available": True,
                    "defaultRecordTypeMapping": True,
                    "master": True,
                    "name": "Master",
                    "recordTypeId": MASTER_RECORD_TYPE,
                }
            },
            "fields": fields,
            "childRelationships": [],
            "dependentFields": {},
            "eTag": None,
        }

    def picklist_values(self, name: str, record_type_id: str, *, cached: bool = True) -> dict[str, Any]:
        """The active ``picklistValues`` of each picklist field in the describe."""
        path = picklist_values_path(name, record_type_id)
        args = {"record_type_id": record_type_id, "cached": cached}
        return self._run(
            "ui.picklist_values", name, args, lambda: self._picklists(name, record_type_id), path
        )

    def _picklists(self, sobject: str, record_type_id: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        base = f"/services/data/v{self._state.api_version}/{picklist_values_path(sobject, record_type_id)}"
        for f in self._fields(sobject):
            if str(f.get("type")).lower() not in ("picklist", "multipicklist"):
                continue
            values = [v for v in _dicts(f.get("picklistValues")) if v.get("active", True)]
            default = next((v for v in values if v.get("defaultValue")), None)
            out[str(f["name"])] = {
                "controllerValues": {},
                "defaultValue": _picklist_entry(default) if default is not None else None,
                "eTag": None,
                "url": f"{base}/{f['name']}",
                "values": [_picklist_entry(v) for v in values],
            }
        return {"eTag": None, "picklistFieldValues": out}

    def create_defaults(self, name: str, record_type_id: str | None = None) -> dict[str, Any]:
        """Default values (each describe field's ``defaultValue``) for a new record."""
        if record_type_id is not None:
            check_record_id(record_type_id)
        path = create_defaults_path(name)
        args = {"record_type_id": record_type_id}
        return self._run("ui.create_defaults", name, args, lambda: self._create(name, record_type_id), path)

    def _defaults(
        self, sobject: str, fields: Mapping[str, Any], record_type_id: str | None
    ) -> dict[str, Any]:
        record = _ui_record(sobject, {"Id": None, **fields}, list(fields))
        record["recordTypeId"] = record_type_id or MASTER_RECORD_TYPE
        return {
            "layout": {"layoutType": "Full", "mode": "Create", "objectApiName": sobject, "sections": []},
            "objectInfos": {sobject: self._object_info(sobject)},
            "record": record,
        }

    def _create(self, sobject: str, record_type_id: str | None) -> dict[str, Any]:
        fields = {
            str(f["name"]): f["defaultValue"]
            for f in self._fields(sobject)
            if f.get("defaultValue") is not None
        }
        return self._defaults(sobject, fields, record_type_id)

    def clone_defaults(self, record_id: str) -> dict[str, Any]:
        """The stored record's values without its id and system fields."""
        path = clone_defaults_path(record_id)
        return self._run("ui.clone_defaults", None, {"id": record_id}, lambda: self._clone(record_id), path)

    def _clone(self, record_id: str) -> dict[str, Any]:
        sobject = self._state.sobject_of_id(record_id)
        if sobject is None:
            raise _not_found("ui.clone_defaults", None)
        rec = self._state.find(sobject, record_id, "ui.clone_defaults")
        fields = {k: v for k, v in rec.items() if k != "attributes" and k not in _SYSTEM}
        rt: Any = rec.get("RecordTypeId")
        return self._defaults(sobject, fields, rt if isinstance(rt, str) else None)

    def list_views(self, name: str) -> dict[str, Any]:
        """The list views set with ``fake.set_list_view``."""
        path = list_views_path(name)
        return self._run("ui.list_views", name, {}, lambda: self._list_views(name), path)

    def _known(self, sobject: str) -> bool:
        st = self._state
        return sobject in st.describes or sobject in st.store or sobject in st.list_views

    def _list_views(self, sobject: str) -> dict[str, Any]:
        if not self._known(sobject):
            raise _no_access("ui.list_views", sobject)
        views: dict[str, ListView] = self._state.list_views.get(sobject, {})
        base = f"/services/data/v{self._state.api_version}/{list_views_path(sobject)}"
        lists = [{"apiName": _RECENT, "id": None, "label": "Recently Viewed", "url": f"{base}/{_RECENT}"}]
        lists += [
            {"apiName": v.api_name, "id": v.id, "label": v.label, "url": f"{base}/{v.api_name}"}
            for v in views.values()
            if v.api_name != _RECENT
        ]
        size = max(20, len(lists))
        return {
            "count": len(lists),
            "currentPageToken": "0",
            "currentPageUrl": f"{base}?pageSize={size}&pageToken=0",
            "eTag": None,
            "lists": lists,
            "nextPageToken": None,
            "nextPageUrl": None,
            "objectApiName": sobject,
            "pageSize": size,
            "previousPageToken": None,
            "previousPageUrl": None,
            "queryString": None,
            "recentListsOnly": False,
        }

    def list_records(
        self,
        name: str,
        list_view: str,
        *,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """One page of the records a list view selects (``__Recent``: every record of the object)."""
        list_records_params(page_size, page_token)
        path = list_records_path(name, list_view)
        args = {"list_view": list_view, "page_size": page_size, "page_token": page_token}
        return self._run(
            "ui.list_records",
            name,
            args,
            lambda: self._list_records(name, list_view, page_size, page_token),
            path,
        )

    def _list_records(
        self, sobject: str, list_view: str, page_size: int | None, page_token: str | None
    ) -> dict[str, Any]:
        rows = list(self._state.store.get(sobject, {}).values())
        fields: Sequence[str] | None = None
        view: ListView | None = None
        if list_view != _RECENT:
            view = self._state.list_views.get(sobject, {}).get(list_view)
            if view is None:
                raise _not_found("ui.list_records", sobject)
            if view.where is not None:
                rows = [r for r in rows if view.where(copy.deepcopy(r))]
            fields = view.fields
        size = page_size or _DEFAULT_PAGE
        try:
            start = int(page_token) if page_token is not None else 0
        except ValueError:
            start = -1
        if start < 0:
            raise RequestError(
                f'For input string: "{page_token}"',
                error_code="ILLEGAL_QUERY_PARAMETER_VALUE",
                status=400,
                operation="ui.list_records",
                sobject=sobject,
            )
        page = rows[start : start + size]
        end = start + len(page)
        base = f"/services/data/v{self._state.api_version}/{list_records_path(sobject, list_view)}"

        def url(token: str | None) -> str | None:
            return None if token is None else f"{base}?pageSize={size}&pageToken={token}"

        next_token = str(end) if end < len(rows) else None
        previous = str(max(0, start - size)) if start > 0 else None
        return {
            "count": len(page),
            "currentPageToken": str(start),
            "currentPageUrl": url(str(start)),
            "fields": [],
            "listInfoETag": None,
            "listReference": {
                "id": None if view is None else view.id,
                "listViewApiName": list_view,
                "objectApiName": sobject,
                "type": "listView",
            },
            "nextPageToken": next_token,
            "nextPageUrl": url(next_token),
            "optionalFields": [],
            "pageSize": size,
            "previousPageToken": previous,
            "previousPageUrl": url(previous),
            "records": [_ui_record(sobject, r, fields) for r in page],
            "searchTerm": None,
            "sortBy": None,
            "where": None,
        }


class AsyncFakeUi:
    """Async twin of :class:`FakeUi`."""

    def __init__(self, inner: FakeUi) -> None:
        self._inner = inner

    async def record(
        self,
        record_id: str,
        fields: Sequence[str] | None = None,
        layout_types: Sequence[LayoutType] | None = None,
        modes: Sequence[Mode] | None = None,
    ) -> dict[str, Any]:
        """See :meth:`FakeUi.record`."""
        return self._inner.record(record_id, fields, layout_types, modes)

    async def object_info(self, name: str, *, cached: bool = True) -> dict[str, Any]:
        """See :meth:`FakeUi.object_info`."""
        return self._inner.object_info(name, cached=cached)

    async def picklist_values(self, name: str, record_type_id: str, *, cached: bool = True) -> dict[str, Any]:
        """See :meth:`FakeUi.picklist_values`."""
        return self._inner.picklist_values(name, record_type_id, cached=cached)

    async def create_defaults(self, name: str, record_type_id: str | None = None) -> dict[str, Any]:
        """See :meth:`FakeUi.create_defaults`."""
        return self._inner.create_defaults(name, record_type_id)

    async def clone_defaults(self, record_id: str) -> dict[str, Any]:
        """See :meth:`FakeUi.clone_defaults`."""
        return self._inner.clone_defaults(record_id)

    async def list_views(self, name: str) -> dict[str, Any]:
        """See :meth:`FakeUi.list_views`."""
        return self._inner.list_views(name)

    async def list_records(
        self,
        name: str,
        list_view: str,
        *,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """See :meth:`FakeUi.list_records`."""
        return self._inner.list_records(name, list_view, page_size=page_size, page_token=page_token)
