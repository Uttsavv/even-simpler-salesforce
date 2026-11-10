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


def _no_shortcuts(monkeypatch: pytest.MonkeyPatch) -> None:
    from even_simpler_salesforce._migrate import _engine

    monkeypatch.setattr(_engine, "_may_apply", lambda source, opts: True)
    monkeypatch.setattr(_engine, "_rules_can_apply", lambda facts, opts: True)


UNRELATED = [
    ("x = 1\n", {}),
    ("# an Account note\nclass Settings:\n    def get(self):\n        return self.Config.get()\n", {}),
    ('client.Lead.get("00Q000000000001AAA")\n', {}),  # not-a-client TODO without any import
    ("mock_sf.Lead.create.assert_called_once()\n", {"is_test_file": True}),
    ('sf.query_all("SELECT Id FROM Lead")\n', {"client_names": frozenset({"sf"})}),
    ("sf.Lead.get(x)\n", {"client_names": frozenset({"other"})}),
]


@pytest.mark.parametrize(("source", "opts"), UNRELATED)
def test_fast_paths_match_the_full_run(
    source: str, opts: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    fast = transform_source(source, Options(**opts))  # type: ignore[arg-type]
    _no_shortcuts(monkeypatch)
    full = transform_source(source, Options(**opts))  # type: ignore[arg-type]
    assert (fast[0], [f.as_json() for f in fast[1]]) == (full[0], [f.as_json() for f in full[1]])


def test_fast_path_skips_unrelated_library_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    from even_simpler_salesforce._migrate import _engine

    source = (Path(_engine.__file__).parents[1] / "_async" / "client.py").read_text(encoding="utf-8")

    def boom(*args: object) -> None:
        raise AssertionError("the transformer ran on an unrelated file")

    monkeypatch.setattr(_engine, "_run", boom)
    assert transform_source(source, Options()) == (source, [])


def test_fast_path_still_reports_parse_errors() -> None:
    import libcst

    with pytest.raises(libcst.ParserSyntaxError):
        transform_source("def broken(:\n", Options())
