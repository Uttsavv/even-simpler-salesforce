"""Read-only probe of a Salesforce org with even-simpler-salesforce: a template.

1. Copy this file to a throwaway path outside the source tree, for example
   ``/tmp/esf_probe_lead_owner.py``.
2. Set ``OBJECT`` and ``FIELD``, and edit ``main()``.
3. Set the ``ESF_*`` login variables of the ``esf`` command (see ``esf --help``) and run
   ``python /tmp/esf_probe_lead_owner.py``.
4. Delete the copy when you have the answer. Never commit it.

The probe only reads: SELECT queries on the data API and the Tooling API. Don't add DML,
``execute_anonymous``, deployments or ``sf.request`` calls with another method than GET. Don't
print tokens or ``sf.auth``.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import even_simpler_salesforce as esf

OBJECT = "Lead"  # the sObject API name to probe
FIELD = "OwnerId"  # the field API name to look for in Apex


def login(env: Mapping[str, str] = os.environ) -> esf.Auth:
    """The login flow from the ``ESF_*`` variables, checked in the same order as the ``esf`` CLI."""

    def get(name: str) -> str:
        return env.get(f"ESF_{name}", "").strip()

    if get("ACCESS_TOKEN") and get("INSTANCE_URL"):
        return esf.StaticToken(get("ACCESS_TOKEN"), get("INSTANCE_URL"))
    if get("DOMAIN") and get("CLIENT_ID") and get("USERNAME") and get("PRIVATE_KEY_FILE"):
        key = Path(get("PRIVATE_KEY_FILE")).expanduser().read_bytes()
        return esf.JwtBearer(
            domain=get("DOMAIN"), client_id=get("CLIENT_ID"), username=get("USERNAME"), private_key=key
        )
    if get("DOMAIN") and get("CLIENT_ID") and get("CLIENT_SECRET"):
        return esf.ClientCredentials(
            domain=get("DOMAIN"), client_id=get("CLIENT_ID"), client_secret=get("CLIENT_SECRET")
        )
    sys.exit(
        "no login: set ESF_ACCESS_TOKEN + ESF_INSTANCE_URL, or ESF_DOMAIN + ESF_CLIENT_ID + "
        "ESF_CLIENT_SECRET, or ESF_DOMAIN + ESF_CLIENT_ID + ESF_USERNAME + ESF_PRIVATE_KEY_FILE"
    )


def connect(env: Mapping[str, str] = os.environ) -> esf.Salesforce:
    """A sync client for the probe. ``ESF_API_VERSION`` pins the API version, as for the CLI."""
    version = env.get("ESF_API_VERSION", "").strip().removeprefix("v")
    if version:
        return esf.Salesforce(login(env), client_name="esf-probe", api_version=version)
    return esf.Salesforce(login(env), client_name="esf-probe")


def tooling(sf: Any, soql: str, *values: Any) -> list[dict[str, Any]]:
    """Every row of a Tooling API query. The client follows the pages."""
    return list(sf.tooling.query_iter(soql, *values))


def data(sf: Any, soql: str, *values: Any) -> list[dict[str, Any]]:
    """Every row of a data API query. The client follows the pages."""
    return list(sf.query_iter(soql, *values))


def grep_apex(
    sf: Any, pattern: str, *, name_like: str = "%", kind: str = "ApexClass"
) -> Iterator[tuple[str, int, str]]:
    """Yield ``(name, line number, line)`` for each Apex line that matches ``pattern``.

    SOQL can't filter on ``Body``, so the probe gets the bodies and searches them here.
    ``name_like`` is a SOQL ``LIKE`` pattern on the class or trigger name.
    """
    if kind not in ("ApexClass", "ApexTrigger"):
        raise ValueError("kind must be 'ApexClass' or 'ApexTrigger'")
    regex = re.compile(pattern)
    soql = esf.soql("SELECT Name, Body FROM {} WHERE Name LIKE {}", esf.soql_identifier(kind), name_like)
    for row in sf.tooling.query_iter(soql):
        for number, line in enumerate(str(row.get("Body") or "").splitlines(), 1):
            if regex.search(line):
                yield str(row["Name"]), number, line


def section(title: str) -> None:
    print(f"\n== {title}")


def main() -> None:
    with connect() as sf:
        section(f"Apex triggers on {OBJECT}")
        for t in tooling(sf, "SELECT Name, Status FROM ApexTrigger WHERE TableEnumOrId = {}", OBJECT):
            print(f"{t['Name']} ({t['Status']})")

        section(f"Apex lines that mention {FIELD}")
        field = re.compile(rf"\b{re.escape(FIELD)}\b")
        for kind in ("ApexTrigger", "ApexClass"):
            for name, number, line in grep_apex(sf, field.pattern, kind=kind):
                print(f"{kind} {name}:{number}: {line.strip()[:160]}")

        section(f"Active record-triggered flows on {OBJECT}")
        try:
            flows = data(
                sf,
                "SELECT ApiName, ProcessType, TriggerType FROM FlowDefinitionView "
                "WHERE TriggerObjectOrEvent.QualifiedApiName = {} AND IsActive = true",
                OBJECT,
            )
        except esf.SalesforceError as exc:  # field availability depends on the API version
            print(f"skipped: {exc}")
        else:
            for f in flows:
                print(f"{f['ApiName']} ({f['ProcessType']}, {f['TriggerType']})")

        section(f"Validation rules on {OBJECT}")
        for v in tooling(
            sf,
            "SELECT ValidationName, Active, ErrorMessage FROM ValidationRule "
            "WHERE EntityDefinition.QualifiedApiName = {}",
            OBJECT,
        ):
            print(f"{v['ValidationName']} active={v['Active']}: {v['ErrorMessage']}")

        section(f"Workflow rules on {OBJECT}")
        for w in tooling(sf, "SELECT Name FROM WorkflowRule WHERE TableEnumOrId = {}", OBJECT):
            print(w["Name"])

        usage = sf.usage.snapshot()
        print(f"\nAPI calls used by the org: {usage.api_used}/{usage.api_max}")


if __name__ == "__main__":
    main()
