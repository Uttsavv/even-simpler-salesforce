"""The pytest plugin: fixtures, sandbox marker, import hygiene."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from even_simpler_salesforce.testing import AsyncFakeSalesforce, FakeSalesforce

pytest_plugins = ["pytester"]


def test_fixtures_are_fresh_fakes(esf_fake: FakeSalesforce, esf_async_fake: AsyncFakeSalesforce) -> None:
    assert isinstance(esf_fake, FakeSalesforce)
    assert isinstance(esf_async_fake, AsyncFakeSalesforce)
    assert esf_fake.records("Lead") == []
    assert esf_async_fake.calls == []


def test_esf_replay_resolves_relative_paths(esf_replay, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    import httpx

    fx = Path(__file__).parent / "_fixture_tmp.json"
    fx.write_text(
        json.dumps(
            {
                "esf_fixture": 1,
                "interactions": [
                    {
                        "request": {"method": "GET", "path": "/x"},
                        "response": {"status": 200, "json": {"ok": True}},
                    }
                ],
            }
        )
    )
    try:
        transport = esf_replay("_fixture_tmp.json")
        with httpx.Client(transport=transport) as c:
            assert c.get("https://h/x").json() == {"ok": True}
        absolute = esf_replay(fx)
        assert isinstance(absolute, httpx.MockTransport)
    finally:
        fx.unlink()


SANDBOX_TEST = """
import pytest

@pytest.mark.esf_sandbox
def test_real_org(esf_sandbox_auth):
    assert esf_sandbox_auth.kind == "static"

def test_auth_fixture_skips(esf_sandbox_auth):
    pass
"""


def test_sandbox_marker_skips_without_env(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ACCESS_TOKEN", "INSTANCE_URL", "DOMAIN", "CLIENT_ID", "CLIENT_SECRET"):
        monkeypatch.delenv(f"ESF_SANDBOX_{name}", raising=False)
    pytester.makepyfile(test_sb=SANDBOX_TEST)
    result = pytester.runpytest_inprocess("-q", "-rs", "--strict-markers", "-p", "no:cacheprovider")
    result.assert_outcomes(skipped=2)
    result.stdout.fnmatch_lines(["*ESF_SANDBOX_*"])


def test_sandbox_marker_runs_with_env(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESF_SANDBOX_ACCESS_TOKEN", "00Dx!tok")
    monkeypatch.setenv("ESF_SANDBOX_INSTANCE_URL", "https://acme.my.salesforce.com")
    pytester.makepyfile(test_sb=SANDBOX_TEST)
    result = pytester.runpytest_inprocess("-q", "--strict-markers", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)


def test_marker_is_registered(pytester: pytest.Pytester) -> None:
    result = pytester.runpytest_inprocess("--markers")
    result.stdout.fnmatch_lines(["*esf_sandbox*"])


def test_imports_without_respx_or_pytest(tmp_path: Path) -> None:
    """``esf.testing`` and the plugin import even when respx (and, for testing, pytest) is missing."""
    code = textwrap.dedent(
        """
        import builtins, sys
        real = builtins.__import__
        BLOCK = {"respx", "pytest", "_pytest"}
        def fake_import(name, *a, **k):
            if name.split(".")[0] in BLOCK:
                raise ImportError("blocked " + name)
            return real(name, *a, **k)
        builtins.__import__ = fake_import
        import even_simpler_salesforce.testing as t
        t.FakeSalesforce().sobject("Lead").create({"LastName": "x"})
        try:
            t.TokenStoreContract
        except ImportError:
            pass
        else:
            raise SystemExit("contract should need pytest")
        BLOCK.discard("pytest"); BLOCK.discard("_pytest")
        import even_simpler_salesforce.testing.plugin
        print("ok")
        """
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"
