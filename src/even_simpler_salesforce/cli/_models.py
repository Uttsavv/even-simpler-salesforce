"""``esf generate models``: one typed model per sObject, from its describe (§9.18).

Types follow the describe ``Field`` properties (*DescribeSObjectResult*, SOAP API Developer
Guide; REST describe has the same ``fields[]``):
https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_calls_describesobjects_describesobjectresult.htm

- ``nillable`` fields become ``X | None``;
- a picklist with ``restrictedPicklist`` becomes ``Literal[...]`` of its active values;
- dates and times stay ``str``, because the client returns them as JSON strings.
"""

from __future__ import annotations

import argparse
import json
import keyword
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

from ._codegen import file_header, write_output
from ._env import client_from_env

__all__ = ["python_type", "register", "render_models", "run"]

_TYPES: dict[str, str] = {
    "boolean": "bool",
    "int": "int",
    "long": "int",
    "double": "float",
    "currency": "float",
    "percent": "float",
    "address": "dict[str, Any]",
    "location": "dict[str, Any]",
    "anyType": "Any",
}


def register(targets: Any) -> None:
    """Add ``models`` to the ``esf generate`` subparsers."""
    p = targets.add_parser("models", help="write a TypedDict (or pydantic model) per sObject")
    p.add_argument("sobjects", nargs="+", metavar="SOBJECT", help="sObject API names, e.g. Lead")
    p.add_argument("--out", metavar="FILE", help="write to FILE (default: stdout)")
    p.add_argument("--pydantic", action="store_true", help="write pydantic models instead of TypedDicts")
    p.set_defaults(handler=run)


def _field_list(describe: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    fields: Any = describe.get("fields")
    if not isinstance(fields, list):
        return []
    return [f for f in fields if isinstance(f, Mapping)]  # pyright: ignore[reportUnknownVariableType]


def _active_values(field: Mapping[str, Any]) -> list[str]:
    values: Any = field.get("picklistValues")
    if not isinstance(values, list):
        return []
    return [
        str(v["value"])  # pyright: ignore[reportUnknownArgumentType]
        for v in values  # pyright: ignore[reportUnknownVariableType]
        if isinstance(v, Mapping) and v.get("active", True) and v.get("value") is not None  # pyright: ignore[reportUnknownMemberType]
    ]


def _literal(text: str) -> str:
    """A double-quoted Python string literal (JSON string escapes are valid Python escapes)."""
    return json.dumps(text, ensure_ascii=False)


def python_type(field: Mapping[str, Any]) -> str:
    """The annotation for one describe field, without the ``| None`` for nillable fields."""
    kind = str(field.get("type") or "")
    if kind == "picklist" and field.get("restrictedPicklist"):
        values = _active_values(field)
        if values:
            return "Literal[" + ", ".join(_literal(v) for v in values) + "]"
    return _TYPES.get(kind, "str")


def _usable(name: Any) -> bool:
    return isinstance(name, str) and name.isidentifier() and not keyword.iskeyword(name)


def _class(describe: Mapping[str, Any], *, pydantic: bool) -> list[str]:
    name = str(describe.get("name") or "")
    if not _usable(name):
        raise ValueError(f"describe returned an sObject name that isn't a Python identifier: {name!r}")
    if pydantic:
        lines = [
            f"class {name}(BaseModel):",
            f'    """{name} row. Every field is optional, because a query returns only what it selects."""',
            "",
            '    model_config = ConfigDict(extra="ignore", protected_namespaces=())',
            "",
        ]
    else:
        lines = [
            f"class {name}(TypedDict, total=False):",
            f'    """{name} row. ``total=False``: a query returns only the fields it selects."""',
            "",
        ]
    for field in _field_list(describe):
        fname = field.get("name")
        if not _usable(fname):
            lines.append(f"    # skipped {fname!r}: not a Python identifier")
            continue
        annotation = python_type(field)
        if field.get("nillable") or pydantic:
            annotation += " | None"
        lines.append(f"    {fname}: {annotation}" + (" = None" if pydantic else ""))
    return lines


def render_models(describes: Sequence[Mapping[str, Any]], *, pydantic: bool, header: str) -> str:
    """Python source with one model per describe result."""
    imports = ["from typing import Any, Literal, TypedDict"]
    if pydantic:
        imports = ["from typing import Any, Literal", "", "from pydantic import BaseModel, ConfigDict"]
    parts = [header, "from __future__ import annotations", "", *imports]
    for describe in describes:
        parts += ["", "", *_class(describe, pydantic=pydantic)]
    names = ", ".join(_literal(str(d.get("name"))) for d in describes)
    parts += ["", "", f"__all__ = [{names}]"]
    return "\n".join(parts) + "\n"


def run(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    """Describe each sObject (one call each), render the models and write them."""
    with client_from_env(env) as sf:
        describes = [sf.sobject(name).describe() for name in args.sobjects]
        token = sf.auth.token()
        header = file_header(
            "Typed sObject models",
            command=["generate", "models", *args.sobjects]
            + (["--out", args.out] if args.out else [])
            + (["--pydantic"] if args.pydantic else []),
            instance_url=sf.instance_url,
            org_id=token.org_id,
            api_version=sf.api_version,
        )
    write_output(render_models(describes, pydantic=args.pydantic, header=header), args.out, out, err)
    return 0
