"""structlog observer (``[structlog]`` extra)."""

from __future__ import annotations

from typing import Any

from .events import Event

__all__ = ["Structlog"]


class Structlog:
    """Emit each event through a structlog logger as ``logger.info(event.name, **fields)``."""

    def __init__(self, logger: Any = None, name: str = "esf") -> None:
        if logger is None:
            try:
                import structlog  # pyright: ignore[reportMissingImports]
            except ImportError as exc:  # pragma: no cover
                raise ImportError("Structlog observer needs the [structlog] extra") from exc
            logger = structlog.get_logger(name)  # pyright: ignore
        self.logger: Any = logger

    def on_event(self, event: Event) -> None:
        method = "warning" if event.fields.get("outcome") == "error" else "info"
        getattr(self.logger, method)(event.name, **dict(event.fields))
