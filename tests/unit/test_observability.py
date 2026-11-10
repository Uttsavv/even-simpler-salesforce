from __future__ import annotations

import asyncio
import io
import logging
import threading
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.observability import (
    DebugPrinter,
    ObserverHub,
    OpenTelemetry,
    StdlibLogging,
    Structlog,
    Summary,
)
from even_simpler_salesforce.observability.debug import debug_level
from even_simpler_salesforce.observability.summary import record_event

from ..conftest import Recorder


def req(**fields: Any) -> esf.Event:
    base: dict[str, Any] = {
        "operation": "query",
        "sobject": "Lead",
        "method": "GET",
        "path_template": "/query",
        "status": 200,
        "outcome": "ok",
        "duration_ms": 115.4,
        "attempts": 1,
        "records": 2000,
        "api_used": 12000,
        "api_max": 100000,
        "refreshed": False,
    }
    base.update(fields)
    return esf.Event("sf.request", base)


# --- hub ----------------------------------------------------------------------------------------


class Boom:
    def on_event(self, event: esf.Event) -> None:
        raise RuntimeError("observer bug")


def test_hub_swallows_and_counts() -> None:
    rec = Recorder()
    hub = ObserverHub([Boom(), rec])
    ev = hub.emit("sf.request", {"a": 1})
    hub.emit("sf.retry", {})
    assert hub.errors == 2
    assert [e.name for e in rec.events] == ["sf.request", "sf.retry"]
    assert ev["a"] == 1
    assert ev.get("missing", 5) == 5
    hub.add(Recorder())
    assert len(hub.observers) == 3
    assert isinstance(rec, esf.Observer)


def test_hub_copies_fields() -> None:
    hub = ObserverHub()
    f = {"a": 1}
    ev = hub.emit("x", f)
    f["a"] = 2
    assert ev["a"] == 1


# --- StdlibLogging --------------------------------------------------------------------------------


def test_stdlib_logging(caplog: pytest.LogCaptureFixture) -> None:
    obs = StdlibLogging(logger="esf.test")
    with caplog.at_level(logging.INFO, logger="esf.test"):
        obs.on_event(req(error_code=None, retry_reasons=[], extra={}))
        obs.on_event(req(outcome="error", error_code="INVALID_FIELD", status=400))
        obs.on_event(esf.Event("sf.auth.reauth_required", {"token_key_hash": "abc"}))
    levels = [r.levelno for r in caplog.records]
    assert levels == [logging.INFO, logging.WARNING, logging.WARNING]
    first = caplog.records[0]
    assert first.getMessage().startswith("sf.request operation=query")
    assert "error_code" not in first.getMessage()
    assert "retry_reasons" not in first.getMessage()
    assert first.esf["operation"] == "query"
    assert first.esf_event == "sf.request"


def test_stdlib_logging_disabled_level_and_logger_object(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("esf.quiet")
    logger.setLevel(logging.ERROR)
    obs = StdlibLogging(logger=logger, level=logging.DEBUG)
    obs.on_event(req())
    obs.on_event(req(outcome="error"))
    assert [r for r in caplog.records if r.name == "esf.quiet"] == []
    logger.setLevel(logging.NOTSET)


# --- DebugPrinter --------------------------------------------------------------------------------


def test_debug_printer_lines() -> None:
    buf = io.StringIO()
    p = DebugPrinter(1, stream=buf)
    p.on_event(req())
    p.on_event(
        req(
            method="PATCH",
            operation="sobject.update",
            status=400,
            outcome="error",
            error_code="FIELD_CUSTOM_VALIDATION_EXCEPTION",
            error_message="[ADDR_REQUIRED] Street is required",
            duration_ms=88,
            records=1,
            attempts=2,
            refreshed=True,
            api_used=None,
        )
    )
    p.on_event(req(outcome="error", error_code=None, error_class="ConnectError", status=None, api_max=0))
    p.on_event(esf.Event("sf.auth.refreshed", {"flow": "web_server", "duration_ms": 240.2, "rotated": True}))
    p.on_event(esf.Event("sf.auth.refreshed", {"flow": "client_credentials", "duration_ms": None}))
    p.on_event(esf.Event("sf.retry", {"attempt": 1}))  # level 1: hidden
    p.on_event(esf.Event("sf.usage.threshold", {"threshold": 0.8}))
    lines = buf.getvalue().splitlines()
    assert lines[0].split() == [
        "esf",
        "GET",
        "query",
        "Lead",
        "200",
        "115ms",
        "2000",
        "rows",
        "api",
        "12k/100k",
    ]
    assert "FIELD_CUSTOM_VALIDATION_EXCEPTION" in lines[1]
    assert '"[ADDR_REQUIRED] Street is required"' in lines[1]
    assert "attempts=2" in lines[1]
    assert "refreshed" in lines[1]
    assert "api" not in lines[1].split()
    assert "ConnectError" in lines[2]
    assert "---" in lines[2]
    assert lines[3].startswith("esf POST  oauth.refresh  web_server  240ms")
    assert lines[3].endswith("rotated")
    assert "rotated" not in lines[4]
    assert "0ms" in lines[4]
    assert lines[5] == "esf sf.usage.threshold threshold=0.8"
    assert len(lines) == 6
    p2 = DebugPrinter(2, stream=buf)
    p2.on_event(esf.Event("sf.retry", {"attempt": 1}))
    assert buf.getvalue().splitlines()[-1] == "esf sf.retry attempt=1"
    p.on_event(req(api_used=500, api_max=900, records=None))
    assert "api 500/900" in buf.getvalue().splitlines()[-1]


def test_debug_printer_escapes_control_characters_in_messages() -> None:
    buf = io.StringIO()
    DebugPrinter(1, stream=buf).on_event(
        req(outcome="error", error_code="X", error_message="bad\x1b]0;pwned\x07\x1b[2J\nnext")
    )
    text = buf.getvalue()
    assert "\x1b" not in text
    assert "\x07" not in text
    assert text.count("\n") == 1
    assert '"bad\\x1b]0;pwned\\x07\\x1b[2J\\nnext"' in text


def test_debug_printer_defaults_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    DebugPrinter().on_event(req())
    assert capsys.readouterr().err.split()[:2] == ["esf", "GET"]


@pytest.mark.parametrize(("value", "level"), [(None, 0), ("", 0), ("0", 0), ("1", 1), ("2", 2), ("yes", 1)])
def test_debug_level(monkeypatch: pytest.MonkeyPatch, value: str | None, level: int) -> None:
    if value is None:
        monkeypatch.delenv("ESF_DEBUG", raising=False)
    else:
        monkeypatch.setenv("ESF_DEBUG", value)
    assert debug_level() == level


# --- Structlog / OpenTelemetry -------------------------------------------------------------------


class FakeLogger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, name: str, **kw: Any) -> None:
        self.calls.append(("info", name, kw))

    def warning(self, name: str, **kw: Any) -> None:
        self.calls.append(("warning", name, kw))


def test_structlog_observer() -> None:
    log = FakeLogger()
    s = Structlog(logger=log)
    s.on_event(req())
    s.on_event(req(outcome="error"))
    assert [c[0] for c in log.calls] == ["info", "warning"]
    assert log.calls[0][1] == "sf.request"
    assert log.calls[0][2]["operation"] == "query"
    assert Structlog().logger is not None  # real structlog


def test_opentelemetry_observer() -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    mp = MeterProvider(metric_readers=[reader])
    o = OpenTelemetry(tracer_provider=tp, meter_provider=mp)
    o.on_event(req(caller="job:x", priority="low"))
    o.on_event(req(outcome="error", error_code="INVALID_FIELD", status=400, api_used=None))
    o.on_event(esf.Event("sf.retry", {}))
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["salesforce query", "salesforce query"]
    assert spans[0].attributes["salesforce.caller"] == "job:x"
    assert spans[0].attributes["url.path"] == "/query"
    assert spans[1].status.status_code.name == "ERROR"
    names = {
        m.name
        for rm in reader.get_metrics_data().resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    assert {"esf.requests", "esf.errors", "esf.duration"} <= names
    labels = [
        dict(p.attributes or {})
        for rm in reader.get_metrics_data().resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "esf.requests"
        for p in m.data.data_points
    ]
    assert labels
    assert all("caller" not in a and "sobject" not in a for a in labels)


def test_otel_metric_attributes_opt_in() -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from even_simpler_salesforce.observability.otel import DEFAULT_METRIC_ATTRIBUTES

    reader = InMemoryMetricReader()
    o = OpenTelemetry(
        meter_provider=MeterProvider(metric_readers=[reader]),
        metric_attributes=(*DEFAULT_METRIC_ATTRIBUTES, "sobject"),
    )
    o.on_event(req())
    points = [
        dict(p.attributes or {})
        for rm in reader.get_metrics_data().resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "esf.requests"
        for p in m.data.data_points
    ]
    assert points == [{**{k: v for k, v in points[0].items() if k != "sobject"}, "sobject": "Lead"}]


# --- capture() ------------------------------------------------------------------------------------


def test_capture_summary_and_nesting() -> None:
    with esf.capture() as outer:
        record_event(req())
        with esf.capture() as inner:
            record_event(
                req(
                    outcome="error",
                    error_code="INVALID_FIELD",
                    attempts=3,
                    retry_reasons=["http_503", "http_503"],
                    operation="sobject.get",
                )
            )
            record_event(esf.Event("sf.auth.refreshed", {"initial": True}))
        record_event(esf.Event("sf.auth.refreshed", {"initial": False}))
        record_event(esf.Event("sf.retry", {}))
        record_event(req(outcome="error", error_code=None, error_class="ConnectError", api_used=None))
    record_event(req())  # outside: ignored
    assert inner.as_fields() == {
        "sf_calls": 1,
        "sf_ms": 115,
        "sf_errors": ["INVALID_FIELD"],
        "sf_retries": 2,
        "sf_refreshed": False,
        "sf_api_used": 12000,
        "sf_api_max": 100000,
        "sf_ops": {"sobject.get": 1},
        "sf_records_failed": 0,
    }
    f = outer.as_fields()
    assert f["sf_calls"] == 3
    assert f["sf_errors"] == ["INVALID_FIELD", "ConnectError"]
    assert f["sf_refreshed"] is True
    assert f["sf_ops"] == {"query": 2, "sobject.get": 1}


def test_capture_refreshed_flag_from_request() -> None:
    s = Summary()
    s.add(req(refreshed=True, operation=None))
    assert s.as_fields()["sf_refreshed"] is True
    assert s.as_fields()["sf_ops"] == {"raw": 1}


def test_capture_across_awaits_and_isolated_between_tasks() -> None:
    async def work(n: int) -> dict[str, Any]:
        with esf.capture() as s:
            for _ in range(n):
                await asyncio.sleep(0)
                record_event(req())
        return s.as_fields()

    async def main() -> list[dict[str, Any]]:
        return list(await asyncio.gather(work(1), work(3)))

    loop = asyncio.new_event_loop()
    try:
        a, b = loop.run_until_complete(main())
    finally:
        loop.close()
    assert (a["sf_calls"], b["sf_calls"]) == (1, 3)


def test_capture_is_thread_local_via_contextvars() -> None:
    seen: list[int] = []
    with esf.capture() as s:

        def other() -> None:
            record_event(req())  # new thread: fresh context, not captured
            seen.append(1)

        t = threading.Thread(target=other)
        t.start()
        t.join()
    assert seen == [1]
    assert s.calls == 0


# --- call_context -------------------------------------------------------------------------------


def test_call_context_nesting() -> None:
    from even_simpler_salesforce.context import current_call_context

    assert current_call_context().caller is None
    with esf.call_context(caller="route:/a", priority="high") as c1:
        assert c1.caller == "route:/a"
        with esf.call_context(priority="low") as c2:
            assert (c2.caller, c2.priority) == ("route:/a", "low")
        assert current_call_context().priority == "high"
    assert current_call_context().priority is None


def test_async_periodic_start_stop_idempotent(arun: Any) -> None:
    from even_simpler_salesforce._async._compat import Periodic, gather_settled

    calls: list[int] = []

    async def fn() -> None:
        calls.append(1)
        raise RuntimeError("swallowed")

    async def main() -> None:
        p = Periodic(fn, 3600)
        await p.stop()  # not started: no-op
        p.start()
        p.start()  # already running: no-op
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await p.stop()
        assert await gather_settled(1, []) == []

    arun(main())
    assert calls == [1]


def test_sync_compat_helpers() -> None:
    from even_simpler_salesforce._sync._compat import Periodic, gather_settled

    assert gather_settled(4, [lambda: 1, lambda: 2, lambda: 3]) == [1, 2, 3]
    assert gather_settled(1, [lambda: 1]) == [1]
    done = threading.Event()
    Periodic(done.set, 3600).stop()  # never started: no-op
    p = Periodic(done.set, 3600)
    p.start()
    p.start()
    assert done.wait(2)
    p.stop()
