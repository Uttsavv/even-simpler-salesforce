"""Parsing of the ``Sforce-Limit-Info`` response header (§7.1 step 7)."""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["LimitInfo", "parse_limit_info"]

_PAIR = re.compile(r"\s*([A-Za-z-]+)\s*=\s*(\d+)\s*/\s*(\d+)\s*")


@dataclass(frozen=True, slots=True)
class LimitInfo:
    api_used: int | None = None
    api_max: int | None = None
    bursts_used: int | None = None
    bursts_max: int | None = None


def parse_limit_info(value: str | None) -> LimitInfo | None:
    """Parse ``api-usage=10018/100000; api-bursts=1/750``. Returns ``None`` if nothing usable."""
    if not value:
        return None
    used = mx = b_used = b_max = None
    for part in value.split(";"):
        m = _PAIR.fullmatch(part)
        if not m:
            continue
        name, a, b = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        if name == "api-usage":
            used, mx = a, b
        elif name == "api-bursts":
            b_used, b_max = a, b
    if used is None and b_used is None:
        return None
    return LimitInfo(used, mx, b_used, b_max)
