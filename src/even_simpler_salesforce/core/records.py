"""Builders and parsers for records, query and collections (§9.1-9.3). No I/O."""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, TypeVar, cast

from ..errors import ErrorDetail, UnexpectedResponse
from ..models import QueryPlan, QueryPlanNote, RecordResult, UpsertResult
from .classify import parse_details

__all__ = [
    "COLLECTION_READ_CHUNK",
    "COLLECTION_WRITE_CHUNK",
    "apply_model",
    "chunked",
    "duplicate_rule_header",
    "flatten",
    "format_sf_datetime",
    "parse_query_plans",
    "parse_record_results",
    "parse_upsert",
    "with_type",
]

COLLECTION_WRITE_CHUNK = 200
COLLECTION_READ_CHUNK = 2000

T = TypeVar("T")


def chunked(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def with_type(row: Mapping[str, Any], sobject: str | None) -> dict[str, Any]:
    """Copy ``row`` and make sure it carries ``attributes.type``."""
    out = dict(row)
    attrs_raw = out.get("attributes")
    attrs: dict[str, Any] = dict(attrs_raw) if isinstance(attrs_raw, Mapping) else {}  # pyright: ignore[reportUnknownArgumentType]
    if sobject is not None:
        attrs.setdefault("type", sobject)
    if not attrs.get("type"):
        raise ValueError("each row needs attributes.type, or pass sobject=")
    out["attributes"] = attrs
    return out


def parse_record_results(body: Any, expected: int) -> list[RecordResult]:
    """Parse an sObject Collections response into ordered :class:`RecordResult` objects."""
    if not isinstance(body, list) or len(body) != expected:  # pyright: ignore[reportUnknownArgumentType]
        raise UnexpectedResponse(
            f"expected a list of {expected} results from sObject Collections",
            raw=repr(body)[:4096],  # pyright: ignore[reportUnknownArgumentType]
        )
    out: list[RecordResult] = []
    for item in body:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(item, Mapping):  # pyright: ignore[reportUnknownArgumentType]
            raise UnexpectedResponse("malformed sObject Collections result", raw=repr(item)[:4096])  # pyright: ignore[reportUnknownArgumentType]
        errs: tuple[ErrorDetail, ...] = parse_details(item.get("errors") or [])  # pyright: ignore[reportUnknownMemberType]
        created: Any = item.get("created")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        out.append(
            RecordResult(
                ok=bool(item.get("success")),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                id=item.get("id"),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                created=bool(created) if created is not None else None,  # pyright: ignore[reportUnknownArgumentType]
                errors=errs,
            )
        )
    return out


def parse_upsert(status: int, body: Any) -> UpsertResult:
    """Parse the response of ``PATCH sobjects/{o}/{field}/{value}``."""
    rid: Any = body.get("id") if isinstance(body, Mapping) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    has_flag = isinstance(body, Mapping) and "created" in body
    created = bool(body["created"]) if has_flag else status == 201  # pyright: ignore[reportUnknownArgumentType]
    return UpsertResult(id=rid, created=created)  # pyright: ignore[reportUnknownArgumentType]


def duplicate_rule_header(
    allow_save: bool = False,
    include_record_details: bool = False,
    run_as_current_user: bool = False,
) -> str:
    """Value for the ``Sforce-Duplicate-Rule-Header`` header."""

    def b(v: bool) -> str:
        return "true" if v else "false"

    return (
        f"allowSave={b(allow_save)}; includeRecordDetails={b(include_record_details)}; "
        f"runAsCurrentUser={b(run_as_current_user)}"
    )


def format_sf_datetime(value: _dt.datetime) -> str:
    """ISO 8601 in UTC, as Salesforce expects for ``updated``/``deleted`` ranges."""
    if value.tzinfo is None:
        raise ValueError("naive datetime is ambiguous; attach a timezone (e.g. UTC)")
    return value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_query_plans(body: Any) -> list[QueryPlan]:
    """Parse ``GET query?explain=`` (``{"plans": [...], "sourceQuery": ...}``), keeping order."""
    data: Mapping[str, Any] = cast("Mapping[str, Any]", body) if isinstance(body, Mapping) else {}
    plans: Any = data.get("plans")
    if not isinstance(plans, list):
        raise UnexpectedResponse("explain response has no 'plans' list", raw=repr(cast(object, body))[:4096])
    out: list[QueryPlan] = []
    for raw_plan in cast("list[Any]", plans):
        if not isinstance(raw_plan, Mapping):
            raise UnexpectedResponse("malformed explain plan", raw=repr(raw_plan)[:4096])
        p = cast("Mapping[str, Any]", raw_plan)
        notes: list[QueryPlanNote] = []
        for raw_note in cast("list[Any]", p.get("notes") or []):
            if isinstance(raw_note, Mapping):
                n = cast("Mapping[str, Any]", raw_note)
                table = n.get("tableEnumOrId")
                notes.append(
                    QueryPlanNote(
                        description=str(n.get("description") or ""),
                        fields=tuple(str(f) for f in cast("list[Any]", n.get("fields") or [])),
                        table_enum_or_id=str(table) if table is not None else None,
                    )
                )
        out.append(
            QueryPlan(
                leading_operation=str(p.get("leadingOperationType") or ""),
                relative_cost=float(p.get("relativeCost") or 0.0),
                cardinality=int(p.get("cardinality") or 0),
                sobject_cardinality=int(p.get("sobjectCardinality") or 0),
                sobject_type=str(p.get("sobjectType") or ""),
                fields=tuple(str(f) for f in cast("list[Any]", p.get("fields") or [])),
                notes=tuple(notes),
            )
        )
    return out


def flatten(row: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten one query row for CSV, DataFrames or JSON (§9.2).

    - ``attributes`` is dropped at every level.
    - Parent lookups become dotted keys: ``{"Owner": {"Name": "Ann"}}`` → ``{"Owner.Name": "Ann"}``.
    - A null lookup stays one key: ``"Owner": None`` (read with ``.get("Owner.Name")``).
    - A child subquery becomes a list of flattened child rows (only the rows present; use
      ``query_iter(..., expand_children=True)`` to fetch all of them).

    Raises:
        TypeError: ``row`` is not a plain mapping (e.g. a model instance from ``model=``).
    """
    if not isinstance(row, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f"flatten() takes a plain dict row, not {type(row).__name__}")
    out: dict[str, Any] = {}
    _flatten_into(row, "", out)
    return out


def _flatten_into(row: Mapping[str, Any], prefix: str, out: dict[str, Any]) -> None:
    for key, value in row.items():
        if key == "attributes":
            continue
        name = prefix + str(key)
        if isinstance(value, Mapping):
            nested = cast("Mapping[str, Any]", value)
            children = nested.get("records")
            if isinstance(children, list) and ("totalSize" in nested or "done" in nested):
                out[name] = [flatten(c) for c in cast("list[Mapping[str, Any]]", children)]
            else:
                _flatten_into(nested, name + ".", out)
        else:
            out[name] = value


def apply_model(rows: Iterable[Any], model: Any) -> list[Any]:
    """Convert rows with ``model`` (a Pydantic model, a dataclass or any callable)."""
    if model is None:
        return list(rows)
    validate = getattr(model, "model_validate", None)
    if callable(validate):
        return [validate(r) for r in rows]
    return [model(**{k: v for k, v in r.items() if k != "attributes"}) for r in rows]
