"""Standard-library logging observer."""

from __future__ import annotations

import logging

from .events import Event

__all__ = ["StdlibLogging"]


class StdlibLogging:
    """Log each event to a stdlib logger. Errors are logged at WARNING.

    The fields are passed as ``extra={"esf": {...}}`` and also flattened into the message.
    The library never configures logging; attach handlers in your app.
    """

    def __init__(self, logger: str | logging.Logger = "esf", level: int = logging.INFO) -> None:
        self.logger = logging.getLogger(logger) if isinstance(logger, str) else logger
        self.level = level

    def on_event(self, event: Event) -> None:
        """Log ``event`` on the configured logger."""
        f = event.fields
        level = (
            logging.WARNING
            if f.get("outcome") == "error" or event.name.endswith("reauth_required")
            else self.level
        )
        if not self.logger.isEnabledFor(level):
            return
        flat = " ".join(f"{k}={v}" for k, v in f.items() if v is not None and v != [] and v != {})
        self.logger.log(level, "%s %s", event.name, flat, extra={"esf": dict(f), "esf_event": event.name})
