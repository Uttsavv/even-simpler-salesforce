"""Golden-file tests for ``esf migrate`` (§9.19.5). All inputs are synthetic.

Each ``golden/<case>/`` has ``input.py``, ``expected.py`` and ``expected_report.json`` (rule,
status, line per finding), plus an optional ``case.json`` (``client_names``, ``file``: the path
the input pretends to live at, e.g. under ``tests/``). Regenerate with ``ESF_GOLDEN_UPDATE=1``
and review the diff before committing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("libcst")

from even_simpler_salesforce._migrate import _is_test_file  # noqa: E402
from even_simpler_salesforce._migrate._engine import Options, transform_source  # noqa: E402

GOLDEN = Path(__file__).parent / "golden"
CASES = sorted(p.name for p in GOLDEN.iterdir() if (p / "input.py").is_file())
UPDATE = os.environ.get("ESF_GOLDEN_UPDATE") == "1"


def run_case(case: str) -> tuple[str, list[dict[str, Any]]]:
    d = GOLDEN / case
    cfg: dict[str, Any] = json.loads((d / "case.json").read_text()) if (d / "case.json").is_file() else {}
    pretend = Path(cfg.get("file", f"src/{case}.py"))
    opts = Options(client_names=frozenset(cfg.get("client_names", ())), is_test_file=_is_test_file(pretend))
    out, findings = transform_source((d / "input.py").read_text(), opts)
    report = [{"rule": f.rule, "status": f.status, "line": f.line} for f in findings]
    return out, report


@pytest.mark.parametrize("case", CASES)
def test_golden(case: str) -> None:
    d = GOLDEN / case
    out, report = run_case(case)
    if UPDATE:
        (d / "expected.py").write_text(out)
        (d / "expected_report.json").write_text(json.dumps(report, indent=2) + "\n")
    assert out == (d / "expected.py").read_text()
    assert report == json.loads((d / "expected_report.json").read_text())


@pytest.mark.parametrize("case", CASES)
def test_golden_output_is_a_fixed_point(case: str) -> None:
    """Running again over the output changes nothing (idempotent)."""
    out, _ = run_case(case)
    cfg_path = GOLDEN / case / "case.json"
    cfg: dict[str, Any] = json.loads(cfg_path.read_text()) if cfg_path.is_file() else {}
    pretend = Path(cfg.get("file", f"src/{case}.py"))
    opts = Options(client_names=frozenset(cfg.get("client_names", ())), is_test_file=_is_test_file(pretend))
    again, findings = transform_source(out, opts)
    assert again == out
    assert all(f.status != "rewritten" for f in findings)


def test_every_rule_has_a_golden_case() -> None:
    from even_simpler_salesforce._migrate._findings import RULES

    seen = {e["rule"] for c in CASES for e in json.loads((GOLDEN / c / "expected_report.json").read_text())}
    assert set(RULES) <= seen, sorted(set(RULES) - seen)
