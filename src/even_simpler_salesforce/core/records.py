"""Builders and parsers for records, query and collections (§9.1-9.3). No I/O."""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, TypeVar, cast

from ..config import DuplicateRules
from ..errors import ErrorDetail, InvalidField, MultipleMatches, UnexpectedResponse
from ..models import QueryPlan, QueryPlanNote, RecordResult, UpsertResult
from .classify import parse_details
from .http import check_record_id
from .soql import quote, soql_identifier

__all__ = [
    "COLLECTION_READ_CHUNK",
    "COLLECTION_WRITE_CHUNK",
    "apply_model",
    "chunked",
    "duplicate_matches",
    "duplicate_rule_header",
    "duplicate_rules_arg",
    "external_id_in_data",
    "external_id_lookup_soql",
    "flatten",
    "format_sf_datetime",
    "needs_external_id_fallback",
    "parse_query_plans",
    "parse_record_results",
    "parse_upsert",
    "query_ids",
    "with_type",
    "write_headers",
]

COLLECTION_WRITE_CHUNK = 200
COLLECTION_READ_CHUNK = 2000

# Salesforce still reads these endings of a REST path as a response format, even on API
# versions where format suffixes are no longer documented (58.0+). An external id value that
# ends in one of them can't be used in ``sobjects/{o}/{field}/{value}``: the lookup returns 404
# NOT_FOUND, also with the dot percent-encoded. Observed on a v68.0 org for ``.json``, ``.xml``
# and ``.html`` (lower case; other endings, ``.JSON`` included, work). Matched case-insensitively
# to be safe: the fallback returns the same result, only with one more request.
_FORMAT_SUFFIXES = (".json", ".xml", ".html")
_ID_IN_MESSAGE = re.compile(r"\b[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?\b")

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


def needs_external_id_fallback(value: str) -> bool:
    """Whether ``value`` can't go in an external id path (it ends like a format suffix)."""
    return value.lower().endswith(_FORMAT_SUFFIXES)


def external_id_lookup_soql(sobject: str, field: str, value: str) -> str:
    """SOQL that finds the ids of the records whose external id ``field`` equals ``value``."""
    # Both names are validated identifiers and the value is quoted, so joining them is safe.
    parts = ("SELECT Id FROM", soql_identifier(sobject), "WHERE", soql_identifier(field), "=", quote(value))
    return " ".join(parts)


def query_ids(body: Any) -> list[str]:
    """The ``Id`` of every record in a query response body."""
    records: Any = body.get("records") if isinstance(body, Mapping) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(records, list):
        raise UnexpectedResponse("query response has no records list", raw=repr(body)[:4096])  # pyright: ignore[reportUnknownArgumentType]
    rows = cast("list[Any]", records)
    return [str(r["Id"]) for r in rows if isinstance(r, Mapping) and r.get("Id")]  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


def external_id_in_data(field: str, data: Mapping[str, Any]) -> InvalidField | None:
    """The error Salesforce returns when an upsert body also sets its external id field.

    ``PATCH sobjects/{o}/{field}/{value}`` rejects that with ``INVALID_FIELD``; the Collections
    upsert, which the fallback uses, takes the field from the body instead. Field names are
    case-insensitive, as in Salesforce.
    """
    if not any(str(k).lower() == field.lower() for k in data):
        return None
    return InvalidField(
        f"The {field} field should not be specified in the sobject data.",
        error_code="INVALID_FIELD",
        status=400,
    )


def duplicate_matches(detail: ErrorDetail, api_version: str, sobject: str) -> MultipleMatches | None:
    """:class:`MultipleMatches` for a Collections ``DUPLICATE_EXTERNAL_ID`` error, else ``None``.

    The single-record upsert answers HTTP 300 with the record URLs; the Collections upsert
    reports the same case as ``DUPLICATE_EXTERNAL_ID`` with the ids in the message. The URLs
    are rebuilt from those ids so both paths give the same ``matches``.
    """
    if detail.error_code != "DUPLICATE_EXTERNAL_ID":
        return None
    ids = _ID_IN_MESSAGE.findall(detail.message.rsplit(":", 1)[-1])
    matches = tuple(f"/services/data/v{api_version}/sobjects/{sobject}/{i}" for i in ids)
    return MultipleMatches(
        f"external id matched {len(matches) or 'several'} records", matches=matches, status=300
    )


def duplicate_rules_arg(
    duplicate_rules: DuplicateRules | None, allow_save: bool | None, *, stacklevel: int = 3
) -> DuplicateRules | None:
    """The :class:`DuplicateRules` of a write, from ``duplicate_rules=`` or the deprecated ``allow_save=``.

    ``allow_save=`` emits :class:`EsfDeprecationWarning`; ``stacklevel`` is counted from this
    function (the default points at the caller of the method that calls it).

    Raises:
        TypeError: Both are passed.
    """
    if allow_save is None:
        return duplicate_rules
    if duplicate_rules is not None:
        raise TypeError("pass duplicate_rules= or allow_save=, not both (allow_save= is deprecated)")
    import warnings

    from .. import EsfDeprecationWarning

    flag = bool(allow_save)
    warnings.warn(
        f"allow_save= is deprecated and will be removed in 0.7: use "
        f"duplicate_rules=esf.DuplicateRules(allow_save={flag})",
        EsfDeprecationWarning,
        stacklevel=stacklevel,
    )
    return DuplicateRules(allow_save=flag)


def write_headers(
    assignment_rule_id: str | bool | None, duplicate_rules: DuplicateRules | None
) -> dict[str, str]:
    """``Sforce-Auto-Assign`` and ``Sforce-Duplicate-Rule-Header`` for a write (``None``: not sent)."""
    h: dict[str, str] = {}
    if assignment_rule_id is not None:
        if isinstance(assignment_rule_id, bool):
            h["Sforce-Auto-Assign"] = "TRUE" if assignment_rule_id else "FALSE"
        else:
            h["Sforce-Auto-Assign"] = check_record_id(assignment_rule_id)
    if duplicate_rules is not None:
        h["Sforce-Duplicate-Rule-Header"] = duplicate_rule_header(
            allow_save=duplicate_rules.allow_save,
            include_record_details=duplicate_rules.include_record_details,
            run_as_current_user=duplicate_rules.run_as_current_user,
        )
    return h


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

    It takes one row. For a page or a list of rows, write ``[esf.flatten(r) for r in page]``.

    Raises:
        TypeError: ``row`` is not a plain mapping: for example a list of rows, a
            ``QueryResult`` or a model instance from ``model=``.
    """
    if not isinstance(row, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
        kind = type(row).__name__  # pyright: ignore[reportUnreachable]
        if isinstance(row, Iterable) and not isinstance(row, (str, bytes, bytearray)):
            raise TypeError(
                f"flatten() takes one row, not a {kind}; for many rows use [esf.flatten(r) for r in page]"
            )
        raise TypeError(f"flatten() takes a plain dict row, not {kind}")
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
