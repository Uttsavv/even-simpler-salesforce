"""Idempotence and formatting preservation for ``esf migrate`` (§9.19.5)."""

from __future__ import annotations

import difflib

import pytest

pytest.importorskip("libcst")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from even_simpler_salesforce._migrate._engine import Options, transform_source  # noqa: E402

HEADER = (
    "from simple_salesforce import Salesforce, SalesforceResourceNotFound, format_soql\n"
    "from simple_salesforce.aio import AsyncSalesforce\n\n"
)
SNIPPETS = [
    'sf.Lead.get("00Q000000000001AAA")',
    'sf.Invoice__c.update("a00000000000001AAA", {"Paid__c": True})',
    'res = sf.Lead.create({"LastName": "Doe"})',
    'x = res["id"]',
    'sf.Account.upsert("Ext__c/42", {"Name": "A"})',
    'sf.Account.upsert(f"Ext__c/{code}", {"Name": "A"})',
    'rows = sf.query_all("SELECT Id FROM Lead")["records"]',
    'page = sf.query(format_soql("SELECT Id FROM Lead WHERE Email = {}", email))',
    'total = page["totalSize"]',
    "more = sf.query_more(url, True)",
    'sf.restful("limits")',
    'sf.apexecute("orders", method="POST", data={"a": 1})',
    "v = sf.sf_version",
    "t = sf.session_id",
    "leads = sf.Lead",
    "# a standalone comment",
    "",
    'client.Lead.get("x")',
    'sf.collections.update("Lead", [])',
    'sf.sobject("Lead").get("x")',
]


def module(lines: list[str]) -> str:
    body = "\n".join([*("    " + line if line else "" for line in lines), "    pass"])
    return (
        HEADER
        + "sf = Salesforce(instance_url='https://example.my.salesforce.com', session_id='x')\n\n\n"
        + "def work(code, email, url, client):\n"
        + body
        + "\n"
    )


@settings(max_examples=150, deadline=None)
@given(st.lists(st.sampled_from(SNIPPETS), max_size=12), st.booleans())
def test_migrate_is_idempotent(lines: list[str], with_names: bool) -> None:
    opts = Options(client_names=frozenset({"sf"}) if with_names else frozenset())
    once, _ = transform_source(module(lines), opts)
    twice, findings = transform_source(once, opts)
    assert twice == once
    assert all(f.status != "rewritten" for f in findings)


@settings(max_examples=100, deadline=None)
@given(st.lists(st.sampled_from(SNIPPETS), max_size=12))
def test_untouched_lines_are_byte_identical(lines: list[str]) -> None:
    """Every line outside a rewritten node survives unchanged (comments and blank lines too)."""
    src = module(lines)
    out, findings = transform_source(src, Options())
    touched = {line for f in findings for line in range(f.line, f.end_line + 1)}
    touched |= {f.holder_line for f in findings}
    src_lines = src.splitlines()
    sm = difflib.SequenceMatcher(a=src_lines, b=out.splitlines(), autojunk=False)
    for tag, i1, i2, _, _ in sm.get_opcodes():
        if tag in ("replace", "delete"):
            for i in range(i1, i2):
                stripped = src_lines[i].strip()
                is_import = stripped.startswith(("from simple_salesforce", "import"))
                assert i + 1 in touched or is_import, (i + 1, src_lines[i])


def test_comments_and_blank_lines_survive() -> None:
    src = (
        "from simple_salesforce import Salesforce\n\n"
        "sf = Salesforce(instance_url='https://e.my.salesforce.com', session_id='x')  # trailing\n\n\n"
        "# leading comment\n"
        "sf.Lead.get('00Q000000000001AAA')  # keep me\n"
        "\n"
        "\n"
    )
    out, _ = transform_source(src, Options())
    assert "sf.sobject(\"Lead\").get('00Q000000000001AAA')  # keep me" in out
    assert "# leading comment\n" in out
    assert "session_id='x')  # trailing\n\n\n" in out
    assert out.endswith("# keep me\n\n\n")
