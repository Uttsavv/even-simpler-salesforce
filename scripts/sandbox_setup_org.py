#!/usr/bin/env python3
"""Deploy the test fixtures that ``tests/sandbox`` and ``verify_sandbox.py`` need into a test org.

Usage (after ``scripts/sandbox_login.py``)::

    uv run python scripts/sandbox_setup_org.py              # deploy every folder
    uv run python scripts/sandbox_setup_org.py --check-only # validate, save nothing
    uv run python scripts/sandbox_setup_org.py --folder records   # deploy one folder only

It deploys every folder under ``tests/sandbox/org`` (``core`` and ``extras`` first, then the
others in name order) with ``sf.metadata.deploy`` (so the deploy path is exercised too), and
assigns each folder's permission sets (``<folder>/permissionsets/*.permissionset``) to the
logged-in user. The main folders:

- core: ``Lead.Esf_Ext__c`` (external id), the V5b validation rule and Apex trigger, the
  platform event ``ESF_Sandbox_Event__e`` (``Message__c``), Change Data Capture on Account, the
  Apex REST class ``EsfSandboxRest`` (``/services/apexrest/esf-sandbox/``) and the invocable
  action ``EsfSandboxAction``;
- duplicates: the Lead matching rule ``Esf_Sandbox_Lead_Email_Match`` (same email) and the
  duplicate rule ``Esf_Sandbox_Lead_Duplicates`` (allow with an alert on create and edit, only
  for Leads with ``Company`` = ``esf-sandbox-duprule``);
- extras: a before-save Lead flow with a Custom Error (V5b) and a tabular and a summary Lead
  report in ``unfiled$public``;
- records: ``Lead.Esf_Ext_Multi__c`` (a non-unique external id, V19), the ``Esf_Sandbox_Queue``
  Lead queue and an active Lead assignment rule ``Esf_Sandbox`` that sends Leads with
  ``Company`` = ``ESF_ASSIGN`` to that queue (V24; it replaces the org's active Lead rule), and
  the custom object ``Esf_Sandbox_Item__c`` with a required, a defaulted and two picklist
  fields (V30), plus the ``Esf_Sandbox_Records`` permission set;
- actions: the autolaunched Flow ``Esf_Sandbox_Action_Flow`` (V31-V33);
- graphql: ``Lead.Esf_GraphQL_Hidden__c``, a field with no field-level security (V51);
- pubsub: the platform event ``ESF_Sandbox_Date_Event__e`` and Date/DateTime fields on Account
  (V77), plus the ``Esf_Sandbox_PubSub`` permission set;
- reports: a matrix Lead report in the ``Esf_Sandbox_Reports`` folder and a dashboard on the
  summary report in ``Esf_Sandbox_Dashboards`` (V60-V69);
- tooling: the Apex test class ``EsfSandboxActionTest`` (V46, V86).

It refuses to run against a production org: ``Organization.IsSandbox`` must be true or the
org must be a Developer Edition. The fixtures change org behaviour on purpose (Leads with
``Company`` = ``ESF_VR``/``ESF_FLOW``/``ESF_ADDERROR``/``ESF_THROW`` fail), so use a dedicated
org. Afterwards, set::

    ESF_SANDBOX_EXTERNAL_ID_FIELD=Lead.Esf_Ext__c
    ESF_SANDBOX_PUBSUB=1
    ESF_SANDBOX_V5B_VALIDATION_RULE='{"sobject":"Lead","record":{"LastName":"esf-sandbox-v5b","Company":"ESF_VR"}}'
    ESF_SANDBOX_V5B_FLOW_ERROR='{"sobject":"Lead","record":{"LastName":"esf-sandbox-v5b","Company":"ESF_FLOW"}}'
    ESF_SANDBOX_V5B_APEX_ADD_ERROR='{"sobject":"Lead","record":{"LastName":"esf-sandbox-v5b","Company":"ESF_ADDERROR"}}'
    ESF_SANDBOX_V5B_TRIGGER_EXCEPTION='{"sobject":"Lead","record":{"LastName":"esf-sandbox-v5b","Company":"ESF_THROW"}}'
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.sandbox.settings import load_settings  # noqa: E402

import even_simpler_salesforce as esf  # noqa: E402

ORG_DIR = ROOT / "tests" / "sandbox" / "org"
FIRST = ("core", "extras")
FOLDERS = (
    *FIRST,
    *sorted(p.name for p in ORG_DIR.iterdir() if (p / "package.xml").is_file() and p.name not in FIRST),
)


def _permission_sets(folder: str) -> list[str]:
    return sorted(p.stem for p in (ORG_DIR / folder / "permissionsets").glob("*.permissionset"))


def _guard(sf: esf.Salesforce) -> str:
    org = sf.query("SELECT IsSandbox, OrganizationType FROM Organization").records[0]
    kind = f"{org['OrganizationType']}, sandbox={org['IsSandbox']}"
    if not org["IsSandbox"] and org["OrganizationType"] != "Developer Edition":
        sys.exit(f"refusing to deploy test fixtures into a production org ({kind})")
    return kind


def _deploy(sf: esf.Salesforce, name: str, check_only: bool) -> bool:
    job = sf.metadata.deploy(ORG_DIR / name, options=esf.DeployOptions(check_only=check_only))
    result = sf.metadata.deploy_status(job.id, wait=True, timeout=900)
    print(f"{name}: {result.status} ({result.components_deployed}/{result.components_total} components)")
    for f in result.component_failures:
        print(f"  FAILED {f.component_type} {f.full_name}: {f.problem}")
    return result.success


def _assign_permission_set(sf: esf.Salesforce, name: str) -> None:
    ps = sf.query(esf.soql("SELECT Id FROM PermissionSet WHERE Name = {}", name)).records
    if not ps:
        print(f"permission set {name} not found; not assigned")
        return
    user_id = sf.auth.userinfo()["user_id"]
    existing = sf.count(
        "SELECT COUNT() FROM PermissionSetAssignment "
        f"WHERE AssigneeId = '{user_id}' AND PermissionSetId = '{ps[0]['Id']}'"
    )
    if existing:
        print(f"permission set {name}: already assigned")
        return
    sf.sobject("PermissionSetAssignment").create({"AssigneeId": user_id, "PermissionSetId": ps[0]["Id"]})
    print(f"permission set {name}: assigned to the logged-in user")


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--check-only", action="store_true", help="validate the deploy; save nothing")
    parser.add_argument(
        "--folder", action="append", choices=FOLDERS, help="deploy only this folder (repeatable)"
    )
    args = parser.parse_args()

    settings = load_settings()
    if not settings.configured:
        sys.exit(f"not configured: {settings.skip_reason}")
    kwargs = settings.client_kwargs()
    store = settings.sync_store() if settings.web_server else None
    with esf.Salesforce(settings.auth(), store=store, **kwargs) as sf:
        print(f"org: {_guard(sf)}")
        ok = True
        for folder in args.folder or FOLDERS:
            ok = _deploy(sf, folder, args.check_only) and ok
            if not args.check_only:
                for name in _permission_sets(folder):
                    _assign_permission_set(sf, name)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
