"""``esf skills`` (list, show, install) and the shipped probe template. No network."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

from even_simpler_salesforce import _version
from even_simpler_salesforce.cli import EXIT_OK, EXIT_USAGE, main
from even_simpler_salesforce.cli._skills import front_matter, skills
from even_simpler_salesforce.testing import FakeSalesforce

SKILLS_DIR = Path(__file__).resolve().parents[2] / "src" / "even_simpler_salesforce" / "skills"
EXPECTED = ["esf-debug", "esf-migrate", "esf-probe", "esf-testing", "esf-usage"]
# Agent Skills specification (https://agentskills.io/specification): lowercase letters, digits
# and single hyphens, at most 64 characters, equal to the directory name.
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


# --- the shipped files ------------------------------------------------------------------


def test_every_expected_skill_ships() -> None:
    assert list(skills()) == EXPECTED


@pytest.mark.parametrize("name", EXPECTED)
def test_front_matter_follows_the_agent_skills_format(name: str) -> None:
    text = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
    meta = front_matter(text)
    assert meta["name"] == name
    assert NAME.match(name)
    assert len(name) <= 64
    assert 50 <= len(meta["description"]) <= 1024
    assert "Use it" in meta["description"]  # says when to use it, with trigger phrases
    body = text.split("\n---\n", 1)[1]
    assert body.lstrip().startswith("# ")


@pytest.mark.parametrize("name", EXPECTED)
def test_skills_hold_no_library_version_or_secrets(name: str) -> None:
    for path in (SKILLS_DIR / name).rglob("*"):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert _version.__version__ not in text, path  # the wheel pins the version, not the text
        assert not re.search(r"\b0\.\d+\.\d+\b", text), path
        assert not re.search(r"00D[A-Za-z0-9]{12,15}!", text), path
        assert "react-admin" not in text.lower()


def test_front_matter_parser() -> None:
    assert front_matter("no front matter") == {}
    assert front_matter("") == {}
    text = "---\nname: x\ndescription: >-\n  one\n  two\nnot a pair\nquoted: 'q'\n---\nbody: no\n"
    assert front_matter(text) == {"name": "x", "description": "one two", "quoted": "q"}
    assert front_matter("---\nname: x\n") == {"name": "x"}  # no closing line


def test_install_copies_subdirectories_and_skips_build_output(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from even_simpler_salesforce.cli import _skills

    src = tmp_path / "src" / "demo"
    (src / "scripts").mkdir(parents=True)
    (src / "__pycache__").mkdir()
    (src / "SKILL.md").write_text("---\nname: demo\n---\n# Demo\n", encoding="utf-8")
    (src / "scripts" / "run.py").write_text("print()\n", encoding="utf-8")
    (src / "stale.pyc").write_bytes(b"")
    monkeypatch.setattr(_skills, "skills", lambda: {"demo": src})

    code, out, _ = run(capsys, "skills", "install", "--dir", str(tmp_path / "out"))

    assert code == EXIT_OK
    copied = sorted(p.relative_to(tmp_path / "out").as_posix() for p in (tmp_path / "out").rglob("*"))
    assert copied == ["demo", "demo/SKILL.md", "demo/scripts", "demo/scripts/run.py"]
    assert out.count("wrote ") == 2


# --- list and show ----------------------------------------------------------------------


def test_list(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "skills", "list")
    assert code == EXIT_OK
    lines = out.splitlines()
    assert [line.split()[0] for line in lines] == EXPECTED
    assert "Read-only introspection" in next(line for line in lines if line.startswith("esf-probe"))


def test_show(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "skills", "show", "esf-usage")
    assert code == EXIT_OK
    assert out == (SKILLS_DIR / "esf-usage" / "SKILL.md").read_text(encoding="utf-8")


def test_show_unknown(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "skills", "show", "nope")
    assert code == EXIT_USAGE
    assert "unknown skill nope" in err
    assert "esf-usage" in err


def test_skills_needs_an_action(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, _ = run(capsys, "skills")
    assert code == EXIT_USAGE


# --- install ----------------------------------------------------------------------------


def test_install_all_into_the_default_dir(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, out, err = run(capsys, "skills", "install")
    assert code == EXIT_OK
    base = tmp_path / ".claude" / "skills"
    assert sorted(p.name for p in base.iterdir()) == EXPECTED
    probe = base / "esf-probe"
    assert (probe / "SKILL.md").read_bytes() == (SKILLS_DIR / "esf-probe" / "SKILL.md").read_bytes()
    assert (probe / "probe_template.py").is_file()
    assert not list(base.rglob("__pycache__"))
    assert f"wrote {Path('.claude/skills/esf-probe/SKILL.md')}" in out.splitlines()
    assert "installed 5 skills in .claude/skills" in err


def test_install_named_into_a_dir(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    target = tmp_path / "agents" / "skills"
    code, out, err = run(capsys, "skills", "install", "esf-usage", "esf-usage", "--dir", str(target))
    assert code == EXIT_OK
    assert [p.name for p in target.iterdir()] == ["esf-usage"]
    assert out == f"wrote {target / 'esf-usage' / 'SKILL.md'}\n"
    assert err == f"installed 1 skill in {target}\n"


def test_install_refuses_to_overwrite(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    (tmp_path / "esf-debug").mkdir()
    (tmp_path / "esf-debug" / "SKILL.md").write_text("mine", encoding="utf-8")
    code, out, err = run(capsys, "skills", "install", "esf-usage", "esf-debug", "--dir", str(tmp_path))
    assert code == EXIT_USAGE
    assert "esf-debug already exists; pass --force" in err
    assert out == ""
    assert not (tmp_path / "esf-usage").exists()  # nothing is written when one target exists
    assert (tmp_path / "esf-debug" / "SKILL.md").read_text(encoding="utf-8") == "mine"


def test_install_force_replaces(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    old = tmp_path / "esf-debug"
    old.mkdir()
    (old / "stale.md").write_text("old", encoding="utf-8")
    (tmp_path / "esf-usage").write_text("a file in the way", encoding="utf-8")
    code, _, _ = run(capsys, "skills", "install", "esf-debug", "esf-usage", "--dir", str(tmp_path), "--force")
    assert code == EXIT_OK
    assert not (old / "stale.md").exists()
    assert (old / "SKILL.md").is_file()
    assert (tmp_path / "esf-usage" / "SKILL.md").is_file()


def test_install_unknown_name(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, _, err = run(capsys, "skills", "install", "esf-usage", "nope", "--dir", str(tmp_path))
    assert code == EXIT_USAGE
    assert "unknown skill nope" in err
    assert not list(tmp_path.iterdir())


def test_install_write_error(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    code, _, err = run(capsys, "skills", "install", "esf-usage", "--dir", str(blocker / "sub"))
    assert code == EXIT_USAGE
    assert err.startswith("esf: error: cannot write")


def test_install_help_names_the_codex_directory(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "skills", "install", "--help")
    assert code == EXIT_OK
    assert ".claude/skills" in out
    assert ".agents/skills" in out


# --- the probe template -----------------------------------------------------------------


def _template() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "esf_probe_template", SKILLS_DIR / "esf-probe" / "probe_template.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_template_login_follows_the_cli_order(tmp_path: Path) -> None:
    import even_simpler_salesforce as esf

    probe = _template()
    static = probe.login({"ESF_ACCESS_TOKEN": "t", "ESF_INSTANCE_URL": "https://acme.my.salesforce.com"})
    assert isinstance(static, esf.StaticToken)
    cc = {"ESF_DOMAIN": "https://acme.my.salesforce.com", "ESF_CLIENT_ID": "id", "ESF_CLIENT_SECRET": "s"}
    assert isinstance(probe.login(cc), esf.ClientCredentials)

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    (tmp_path / "server.key").write_bytes(pem)
    jwt = {**cc, "ESF_USERNAME": "u@example.com", "ESF_PRIVATE_KEY_FILE": str(tmp_path / "server.key")}
    assert isinstance(probe.login(jwt), esf.JwtBearer)
    with pytest.raises(SystemExit, match="no login"):
        probe.login({})


def test_template_connect_reads_the_api_version() -> None:
    probe = _template()
    env = {"ESF_ACCESS_TOKEN": "t", "ESF_INSTANCE_URL": "https://acme.my.salesforce.com"}
    with probe.connect({**env, "ESF_API_VERSION": "v66.0"}) as sf:
        assert sf.api_version == "66.0"
    with probe.connect(env) as sf:
        assert sf.api_version  # the library default


def test_template_greps_apex_bodies() -> None:
    probe = _template()
    fake = FakeSalesforce()
    fake.on_query(
        re.compile(r"FROM ApexClass WHERE Name LIKE '%Lead%'"),
        rows=[{"Name": "LeadHandler", "Body": "class LeadHandler {\n  l.OwnerId = u;\n  l.OwnerIds = x;\n}"}],
    )
    hits = list(probe.grep_apex(fake, r"\bOwnerId\b", name_like="%Lead%"))
    assert hits == [("LeadHandler", 2, "  l.OwnerId = u;")]
    with pytest.raises(ValueError, match="kind"):
        list(probe.grep_apex(fake, "x", kind="Flow"))


def test_template_main_only_reads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = _template()
    fake = FakeSalesforce()
    fake.on_query(
        re.compile(r"FROM ApexTrigger WHERE TableEnumOrId"),
        rows=[{"Name": "LeadTrigger", "Status": "Active"}],
    )
    fake.on_query(
        re.compile(r"SELECT Name, Body FROM ApexTrigger"),
        rows=[{"Name": "LeadTrigger", "Body": "x.OwnerId = y;"}],
    )
    fake.on_query(re.compile(r"SELECT Name, Body FROM ApexClass"), rows=[])
    fake.on_query(
        re.compile(r"FROM FlowDefinitionView"),
        rows=[{"ApiName": "Assign", "ProcessType": "AutoLaunchedFlow", "TriggerType": "RecordAfterSave"}],
    )
    fake.on_query(
        re.compile(r"FROM ValidationRule"),
        rows=[{"ValidationName": "Street", "Active": True, "ErrorMessage": "Street!"}],
    )
    fake.on_query(re.compile(r"FROM WorkflowRule"), rows=[{"Name": "Legacy"}])
    monkeypatch.setattr(probe, "connect", lambda: fake)

    probe.main()

    out = capsys.readouterr().out
    assert "LeadTrigger (Active)" in out
    assert "ApexTrigger LeadTrigger:1: x.OwnerId = y;" in out
    assert "Assign (AutoLaunchedFlow, RecordAfterSave)" in out
    assert "Street active=True: Street!" in out
    assert "Legacy" in out
    assert {c.operation for c in fake.calls} <= {"query", "query_more", "tooling.query", "tooling.query_more"}


def test_template_main_survives_a_failing_flow_query(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import even_simpler_salesforce as esf

    probe = _template()
    fake = FakeSalesforce()
    fake.on_query(lambda soql: True, rows=[])
    fake.fail_next("query", esf.InvalidField("No such column 'TriggerType'"))  # the data API query
    monkeypatch.setattr(probe, "connect", lambda: fake)
    probe.main()
    assert "skipped: " in capsys.readouterr().out
