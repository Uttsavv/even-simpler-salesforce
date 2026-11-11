"""OpenTelemetry observer (``[otel]`` extra): spans and metrics."""

from __future__ import annotations

import importlib
import time
from collections.abc import Iterable
from typing import Any

from .events import Event

__all__ = ["DEFAULT_METRIC_ATTRIBUTES", "OpenTelemetry"]

_SPAN_ATTRIBUTES = ("operation", "sobject", "method", "status", "outcome", "error_code", "caller", "priority")
# Bounded label sets only: every distinct combination becomes its own metric time series.
DEFAULT_METRIC_ATTRIBUTES: tuple[str, ...] = (
    "operation",
    "method",
    "status",
    "outcome",
    "error_code",
    "priority",
)


class OpenTelemetry:
    """A span per ``sf.request`` plus metrics.

    Metrics: counters ``esf.requests`` and ``esf.errors``, histogram ``esf.duration`` (ms),
    gauge ``esf.api_usage`` (fraction of the daily limit). For Pub/Sub subscriptions, counter
    ``esf.pubsub.events`` and histogram ``esf.pubsub.lag`` (ms between an event's commit or
    creation time and its arrival), both labelled with ``topic``.

    Spans carry every attribute. Metrics carry only ``metric_attributes``
    (:data:`DEFAULT_METRIC_ATTRIBUTES`), leaving out ``caller`` and ``sobject``, which can take
    many distinct values. Pass e.g. ``(*DEFAULT_METRIC_ATTRIBUTES, "sobject")`` to add them.
    """

    def __init__(
        self,
        tracer_provider: Any = None,
        meter_provider: Any = None,
        *,
        metric_attributes: Iterable[str] = DEFAULT_METRIC_ATTRIBUTES,
    ) -> None:
        self.metric_attributes = tuple(metric_attributes)
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
        self.pubsub_events: Any = meter.create_counter("esf.pubsub.events", unit="1")
        self.pubsub_lag: Any = meter.create_histogram("esf.pubsub.lag", unit="ms")
        self.usage: Any = (
            meter.create_gauge("esf.api_usage", unit="1") if hasattr(meter, "create_gauge") else None
        )

    def on_event(self, event: Event) -> None:
        """Record a span and metrics for each ``sf.request`` event, metrics for ``sf.pubsub.batch``."""
        if event.name == "sf.pubsub.batch":
            labels = {"topic": event.fields.get("topic") or ""}
            self.pubsub_events.add(int(event.fields.get("events") or 0), labels)
            lag = event.fields.get("lag_ms")
            if lag is not None:
                self.pubsub_lag.record(float(lag), labels)
            return
        if event.name != "sf.request":
            return
        f = event.fields
        attrs = {k: v for k in _SPAN_ATTRIBUTES if (v := f.get(k)) is not None}
        labels = {k: v for k in self.metric_attributes if (v := f.get(k)) is not None}
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
        self.requests.add(1, labels)
        if f.get("outcome") == "error":
            self.errors.add(1, labels)
        self.duration.record(dur_ms, labels)
        used, mx = f.get("api_used"), f.get("api_max")
        if self.usage is not None and used is not None and mx:
            self.usage.set(used / mx)
