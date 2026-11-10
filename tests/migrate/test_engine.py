"""Focused ``esf migrate`` engine cases that don't need a whole golden file. Inputs are synthetic."""

from __future__ import annotations

import pytest

pytest.importorskip("libcst")

from even_simpler_salesforce._migrate._engine import Options, transform_source  # noqa: E402

SYNC = "from simple_salesforce import Salesforce\n\n\ndef f(sf: Salesforce, d):\n"


def migrate(body: str, header: str = SYNC, **opts: object) -> tuple[str, list[tuple[str, str]]]:
    out, findings = transform_source(header + body, Options(**opts))  # type: ignore[arg-type]
    return out, [(f.rule, f.status) for f in findings]


@pytest.mark.parametrize("rule", ["sobject-attr", "upsert-path", "get-by-custom-id"])
def test_partial_rules_run_the_sobject_group_together(rule: str) -> None:
    body = '    sf.Account.upsert("Ext__c/123", d)\n    return sf.Account.get_by_custom_id("Ext__c", "1")\n'
    out, found = migrate(body, rules=frozenset({rule}))
    assert 'sf.sobject("Account").upsert("Ext__c", "123", d)' in out
    assert 'sf.sobject("Account").get_by_external_id("Ext__c", "1")' in out
    assert ("upsert-path", "rewritten") in found
    assert ("get-by-custom-id", "rewritten") in found
