#!/usr/bin/env python3
"""Deploy the test fixtures that ``tests/sandbox`` and ``verify_sandbox.py`` need into a test org.

Usage (after ``scripts/sandbox_login.py``)::

    uv run python scripts/sandbox_setup_org.py              # deploy core + extras
    uv run python scripts/sandbox_setup_org.py --check-only # validate, save nothing

It deploys ``tests/sandbox/org/core``, ``tests/sandbox/org/extras`` and
``tests/sandbox/org/actions`` with ``sf.metadata.deploy`` (so the deploy path is exercised too),
and assigns the ``Esf_Sandbox`` permission set to the logged-in user:

- core: ``Lead.Esf_Ext__c`` (external id), the V5b validation rule and Apex trigger, the
  platform event ``ESF_Sandbox_Event__e`` (``Message__c``), Change Data Capture on Account, the
  Apex REST class ``EsfSandboxRest`` (``/services/apexrest/esf-sandbox/``) and the invocable
  action ``EsfSandboxAction``;
- extras: a before-save Lead flow with a Custom Error (V5b) and a tabular and a summary Lead
  report in ``unfiled$public``;
- actions: the autolaunched Flow ``Esf_Sandbox_Action_Flow`` (V31-V33; input ``name``, output
  ``greeting``; ``name='fail'`` fails that interview).

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
PERMISSION_SET = "Esf_Sandbox"


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


def _assign_permission_set(sf: esf.Salesforce) -> None:
    ps = sf.query(f"SELECT Id FROM PermissionSet WHERE Name = '{PERMISSION_SET}'").records
    if not ps:
        print(f"permission set {PERMISSION_SET} not found; not assigned")
        return
    user_id = sf.auth.userinfo()["user_id"]
    existing = sf.count(
        "SELECT COUNT() FROM PermissionSetAssignment "
        f"WHERE AssigneeId = '{user_id}' AND PermissionSetId = '{ps[0]['Id']}'"
    )
    if existing:
        print(f"permission set {PERMISSION_SET}: already assigned")
        return
    sf.sobject("PermissionSetAssignment").create({"AssigneeId": user_id, "PermissionSetId": ps[0]["Id"]})
    print(f"permission set {PERMISSION_SET}: assigned to the logged-in user")


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--check-only", action="store_true", help="validate the deploy; save nothing")
    args = parser.parse_args()

    settings = load_settings()
    if not settings.configured:
        sys.exit(f"not configured: {settings.skip_reason}")
    kwargs = settings.client_kwargs()
    store = settings.sync_store() if settings.web_server else None
    with esf.Salesforce(settings.auth(), store=store, **kwargs) as sf:
        print(f"org: {_guard(sf)}")
        ok = _deploy(sf, "core", args.check_only)
        ok = _deploy(sf, "extras", args.check_only) and ok
        ok = _deploy(sf, "actions", args.check_only) and ok
        if not args.check_only:
            _assign_permission_set(sf)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
