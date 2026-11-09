"""Render docs/salesforce-dependencies.md from docs/salesforce-dependencies.yaml.

    python scripts/render_sf_index.py           # write the page
    python scripts/render_sf_index.py --check   # exit 1 if the page is stale

The YAML file is the source of truth; never edit the Markdown page by hand.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "salesforce-dependencies.yaml"
TARGET = ROOT / "docs" / "salesforce-dependencies.md"


def _link(url: str | None, label: str = "docs") -> str:
    return f"[{label}]({url})" if url else ""


def _checked(entry: dict[str, Any]) -> str:
    return str(entry.get("checked") or "")


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in rows]
    return [*out, ""]


def render(data: dict[str, Any]) -> str:
    v = data["api_versions"]
    oauth = data["oauth"]
    lines = [
        "<!-- Generated from salesforce-dependencies.yaml by scripts/render_sf_index.py. Do not edit. -->",
        "",
        "# Salesforce dependency index",
        "",
        "Every Salesforce endpoint, OAuth flow, header, response shape, error code and limit this",
        "library depends on. Review it against each Salesforce release; anything here that changes",
        "or is retired is a potential break. The source of truth is",
        "[`salesforce-dependencies.yaml`](salesforce-dependencies.yaml), which",
        "`tests/unit/test_salesforce_index.py` checks against the code.",
        "",
        f"**Last full review:** {data.get('last_full_review') or 'not yet reviewed against a release'}.",
        "*Checked* is the date an entry was last compared with the official documentation.",
        "",
        "## API versions",
        "",
        *_table(
            ["Item", "Value"],
            [
                ["Default API version (`DEFAULT_API_VERSION`)", f"`{v['default']}`"],
                ["Oldest supported version (`MIN_API_VERSION`)", f"`{v['minimum']}`"],
                ["End-of-life policy", _link(v.get("end_of_life_policy"), "Salesforce API retirement")],
            ],
        ),
        "## REST endpoints",
        "",
        "Paths are relative to `/services/data/vXX.X/` unless they start with `/services/`;",
        "`{}` is a value filled in at runtime.",
        "",
        *_table(
            ["Path", "Methods", "Used by", "Docs", "Checked"],
            [
                [f"`{e['path']}`", ", ".join(e["methods"]), e["used_by"], _link(e.get("docs")), _checked(e)]
                for e in data["rest_endpoints"]
            ],
        ),
        "## OAuth",
        "",
        "Hosts: " + "; ".join(f"`{k}` = {h}" for k, h in oauth["hosts"].items()) + ".",
        "",
        *_table(
            ["Endpoint", "Methods", "Used by", "Docs", "Checked"],
            [
                [f"`{e['path']}`", ", ".join(e["methods"]), e["used_by"], _link(e.get("docs")), _checked(e)]
                for e in oauth["endpoints"]
            ],
        ),
        *_table(
            ["grant_type", "Used by", "Docs", "Checked"],
            [
                [f"`{g['grant_type']}`", g["used_by"], _link(g.get("docs")), _checked(g)]
                for g in oauth["grant_types"]
            ],
        ),
        "Behaviour relied on:",
        "",
        *[f"- {b}" for b in oauth["behaviour"]],
        "",
        "## Headers",
        "",
        *_table(
            ["Header", "Direction", "Used for", "Docs", "Checked"],
            [
                [f"`{h['name']}`", h["direction"], h["used_for"], _link(h.get("docs")), _checked(h)]
                for h in data["headers"]
            ],
        ),
        "## Response shapes",
        "",
        *_table(
            ["Resource", "Relied on"],
            [[r["resource"], ", ".join(f"`{f}`" for f in r["fields"])] for r in data["response_shapes"]],
        ),
        "## Error codes",
        "",
        *_table(["errorCode", "Error class"], [[f"`{c}`", f"`{k}`"] for c, k in data["error_codes"].items()]),
        "Also relied on: "
        + "; ".join(f"`{e['code']}` ({e['note']})" for e in data["other_error_codes"])
        + ". HTTP statuses: "
        + ", ".join(str(s) for s in data["http_statuses"])
        + ".",
        "",
        "## Limits and sizes",
        "",
        *_table(["Name", "Value"], [[f"`{k}`", f"`{val}`"] for k, val in data["limits"].items()]),
    ]
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the page is out of date")
    args = parser.parse_args(argv)
    text = render(yaml.safe_load(SOURCE.read_text(encoding="utf-8")))
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print(
                f"{TARGET.relative_to(ROOT)} is stale: run python scripts/render_sf_index.py", file=sys.stderr
            )
            return 1
        return 0
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
