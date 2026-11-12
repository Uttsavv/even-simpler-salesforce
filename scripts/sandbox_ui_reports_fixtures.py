#!/usr/bin/env python3
"""Turn the recordings of ``tests/sandbox/test_ui_reports.py`` into contract-test fixtures.

Usage::

    ESF_SANDBOX_RECORD_DIR=/tmp/esf-rec uv run pytest tests/sandbox/test_ui_reports.py -m sandbox
    uv run python scripts/sandbox_ui_reports_fixtures.py /tmp/esf-rec

Each fixture in ``tests/fixtures/recorded/ui`` and ``.../reports`` is one interaction of a
recording, kept in the recorder format (``esf_fixture: 1``). On top of the recorder's own
redaction this script:

- replaces user names (``displayName``, the ``displayValue`` of user lookups and the ``Name``
  of nested User records) with ``User <N>``;
- trims the parts of a few responses that are hundreds of kilobytes of org metadata and that
  no test reads (listed in each fixture's ``note``).

Read every fixture before committing it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "recorded"

# fixture -> (recording, interaction index, trim function name or None)
PLAN: dict[str, tuple[str, int, str | None]] = {
    "ui/list_views_lead.json": ("list_views_lead", 0, None),
    "ui/record_lead.json": ("shapes", 0, None),
    "ui/record_lead_full.json": ("record_parameters", 4, None),
    "ui/create_defaults_lead.json": ("shapes", 1, "defaults"),
    "ui/clone_defaults_lead.json": ("shapes", 2, "defaults"),
    "ui/picklist_values_lead.json": ("shapes", 3, "picklists"),
    "ui/object_info_lead.json": ("shapes", 4, None),
    "ui/list_records_page1.json": ("list_records", 0, None),
    "ui/list_records_page2.json": ("list_records", 1, None),
    "ui/error_unknown_object.json": ("errors", 0, None),
    "ui/error_unsupported_object.json": ("errors", 2, None),
    "ui/error_not_found.json": ("errors", 3, None),
    "ui/error_invalid_field.json": ("errors", 4, None),
    "ui/error_other_object_field.json": ("errors", 5, None),
    "ui/error_invalid_id.json": ("errors", 6, None),
    "ui/error_page_token.json": ("errors", 8, None),
    "ui/error_no_fields.json": ("record_parameters", 0, None),
    "ui/error_fields_and_layout.json": ("record_parameters", 1, None),
    "reports/describe.json": ("tabular_run", 1, "describe"),
    "reports/tabular_run.json": ("tabular_run", 0, None),
    "reports/tabular_run_no_details.json": ("tabular_run", 2, None),
    "reports/summary_run.json": ("summary_run", 0, None),
    "reports/summary_two_level_run.json": ("summary_two_level_run", 0, None),
    "reports/matrix_run.json": ("matrix_run", 0, None),
    "reports/matrix_two_level_run.json": ("matrix_run", 1, None),
    "reports/recent.json": ("recent", 1, None),
    "reports/instance_new.json": ("instances", 0, None),
    "reports/instance_success.json": ("instances", 1, None),
    "reports/instances_list.json": ("instances", 2, None),
    "reports/error_instance_not_found.json": ("instances", 3, None),
    "reports/error_not_found.json": ("errors_reports", 0, None),
    "reports/error_bad_filter_column.json": ("errors_reports", 1, None),
    "reports/error_bad_filter_operator.json": ("errors_reports", 2, None),
    "reports/error_dashboard_not_found.json": ("errors_reports", 4, None),
    "reports/dashboard.json": ("dashboard", 2, None),
    "reports/dashboard_status.json": ("dashboard", 1, None),
    "reports/dashboard_refresh.json": ("dashboard_refresh", 1, None),
    "reports/error_dashboard_refresh_too_soon.json": ("dashboard_refresh", 2, None),
}

USER_PREFIX = "005"
USER_NAME_FIELDS = ("Name", "NameOrAlias", "Alias", "FirstName", "LastName")


class Names:
    """Replaces user names with ``User <N>``, consistently."""

    def __init__(self) -> None:
        self.seen: dict[str, str] = {}

    def mask(self, name: Any) -> Any:
        if not isinstance(name, str) or not name:
            return name
        return self.seen.setdefault(name, f"User {len(self.seen) + 1}")

    def walk(self, node: Any) -> Any:
        if isinstance(node, list):
            return [self.walk(v) for v in node]
        if not isinstance(node, dict):
            return node
        out = {k: self.walk(v) for k, v in node.items()}
        if "displayName" in out:
            out["displayName"] = self.mask(out["displayName"])
        # UI API field: {"value": <user id or nested User record>, "displayValue": <name>}
        if set(out) == {"value", "displayValue"} and _is_user(out["value"]):
            out["displayValue"] = self.mask(out["displayValue"])
        # Nested UI API record of a user: its name fields.
        if _is_user(out) and isinstance(out.get("fields"), dict):
            for key in USER_NAME_FIELDS:
                name = out["fields"].get(key)
                if isinstance(name, dict):
                    name["value"] = self.mask(name.get("value"))
        return out


def _is_user(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith(USER_PREFIX) and len(value) in (15, 18)
    if isinstance(value, dict):
        rid = value.get("id")
        return isinstance(rid, str) and rid.startswith(USER_PREFIX) and "fields" in value
    return False


def _trim_describe(body: dict[str, Any]) -> str:
    meta = body["reportTypeMetadata"]
    for cat in meta.get("categories", [])[:1]:
        cat["columns"] = dict(list(cat.get("columns", {}).items())[:5])
    meta["categories"] = meta.get("categories", [])[:1]
    meta["standardDateFilterDurationGroups"] = meta.get("standardDateFilterDurationGroups", [])[:1]
    meta["dataTypeFilterOperatorMap"] = {}
    return (
        "trimmed: reportTypeMetadata.categories (first category, first 5 columns), "
        "standardDateFilterDurationGroups (first group), dataTypeFilterOperatorMap (emptied)"
    )


def _trim_defaults(body: dict[str, Any]) -> str:
    infos = body["objectInfos"]
    lead = infos.get("Lead")
    body["objectInfos"] = {"Lead": lead} if lead is not None else {}
    if lead is not None:
        keep = set(body["record"]["fields"])
        lead["fields"] = {k: v for k, v in lead["fields"].items() if k in keep}
    return "trimmed: objectInfos (only Lead, only the fields in record)"


def _trim_picklists(body: dict[str, Any]) -> str:
    fields = body["picklistFieldValues"]
    for name in ("CountryCode", "StateCode"):
        if name in fields:
            fields[name]["values"] = fields[name]["values"][:3]
    return "trimmed: picklistFieldValues.CountryCode and .StateCode values (first 3 of each)"


TRIMS = {"describe": _trim_describe, "defaults": _trim_defaults, "picklists": _trim_picklists}


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("recordings", type=Path, help="the ESF_SANDBOX_RECORD_DIR of the sandbox run")
    args = parser.parse_args()
    names = Names()
    for target, (source, index, trim) in PLAN.items():
        doc = json.loads((args.recordings / f"{source}.json").read_text())
        entry = names.walk(doc["interactions"][index])
        note = "recorded with esf.testing.record (tests/sandbox/test_ui_reports.py, API 68.0)"
        if trim is not None:
            note += "; " + TRIMS[trim](entry["response"]["json"])
        out = {"esf_fixture": doc["esf_fixture"], "note": note, "interactions": [entry]}
        path = OUT / target
        path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
        print(f"{target}: {path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
