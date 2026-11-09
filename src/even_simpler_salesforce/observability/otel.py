"""OpenTelemetry observer (``[otel]`` extra): spans and metrics."""

from __future__ import annotations

import importlib
import time
from typing import Any

from .events import Event

__all__ = ["OpenTelemetry"]


class OpenTelemetry:
    """A span per ``sf.request`` plus metrics.

    Metrics: counters ``esf.requests`` and ``esf.errors``, histogram ``esf.duration`` (ms),
    gauge ``esf.api_usage`` (fraction of the daily limit).
    """

    def __init__(self, tracer_provider: Any = None, meter_provider: Any = None) -> None:
        try:
            trace: Any = importlib.import_module("opentelemetry.trace")
            metrics: Any = importlib.import_module("opentelemetry.metrics")
        except ImportError as exc:  # pragma: no cover
            raise ImportError("OpenTelemetry observer needs the [otel] extra") from exc
        from .._version import __version__

        self._trace: Any = trace
        self.tracer: Any = trace.get_tracer("even_simpler_salesforce", __version__, tracer_provider)
        meter: Any = metrics.get_meter("even_simpler_salesforce", __version__, meter_provider)
        self.requests: Any = meter.create_counter("esf.requests", unit="1")
        self.errors: Any = meter.create_counter("esf.errors", unit="1")
        self.duration: Any = meter.create_histogram("esf.duration", unit="ms")
        self.usage: Any = (
            meter.create_gauge("esf.api_usage", unit="1") if hasattr(meter, "create_gauge") else None
        )

    def on_event(self, event: Event) -> None:
        """Record a span and metrics for each ``sf.request`` event."""
        if event.name != "sf.request":
            return
        f = event.fields
        attrs = {
            k: v
            for k in (
                "operation",
                "sobject",
                "method",
                "status",
                "outcome",
                "error_code",
                "caller",
                "priority",
            )
            if (v := f.get(k)) is not None
        }
        dur_ms = float(f.get("duration_ms") or 0)
        end_ns = time.time_ns()
        span: Any = self.tracer.start_span(
            f"salesforce {f.get('operation')}",
            start_time=end_ns - int(dur_ms * 1e6),
            attributes={
                **{f"salesforce.{k}": v for k, v in attrs.items()},
                "http.request.method": f.get("method") or "",
                "url.path": f.get("path_template") or "",
                "salesforce.attempts": f.get("attempts") or 1,
            },
        )
        if f.get("outcome") == "error":
            span.set_status(self._trace.Status(self._trace.StatusCode.ERROR, str(f.get("error_code") or "")))
        span.end(end_time=end_ns)
        self.requests.add(1, attrs)
        if f.get("outcome") == "error":
            self.errors.add(1, attrs)
        self.duration.record(dur_ms, attrs)
        used, mx = f.get("api_used"), f.get("api_max")
        if self.usage is not None and used is not None and mx:
            self.usage.set(used / mx)
