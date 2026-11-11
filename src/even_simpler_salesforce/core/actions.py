"""Invocable actions (§9.7): paths, typed ``Action`` stubs and result parsing. No I/O."""

from __future__ import annotations

import dataclasses
import re
import typing
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Generic, TypeVar, cast

from ..errors import RecordError, SalesforceError, UnexpectedResponse
from ..models import ActionResult
from .classify import error_from_detail, parse_details
from .http import RequestSpec, url_segment

__all__ = [
    "ACTION_KINDS",
    "Action",
    "action_error",
    "action_inputs",
    "action_list_path",
    "action_path",
    "action_result_type",
    "build_action_result",
    "check_action_kind",
    "check_action_name",
    "parse_action_results",
]

R = TypeVar("R")

ACTION_KINDS = ("standard", "flow", "apex")
"""Action kinds with their own ``sf.actions`` method (and valid in ``Action.__action__``)."""

_KIND = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
# Flow API names, Apex class names (with an optional namespace) and standard action names.
# Custom actions such as email alerts use ``Object.Name``.
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)?$")


class Action(Generic[R]):
    """Base class for typed invocable action stubs; run one with ``sf.run(action)``.

    ``esf generate actions`` writes subclasses for you. A subclass is a dataclass (or a
    Pydantic model) whose fields are the action inputs, with ``__action__`` naming the action
    and the type argument naming the result type:

    Example:
        @dataclass(frozen=True, kw_only=True)
        class AssignLead(esf.Action[AssignLeadResult]):
            __action__ = ("flow", "Assign_Lead")
            lead_id: str = field(metadata={"api_name": "leadId"})

        result = await sf.run(AssignLead(lead_id="00Q..."))

    Attributes:
        __action__: ``(kind, name)``, where kind is ``"standard"``, ``"flow"`` or ``"apex"``.

    Dataclass fields send their ``metadata["api_name"]`` (else the field name). Pydantic models
    send their aliases. Fields set to ``None`` are not sent.
    """

    __action__: ClassVar[tuple[str, str]]


def check_action_kind(kind: str) -> str:
    """Validate an action kind (``"standard"``, ``"flow"``, ``"apex"`` or another custom type)."""
    if not isinstance(kind, str) or not _KIND.fullmatch(kind):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(f"invalid action kind: {kind!r}")
    return kind


def check_action_name(name: str) -> str:
    """Validate an action name before putting it in a path."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(f"invalid action name: {name!r}")
    return name


def action_list_path(kind: str) -> str:
    """``actions/standard`` or ``actions/custom/{kind}``."""
    check_action_kind(kind)
    return "actions/standard" if kind == "standard" else f"actions/custom/{kind}"


def action_path(kind: str, name: str) -> str:
    """Path of one action: ``actions/standard/{name}`` or ``actions/custom/{kind}/{name}``."""
    return f"{action_list_path(kind)}/{url_segment(check_action_name(name))}"


def parse_action_results(body: Any, expected: int) -> list[ActionResult]:
    """Parse an action invocation response: one entry per input, in input order.

    Raises:
        UnexpectedResponse: the body is not a list of ``expected`` result objects.
    """
    if not isinstance(body, list) or len(cast("list[Any]", body)) != expected:
        raise UnexpectedResponse(
            f"expected a list of {expected} action results", raw=repr(cast(object, body))[:4096]
        )
    out: list[ActionResult] = []
    for raw_item in cast("list[Any]", body):
        if not isinstance(raw_item, Mapping) or "isSuccess" not in raw_item:
            raise UnexpectedResponse("malformed action result", raw=repr(cast(object, raw_item))[:4096])
        item = cast("Mapping[str, Any]", raw_item)
        outputs: Any = item.get("outputValues")
        name: Any = item.get("actionName")
        out.append(
            ActionResult(
                ok=item.get("isSuccess") is True,
                outputs=dict(cast("Mapping[str, Any]", outputs)) if isinstance(outputs, Mapping) else {},
                errors=parse_details(item.get("errors") or []),
                action_name=str(name) if name is not None else None,
            )
        )
    return out


def action_error(
    result: ActionResult,
    *,
    status: int,
    spec: RequestSpec,
    error_tags: Mapping[str, type[SalesforceError]] | None = None,
) -> SalesforceError:
    """The typed error for one failed invocation (first error entry picks the class).

    Unknown codes map to :class:`RecordError`, as for DML: actions run org logic (Flows,
    Apex) that rejects records.
    """
    if not result.errors:
        return RecordError(
            "action failed without error details",
            status=status,
            operation=spec.operation,
            method=spec.method,
            path=spec.path_template,
        )
    return error_from_detail(
        result.errors[0], status=status, spec=spec, dml=True, error_tags=error_tags, errors=result.errors
    )


# --- typed stubs ---------------------------------------------------------------------------


def _check_action(action: Any) -> tuple[str, str]:
    cls: type[Any] = type(cast(object, action))
    spec: Any = getattr(cls, "__action__", None)
    if not (isinstance(spec, tuple) and len(cast("tuple[Any, ...]", spec)) == 2):
        raise TypeError(f"{type(action).__name__} needs __action__ = (kind, name)")
    kind, name = cast("tuple[Any, Any]", spec)
    if kind not in ACTION_KINDS:
        raise ValueError(f"__action__ kind must be one of {ACTION_KINDS}, got {kind!r}")
    return str(kind), check_action_name(name)


def action_inputs(action: Any) -> tuple[str, str, dict[str, Any]]:
    """``(kind, name, inputs)`` for a typed action; ``None`` values are left out.

    Raises:
        TypeError: ``action`` is neither a dataclass nor a Pydantic model, or has no
            ``__action__``.
    """
    kind, name = _check_action(action)
    dump = getattr(action, "model_dump", None)
    if callable(dump):
        data = cast("dict[str, Any]", dump(by_alias=True, exclude_none=True))
        return kind, name, data
    if not (dataclasses.is_dataclass(action) and not isinstance(action, type)):
        raise TypeError(f"{type(action).__name__} must be a dataclass or a Pydantic model")
    inputs: dict[str, Any] = {}
    for f in dataclasses.fields(action):
        value = getattr(action, f.name)
        if value is not None:
            inputs[str(f.metadata.get("api_name", f.name))] = value
    return kind, name, inputs


def action_result_type(cls: type[Any]) -> Any:
    """The ``R`` of ``Action[R]`` in ``cls``'s bases (``None`` when not given)."""
    for klass in cls.__mro__:
        for base in cast("Sequence[Any]", getattr(klass, "__orig_bases__", ())):
            if typing.get_origin(base) is Action:
                args = typing.get_args(base)
                return args[0] if args else None
    return None


def build_action_result(result_type: Any, outputs: Mapping[str, Any]) -> Any:
    """Convert output values into ``result_type``.

    A Pydantic model validates ``outputs`` (aliases are the API names). A dataclass gets the
    fields whose ``metadata["api_name"]`` (else name) appears in ``outputs``; other outputs are
    ignored. Anything else (``None``, ``dict``, ``Any``) returns a plain ``dict``.
    """
    validate = getattr(result_type, "model_validate", None)
    if callable(validate):
        return validate(dict(outputs))
    if isinstance(result_type, type) and dataclasses.is_dataclass(result_type):
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(result_type):
            api = str(f.metadata.get("api_name", f.name))
            if api in outputs:
                kwargs[f.name] = outputs[api]
        return result_type(**kwargs)
    return dict(outputs)
