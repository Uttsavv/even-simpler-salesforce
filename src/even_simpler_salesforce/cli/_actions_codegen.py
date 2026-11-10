"""Render typed action stubs for ``esf generate actions`` (§9.7, §9.18). No I/O.

Input is the ``describe`` of each action (``GET actions/...``: ``inputs`` and ``outputs`` with
``name``, ``type``, ``required`` and ``maxOccurs``). Output is the source of one Python module
with a dataclass (or a Pydantic model) per action and per result.
"""

from __future__ import annotations

import json
import keyword
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

__all__ = ["ActionDescribe", "class_name", "py_type", "render_actions", "snake_case"]

# Describe ``type`` values (upper case) → the JSON value Python sees. Dates and times travel
# as ISO strings.
_TYPES: dict[str, str] = {
    **dict.fromkeys(
        (
            "STRING",
            "TEXTAREA",
            "ID",
            "REFERENCE",
            "PICKLIST",
            "MULTIPICKLIST",
            "COMBOBOX",
            "EMAIL",
            "PHONE",
            "URL",
            "ENCRYPTEDSTRING",
            "DATE",
            "DATETIME",
            "TIME",
        ),
        "str",
    ),
    "BOOLEAN": "bool",
    **dict.fromkeys(("INTEGER", "INT", "LONG"), "int"),
    **dict.fromkeys(("DOUBLE", "CURRENCY", "PERCENT", "DECIMAL"), "float"),
    "SOBJECT": "dict[str, Any]",
}
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NOT_IDENT = re.compile(r"[^0-9A-Za-z_]+")


@dataclass(frozen=True, slots=True)
class ActionDescribe:
    """One action to render: its kind, name and describe body."""

    kind: str
    name: str
    describe: Mapping[str, Any]


def snake_case(api_name: str) -> str:
    """``leadId`` → ``lead_id``, ``Flow__InterviewStatus`` → ``flow_interview_status``."""
    text = _NOT_IDENT.sub("_", _CAMEL.sub("_", api_name))
    text = re.sub(r"_+", "_", text).strip("_").lower() or "field"
    if text[0].isdigit():
        text = "f_" + text
    if keyword.iskeyword(text) or text.startswith("model_"):
        text += "_"
    return text


def class_name(api_name: str) -> str:
    """``Assign_Lead`` → ``AssignLead``, ``ns__Order_Service`` → ``NsOrderService``."""
    parts = [p for p in _NOT_IDENT.sub("_", api_name).split("_") if p]
    name = "".join(p[:1].upper() + p[1:] for p in parts) or "Action"
    return "A" + name if name[0].isdigit() else name


def py_type(param: Mapping[str, Any]) -> str:
    """Python annotation for one describe input or output (``Any`` for unknown types)."""
    base = _TYPES.get(str(param.get("type") or "").upper(), "Any")
    occurs: Any = param.get("maxOccurs")
    return f"list[{base}]" if isinstance(occurs, int) and occurs > 1 else base


def _unique(name: str, taken: set[str]) -> str:
    out, n = name, 2
    while out in taken:
        out, n = f"{name}{n}", n + 1
    taken.add(out)
    return out


def _params(describe: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    raw: Any = describe.get(key)
    if not isinstance(raw, list):
        return []
    return [cast("Mapping[str, Any]", p) for p in cast("list[Any]", raw) if isinstance(p, Mapping)]


def _field_lines(params: Sequence[Mapping[str, Any]], *, inputs: bool, pydantic: bool) -> list[str]:
    lines: list[str] = []
    taken: set[str] = set()
    for p in params:
        api = str(p.get("name") or "")
        if not api:
            continue
        attr = _unique(snake_case(api), taken)
        required = inputs and p.get("required") is True
        annotation = py_type(p) if required else f"{py_type(p)} | None"
        if pydantic:
            default = "" if required else "default=None, "
            lines.append(f"    {attr}: {annotation} = Field({default}alias={_q(api)})")
        else:
            default = "" if required else "default=None, "
            lines.append(f'    {attr}: {annotation} = field({default}metadata={{"api_name": {_q(api)}}})')
    return lines


def _q(text: str) -> str:
    """A double-quoted Python string literal (JSON string syntax is valid Python)."""
    return json.dumps(text)


def _doc(text: Any) -> str:
    one_line = " ".join(str(text or "").split()).replace('"""', "'''").replace("\\", "\\\\")
    return f'    """{one_line}"""' if one_line else ""


def render_actions(
    actions: Sequence[ActionDescribe],
    *,
    org: str,
    api_version: str,
    generated_at: datetime,
    pydantic: bool = False,
) -> str:
    """Source code of a module with one stub (and one result class) per action.

    Args:
        actions: The actions to render, in output order.
        org: Org instance URL, written in the header.
        api_version: API version the describes came from.
        generated_at: Timestamp written in the header.
        pydantic: Emit Pydantic models instead of frozen dataclasses.
    """
    taken: set[str] = set()
    classes: list[tuple[str, str, ActionDescribe, list[str], list[str]]] = []
    for a in actions:
        cls = _unique(class_name(a.name), taken)
        result = _unique(f"{cls}Result", taken)
        outputs = _field_lines(_params(a.describe, "outputs"), inputs=False, pydantic=pydantic)
        inputs = _field_lines(_params(a.describe, "inputs"), inputs=True, pydantic=pydantic)
        classes.append((cls, result, a, inputs, outputs))
    uses_any = any("Any" in line for c in classes for line in (*c[3], *c[4]))
    typing_names = ", ".join(n for n, used in (("Any", uses_any), ("ClassVar", pydantic)) if used)

    out: list[str] = [
        '"""Typed invocable action stubs for even-simpler-salesforce.',
        "",
        "Generated by `esf generate actions`. Do not edit: run the command again instead.",
        "",
        f"Org: {org}",
        f"API version: {api_version}",
        f"Generated: {generated_at.isoformat(timespec='seconds')}",
        '"""',
        "",
        "from __future__ import annotations",
        "",
    ]
    if pydantic:
        out.append("from typing import " + typing_names)
        out += ["", "from pydantic import BaseModel, ConfigDict, Field", ""]
    else:
        out.append("from dataclasses import dataclass, field")
        out += ([f"from typing import {typing_names}"] if typing_names else []) + [""]
    out.append("import even_simpler_salesforce as esf")
    if pydantic:
        out += ["", "_CONFIG = ConfigDict(frozen=True, populate_by_name=True, protected_namespaces=())"]
    out += ["", "__all__ = ["]
    out += [f'    "{n}",' for cls, result, *_ in classes for n in (cls, result)]
    out += ["]"]
    for cls, result, a, inputs, outputs in classes:
        doc = _doc(a.describe.get("description") or a.describe.get("label"))
        result_doc = f'    """Output values of {a.kind}:{a.name}."""'
        action = f"    __action__ = ({_q(a.kind)}, {_q(a.name)})"
        if pydantic:
            out += ["", "", f"class {result}(BaseModel):", result_doc, "", "    model_config = _CONFIG"]
            out += outputs
            out += ["", "", f"class {cls}(BaseModel, esf.Action[{result}]):"]
            out += [doc, ""] if doc else []
            out += ["    model_config = _CONFIG", action.replace(" = ", ": ClassVar[tuple[str, str]] = ", 1)]
            out += inputs
        else:
            out += ["", "", "@dataclass(frozen=True, kw_only=True)", f"class {result}:", result_doc]
            out += ["", *outputs] if outputs else []
            out += ["", "", "@dataclass(frozen=True, kw_only=True)", f"class {cls}(esf.Action[{result}]):"]
            out += [doc, ""] if doc else []
            out += [action, *inputs]
    return "\n".join(out) + "\n"
