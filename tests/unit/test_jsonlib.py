"""Both JSON backends behave identically (simple-salesforce #442, #309: NaN became null)."""

from __future__ import annotations

import decimal
import json
from typing import Any

import pytest

from even_simpler_salesforce.core import jsonlib

BACKENDS = [jsonlib.std_dumps, jsonlib.orjson_dumps]
D = decimal.Decimal


@pytest.mark.parametrize("dumps", BACKENDS)
@pytest.mark.parametrize(
    "bad",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        D("NaN"),
        D("sNaN"),
        D("Infinity"),
        {"a": float("nan")},
        {"a": {"b": [1, {"c": float("inf")}]}},
        [D("-Infinity")],
        (1.0, float("nan")),
        {float("nan")},
        frozenset({float("inf")}),
    ],
)
def test_non_finite_rejected_before_sending(dumps: Any, bad: Any) -> None:
    with pytest.raises(jsonlib.NonFiniteNumber):
        dumps(bad)
    assert issubclass(jsonlib.NonFiniteNumber, ValueError)


@pytest.mark.parametrize("dumps", BACKENDS)
def test_error_names_the_path(dumps: Any) -> None:
    with pytest.raises(ValueError, match=r"\$\.a\.b\[1\]"):
        dumps({"a": {"b": [1.0, float("nan")]}})


@pytest.mark.parametrize("dumps", BACKENDS)
@pytest.mark.parametrize("value", ["12.3400", "1E+2", "-0", "0.000000001", "123456789012345678901234.5"])
def test_decimal_is_an_exact_json_number(dumps: Any, value: str) -> None:
    out = dumps({"amount": D(value), "list": [D(value)]})
    parsed = json.loads(out, parse_float=D, parse_int=D)
    assert parsed["amount"] == D(value)
    assert parsed["list"] == [D(value)]
    assert b'"' + value.encode() + b'"' not in out  # never a string


@pytest.mark.parametrize("dumps", BACKENDS)
def test_backends_agree(dumps: Any) -> None:
    obj = {"s": "x\x00esf-dec-00:1", "n": 1.5, "i": 3, "b": True, "z": None, "u": "ü", "d": D("2.50")}
    assert json.loads(dumps(obj), parse_float=D) == json.loads(jsonlib.std_dumps(obj), parse_float=D)
    assert json.loads(dumps(obj))["s"] == "x\x00esf-dec-00:1"  # look-alike strings untouched


def test_loads_both_backends() -> None:
    assert jsonlib.std_loads(b'{"a":1}') == {"a": 1}
    assert jsonlib.orjson_loads(b'{"a":1}') == {"a": 1}
    assert jsonlib.BACKEND in ("orjson", "json")


@pytest.mark.parametrize("dumps", BACKENDS)
def test_unsupported_type(dumps: Any) -> None:
    with pytest.raises(TypeError):
        dumps({"x": object()})
