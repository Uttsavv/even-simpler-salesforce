"""``esf migrate`` command line: modes, report, exit codes (§9.19)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

pytest.importorskip("libcst")

from even_simpler_salesforce import __version__  # noqa: E402
from even_simpler_salesforce._migrate import iter_python_files, main  # noqa: E402
from even_simpler_salesforce.cli import main as esf_main  # noqa: E402

CLEAN = (
    "from simple_salesforce.aio import AsyncSalesforce\n\n\n"
    "async def f(sf: AsyncSalesforce):\n"
    "    return await sf.Lead.get('00Q000000000001AAA')\n"
)
TODO = (
    "from simple_salesforce import Salesforce\n\n\n"
    "def f(sf: Salesforce, url):\n"
    "    return sf.query_more(url, True)\n"
)
# synthetic, shaped like a Salesforce session id (split so the repo's secret scan doesn't match)
SECRET = "00D000000000001" + "!" + "AQ0AQH0dMHZfz972Szmpkb58urFRkgeBGsxLQJWwYMfAbUeeG7c1E6LYUfiDUkWe6"


def run(args: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(args, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_dry_run_prints_diff_and_changes_nothing(tmp_path: Path) -> None:
    f = tmp_path / "app.py"
    f.write_text(CLEAN)
    code, out, err = run([str(tmp_path)])
    assert code == 1  # rewrites are pending until --write
    assert '+    return await sf.sobject("Lead").get' in out
    assert f.read_text() == CLEAN
    assert "1 rewritten" in err


def test_write_then_check_is_green(tmp_path: Path) -> None:
    f = tmp_path / "app.py"
    f.write_text(CLEAN)
    assert run(["--write", str(f)])[0] == 1  # the simple-salesforce import is still needed
    text = f.read_text()
    assert 'sf.sobject("Lead")' in text
    assert "TODO[unsupported] still uses simple-salesforce (AsyncSalesforce)" in text
    assert run(["--check", str(f)])[0] == 1
    # resolve: type the client with esf and delete the marker
    lines = [ln for ln in text.splitlines() if "esf-migrate" not in ln and "simple_salesforce" not in ln]
    fixed = "\n".join(lines).replace("sf: AsyncSalesforce", "sf: esf.AsyncSalesforce")
    f.write_text("import even_simpler_salesforce as esf\n" + fixed + "\n")
    assert run(["--check", str(f)])[0] == 0


def test_check_fails_while_work_remains(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(CLEAN)
    before = (tmp_path / "a.py").read_text()
    assert run(["--check", str(tmp_path)])[0] == 1  # still in old form
    assert (tmp_path / "a.py").read_text() == before  # --check never writes
    (tmp_path / "a.py").write_text("# esf-migrate: TODO[login] x\nx = 1\n")
    assert run(["--check", str(tmp_path)])[0] == 1  # a marker is left


def test_todo_exits_1_and_markers_are_not_duplicated(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text(TODO)
    assert run(["--write", str(f)])[0] == 1
    first = f.read_text()
    assert first.count("TODO[query-more]") == 1
    assert first.count("TODO[unsupported]") == 1
    assert run(["--write", str(f)])[0] == 1
    assert f.read_text() == first


def test_report_schema_and_line_numbers_after_write(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text(TODO)
    rep = tmp_path / "r.json"
    run(["--write", "--report", str(rep), str(f)])
    data = json.loads(rep.read_text())
    assert list(data) == ["version", "esf_version", "mode", "summary", "findings", "errors"]
    assert data["version"] == 1
    assert data["esf_version"] == __version__
    assert data["mode"] == "write"
    assert data["summary"] == {"files": 1, "rewritten": 0, "needs_review": 0, "todo": 2, "errors": 0}
    finding = next(f for f in data["findings"] if f["rule"] == "query-more")
    assert set(finding) == {
        "file",
        "line",
        "col",
        "end_line",
        "end_col",
        "rule",
        "status",
        "before",
        "after",
        "message",
        "suggestion",
        "docs_url",
    }
    assert finding["docs_url"].endswith("/migrate/#query-more")
    lines = f.read_text().splitlines()
    assert "query_more" in lines[finding["line"] - 1]  # after the inserted marker line


def test_report_dry_run_lines_refer_to_original(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text(TODO)
    code, out, _ = run(["--report", "-", str(f)])
    data = json.loads(out[out.index("{") :])
    assert data["mode"] == "dry-run"
    assert next(f for f in data["findings"] if f["rule"] == "query-more")["line"] == 5


def test_report_redacts_long_strings_and_secrets(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    long = "x" * 120
    f.write_text(
        "from simple_salesforce import Salesforce\n"
        f"sf = Salesforce(instance_url='https://e.my.salesforce.com', session_id='{SECRET}')\n"
        f"sf.Lead.update('00Q000000000001AAA', {{'Description': '{long}'}})\n"
    )
    _, out, _ = run(["--report", "-", str(f)])
    text = out[out.index('{\n  "version"') :]
    assert SECRET not in text
    assert long not in text


def test_parse_error_is_reported_and_other_files_still_run(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("def broken(:\n")
    (tmp_path / "good.py").write_text(CLEAN)
    code, _, err = run(["--write", str(tmp_path)])
    assert code == 2
    assert "bad.py: parse error" in err
    assert 'sobject("Lead")' in (tmp_path / "good.py").read_text()


def test_usage_errors(tmp_path: Path) -> None:
    assert run([])[0] == 2
    assert run(["--rules", "nope", str(tmp_path)])[0] == 2
    assert run([str(tmp_path / "missing")])[0] == 2
    assert run(["--write", "--check", str(tmp_path)])[0] == 2


def test_rules_and_exclude(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(CLEAN + TODO)
    (tmp_path / "gen").mkdir()
    (tmp_path / "gen" / "b.py").write_text(CLEAN)
    code, out, _ = run(["--rules", "query-more", "--exclude", "*/gen/*", "--report", "-", str(tmp_path)])
    data = json.loads(out[out.index('{\n  "version"') :])
    assert {f["rule"] for f in data["findings"]} == {"query-more"}
    assert data["summary"]["files"] == 1


def test_skips_virtualenvs(tmp_path: Path) -> None:
    for d in (".venv", "node_modules", ".git", "site-packages"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "x.py").write_text("x = 1\n")
    (tmp_path / "keep.py").write_text("x = 1\n")
    assert [p.name for p in iter_python_files([str(tmp_path)])] == ["keep.py"]


def test_esf_cli_dispatches_migrate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "a.py"
    f.write_text(CLEAN)
    assert esf_main(["migrate", "--check", str(f)]) == 1
    assert "esf migrate (check)" in capsys.readouterr().err


def test_test_files_get_mock_rule(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("def test_a(sf):\n    sf.Lead.get.assert_called_once()\n")
    _, out, _ = run(["--report", "-", str(tmp_path)])
    data = json.loads(out[out.index('{\n  "version"') :])
    assert [f["rule"] for f in data["findings"]] == ["mock-sobject"]


def test_every_rule_has_a_docs_anchor() -> None:
    """``docs_url`` links to ``.../migrate/#<rule>``: each rule id needs a heading there."""
    from even_simpler_salesforce._migrate._findings import RULES

    page = (Path(__file__).resolve().parents[2] / "docs" / "migrate.md").read_text(encoding="utf-8")
    missing = [r for r in RULES if f"### `{r}`" not in page]
    assert not missing, missing


def test_write_line_numbers_follow_added_and_removed_imports(tmp_path: Path) -> None:
    """Review M3: line N of the written file holds the finding's code, even when imports change."""
    f = tmp_path / "a.py"
    f.write_text(
        '''"""Doc."""
from simple_salesforce import Salesforce, SalesforceResourceNotFound
import os

sf = Salesforce(instance_url="https://e.my.salesforce.com", session_id="x")


def get(lead_id):
    try:
        return sf.Lead.get(lead_id)
    except SalesforceResourceNotFound:
        return sf.query_more(os.environ["URL"], True)
'''
    )
    rep_path = tmp_path / "r.json"
    run(["--write", "--report", str(rep_path), str(f)])
    lines = f.read_text().splitlines()
    data = json.loads(rep_path.read_text())
    assert data["findings"]
    for finding in data["findings"]:
        line = lines[finding["line"] - 1]
        assert not line.lstrip().startswith("# esf-migrate"), finding  # never a marker line
        if finding["rule"] == "unsupported":  # the import line itself was edited (a name pruned)
            assert line.startswith("from simple_salesforce import"), finding
            continue
        rewritten = finding["status"] == "rewritten" and finding["after"]
        shown = finding["after"] if rewritten else finding["before"]
        assert shown.splitlines()[0].strip().split("(")[0] in line, finding  # report text is redacted


def test_report_path_must_be_writable(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(CLEAN)
    before = (tmp_path / "a.py").read_text()
    code, _, err = run(["--write", "--report", str(tmp_path / "missing" / "r.json"), str(tmp_path)])
    assert code == 2
    assert "can't write the report" in err
    assert (tmp_path / "a.py").read_text() == before  # checked before anything is written


def test_report_redacts_multiline_strings(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    body = "\n".join(f"  AND Name != 'person{i}@example.com'" for i in range(4))
    f.write_text(
        "from simple_salesforce import Salesforce\n"
        "sf = Salesforce(instance_url='https://e.my.salesforce.com', session_id='x')\n"
        f'rows = sf.query_all("""SELECT Id FROM Lead WHERE Id != null\n{body}""")["records"]\n'
    )
    _, out, _ = run(["--report", "-", str(f)])
    text = out[out.index('{\n  "version"') :]
    assert "person0@example.com" not in text


def test_not_migrated_label_for_internal_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from even_simpler_salesforce._migrate import _engine

    def boom(source: str, opts: object) -> object:
        raise RuntimeError("a rewrite would produce invalid code (x, line 1)")

    monkeypatch.setattr(_engine, "transform_source", boom)
    (tmp_path / "a.py").write_text(CLEAN)
    code, _, err = run([str(tmp_path)])
    assert code == 2
    assert "not migrated: a rewrite would produce invalid code" in err


@pytest.mark.parametrize("newline", ["\r\n", "\n"])
def test_write_keeps_line_endings_and_mode(tmp_path: Path, newline: str) -> None:
    f = tmp_path / "app.py"
    f.write_bytes(CLEAN.replace("\n", newline).encode())
    f.chmod(0o640)
    run(["--write", str(f)])
    data = f.read_bytes()
    assert b'sf.sobject("Lead")' in data
    if newline == "\r\n":
        assert data.count(b"\n") == data.count(b"\r\n")  # no bare LF introduced
    else:
        assert b"\r" not in data
    assert f.stat().st_mode & 0o777 == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["app.py"]  # no temp file left behind


def test_failed_write_leaves_original_and_no_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    f = tmp_path / "app.py"
    f.write_text(CLEAN)

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        run(["--write", str(f)])
    assert f.read_text() == CLEAN
    assert [p.name for p in tmp_path.iterdir()] == ["app.py"]
