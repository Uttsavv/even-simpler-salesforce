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


def test_source_that_this_python_rejects_still_migrates() -> None:
    """The compile() safety net runs only when the original compiles here: a module-level
    ``return`` parses with libcst but not with compile(), like syntax newer than this Python."""
    out, found = migrate(
        '    pass\n\n\nsf.Lead.get("x")\nreturn 1\n',
        header="from simple_salesforce import Salesforce\n\nsf: Salesforce\n\n\ndef f():\n",
    )
    assert 'sf.sobject("Lead").get("x")' in out
    assert ("sobject-attr", "rewritten") in found


def test_except_star_handler_holds_its_marker() -> None:
    """``except*`` parses with libcst on every Python; the marker goes above the handler."""
    body = "    try:\n        pass\n    except* SalesforceGeneralError:\n        pass\n"
    out, found = migrate(body, header="from simple_salesforce import SalesforceGeneralError\n\n\ndef f():\n")
    lines = out.splitlines()
    i = lines.index("    except* SalesforceGeneralError:")
    assert lines[i - 1].lstrip().startswith("# esf-migrate: TODO[exceptions]")
    assert ("exceptions", "todo") in found
