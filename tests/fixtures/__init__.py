"""Fixture loader for ``tests/fixtures/recorded``.

A file there is one of two kinds (spec §18.4):

- **recorded**: written by ``esf.testing.record`` against a real org (``{"esf_fixture": 1,
  "interactions": [...]}``, redacted), usually with a ``source`` note naming the sandbox test that
  recorded it. Token requests (``/services/oauth2/...``) the recorder may have captured are skipped
  when counting interactions, so ``index`` means the same thing in every helper below.
- **synthetic**: hand-written from the Salesforce documentation where no recording exists yet,
  ``{"synthetic": true, "source": ..., "body": ...}``. :func:`load_fixture` returns ``body``.

Any other file is refused. Replace synthetic files with recordings when a sandbox can produce them.
"""

from __future__ import annotations

import base64
import copy
import functools
import json
from pathlib import Path
from typing import Any

RECORDED = Path(__file__).parent / "recorded"


@functools.cache
def _read(name: str) -> dict[str, Any]:
    data = json.loads((RECORDED / name).read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("esf_fixture") == 1 and isinstance(data.get("interactions"), list):
        return data
    if not (isinstance(data, dict) and data.get("synthetic") is True and "body" in data):
        raise ValueError(
            f"fixture {name} is neither a recording nor marked synthetic (see tests/fixtures/__init__.py)"
        )
    return data


def _calls(name: str) -> list[dict[str, Any]]:
    data = _read(name)
    if "esf_fixture" not in data:
        raise ValueError(f"fixture {name} is synthetic, not a recording")
    return [i for i in data["interactions"] if not str(i["request"]["path"]).startswith("/services/oauth2/")]


def is_recorded(name: str) -> bool:
    """Whether ``name`` is a real-org recording (not a synthetic fixture)."""
    return "esf_fixture" in _read(name)


def load_interactions(name: str) -> list[dict[str, Any]]:
    """A deep copy of a recording's interactions (``request`` / ``response``), token calls skipped."""
    return copy.deepcopy(_calls(name))


def load_interaction(name: str, index: int = 0) -> dict[str, Any]:
    """A deep copy of interaction ``index`` (``{"request": ..., "response": ...}``) of a recording."""
    return copy.deepcopy(_calls(name)[index])


def load_fixture(name: str, index: int = 0) -> Any:
    """The response body of a fixture: ``body`` (synthetic) or interaction ``index`` (recorded).

    A recorded body is the parsed JSON, else the text (``None`` for an empty body).
    """
    data = _read(name)
    if "esf_fixture" not in data:
        return copy.deepcopy(data["body"])
    response = load_interaction(name, index)["response"]
    if "json" in response:
        return response["json"]
    return response.get("text")


def load_response(name: str, index: int = 0) -> tuple[int, Any]:
    """``(status, body)`` of interaction ``index`` of a recording (a deep copy)."""
    response = load_interaction(name, index)["response"]
    return int(response["status"]), response.get("json")


def load_request(name: str, index: int = 0) -> Any:
    """The JSON request body of interaction ``index`` of a recording."""
    return load_interaction(name, index)["request"].get("json")


def recorded_response(name: str, index: int = 0) -> tuple[int, dict[str, str], bytes]:
    """``(status, headers, body bytes)`` of interaction ``index`` of a recording, for a mock."""
    response = load_interaction(name, index)["response"]
    if "json" in response:
        body = json.dumps(response["json"]).encode()
    elif "base64" in response:
        body = base64.b64decode(response["base64"])
    else:
        body = str(response.get("text", "")).encode()
    return int(response["status"]), dict(response.get("headers") or {}), body


def recording(name: str) -> Path:
    """The path of a recorded fixture (``esf.testing.record`` format), checked."""
    if not is_recorded(name):
        raise ValueError(f"fixture {name} is not an esf.testing.record recording")
    return RECORDED / name


def recorded_body(name: str, method: str, path_suffix: str, index: int = 0) -> Any:
    """The response body of the ``index``-th recorded ``method`` request whose path ends with
    ``path_suffix`` (JSON parsed, otherwise the text)."""
    found = [
        i["response"]
        for i in _calls(name)
        if i["request"]["method"] == method and str(i["request"]["path"]).endswith(path_suffix)
    ]
    response = found[index]
    return copy.deepcopy(response["json"]) if "json" in response else response.get("text", "")


def all_fixtures() -> list[str]:
    return sorted(p.relative_to(RECORDED).as_posix() for p in RECORDED.rglob("*.json"))
