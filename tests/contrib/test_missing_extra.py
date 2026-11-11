"""Importing an integration without its framework names the extra to install."""

from __future__ import annotations

import importlib
import sys

import pytest


@pytest.mark.parametrize("framework", ["fastapi", "flask"])
def test_missing_framework_names_the_extra(monkeypatch: pytest.MonkeyPatch, framework: str) -> None:
    module = f"even_simpler_salesforce.contrib.{framework}"
    monkeypatch.setitem(sys.modules, framework, None)  # makes `import <framework>` fail
    monkeypatch.delitem(sys.modules, module, raising=False)
    with pytest.raises(ImportError, match=rf"\[{framework}\] extra"):
        importlib.import_module(module)
