"""``ESF_DEBUG`` stderr observer (§11.6)."""

from __future__ import annotations

import os
import sys
from typing import TextIO

from .events import Event

__all__ = ["DebugPrinter", "debug_level"]


def debug_level() -> int:
    """``ESF_DEBUG`` as an int (0 when unset or invalid)."""
    try:
        return int(os.environ.get("ESF_DEBUG", "0") or 0)
    except ValueError:
        return 1


def _k(n: int) -> str:
    if n < 1000:
        return str(n)
    return f"{n // 1000}k" if n % 1000 == 0 else f"{n / 1000:.1f}k"


class DebugPrinter:
    """Prints one readable line per call to stderr."""

    def __init__(self, level: int = 1, stream: TextIO | None = None) -> None:
        self.level = level
        self.stream = stream

    def on_event(self, event: Event) -> None:
        out = self.stream or sys.stderr
        f = event.fields
        if event.name == "sf.request":
            parts = [
                "esf",
                f"{f.get('method', ''):<5}",
                f"{f.get('operation', ''):<14}",
                f"{f.get('sobject') or '':<9}",
                f"{f.get('status') or '---':<4}",
                f"{int(f.get('duration_ms') or 0):>4}ms",
            ]
            if f.get("outcome") == "error":
                parts.append(str(f.get("error_code") or f.get("error_class")))
                if f.get("error_message"):
                    parts.append(f'"{f["error_message"]}"')
            elif f.get("records") is not None:
                parts.append(f"{f['records']} rows")
            if f.get("attempts", 1) > 1:
                parts.append(f"attempts={f['attempts']}")
            if f.get("refreshed"):
                parts.append("refreshed")
            if f.get("api_used") is not None and f.get("api_max"):
                parts.append(f"api {_k(f['api_used'])}/{_k(f['api_max'])}")
            print("  ".join(parts), file=out)
        elif event.name == "sf.auth.refreshed":
            print(
                f"esf POST  oauth.refresh  {f.get('flow', '')}  {int(f.get('duration_ms') or 0)}ms"
                + ("  rotated" if f.get("rotated") else ""),
                file=out,
            )
        elif self.level >= 2 or event.name != "sf.retry":
            print(f"esf {event.name} " + " ".join(f"{k}={v}" for k, v in f.items()), file=out)
