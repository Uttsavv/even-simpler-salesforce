"""Builders and parsers for records, query and collections (§9.1–9.3). No I/O."""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, TypeVar

from ..errors import ErrorDetail, UnexpectedResponse
from ..models import RecordResult, UpsertResult
from .classify import parse_details

__all__ = [
    "COLLECTION_READ_CHUNK",
    "COLLECTION_WRITE_CHUNK",
    "apply_model",
    "chunked",
    "duplicate_rule_header",
    "format_sf_datetime",
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
        if not isinstance(item, Mapping):
            raise UnexpectedResponse("malformed sObject Collections result", raw=repr(item)[:4096])  # pyright: ignore[reportUnknownArgumentType]
        errs: tuple[ErrorDetail, ...] = parse_details(item.get("errors") or [])  # pyright: ignore[reportUnknownMemberType]
        created = item.get("created")  # pyright: ignore[reportUnknownMemberType]
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
    rid = body.get("id") if isinstance(body, Mapping) else None  # pyright: ignore[reportUnknownMemberType]
    if isinstance(body, Mapping) and "created" in body:
        created = bool(body["created"])  # pyright: ignore[reportUnknownArgumentType]
    else:
        created = status == 201
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


def apply_model(rows: Iterable[Any], model: Any) -> list[Any]:
    """Convert rows with ``model`` (a Pydantic model, a dataclass or any callable)."""
    if model is None:
        return list(rows)
    validate = getattr(model, "model_validate", None)
    if callable(validate):
        return [validate(r) for r in rows]
    return [model(**{k: v for k, v in r.items() if k != "attributes"}) for r in rows]
