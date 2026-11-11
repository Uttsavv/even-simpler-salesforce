"""Without the ``[pubsub]`` extra the library imports, and ``sf.pubsub`` names the extra (§5.5).

Each case runs in a fresh interpreter in which ``import grpc`` (or protobuf, or fastavro) fails,
so modules imported by other tests can't hide a top-level import.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

PRELUDE = """
import sys
for name in {blocked!r}:
    sys.modules[name] = None  # makes `import <name>` raise ImportError
"""


def run(blocked: list[str], body: str) -> subprocess.CompletedProcess[str]:
    code = PRELUDE.format(blocked=blocked) + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )


ALL = ["grpc", "grpc.aio", "google.protobuf", "fastavro"]


def test_library_and_testing_import_without_the_extra() -> None:
    out = run(
        ALL,
        """
        import even_simpler_salesforce as esf
        from even_simpler_salesforce.testing import FakeSalesforce, AsyncFakeSalesforce
        assert esf.PubSubEvent and esf.ChangeType.GAP_OVERFLOW.is_gap
        store = esf.InMemoryReplayStore()
        store.save("k", b"1")
        assert store.load("k") == b"1"
        fake = FakeSalesforce()
        fake.pubsub.emit("/event/Order_Event__e", {"n": 1})
        with fake.pubsub.subscribe("/event/Order_Event__e") as events:
            assert next(iter(events)).payload == {"n": 1}
        print("ok")
        """,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


@pytest.mark.parametrize("missing", ["grpc", "google.protobuf", "fastavro"])
@pytest.mark.parametrize("kind", ["Salesforce", "AsyncSalesforce"])
def test_subscribe_names_the_extra(missing: str, kind: str) -> None:
    out = run(
        [missing, f"{missing}.aio"] if missing == "grpc" else [missing],
        f"""
        import even_simpler_salesforce as esf
        sf = esf.{kind}(esf.StaticToken(access_token="x", instance_url="https://acme.my.salesforce.com"))
        try:
            sf.pubsub.subscribe("/event/Order_Event__e")
        except ImportError as exc:
            print(exc)
        else:
            print("no error")
        """,
    )
    assert out.returncode == 0, out.stderr
    assert "needs the [pubsub] extra: pip install 'even-simpler-salesforce[pubsub]'" in out.stdout
    assert missing in out.stdout


def test_bad_arguments_are_reported_before_the_missing_extra() -> None:
    out = run(
        ALL,
        """
        import even_simpler_salesforce as esf
        sf = esf.Salesforce(esf.StaticToken(access_token="x", instance_url="https://acme.my.salesforce.com"))
        try:
            sf.pubsub.subscribe("Order_Event__e")
        except ValueError as exc:
            print("ValueError", exc)
        """,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("ValueError topic must look like")


@pytest.mark.parametrize("module", ["_async._compat", "_sync._compat"])
def test_grpc_helpers_name_the_extra(module: str) -> None:
    out = run(
        ["grpc", "grpc.aio"],
        f"""
        from even_simpler_salesforce.{module} import grpc_channel
        try:
            grpc_channel("127.0.0.1:1")
        except ImportError as exc:
            print(exc)
        """,
    )
    assert "[pubsub] extra" in out.stdout, out.stderr


def test_decoding_names_the_extra() -> None:
    out = run(
        ["fastavro"],
        """
        import json
        from even_simpler_salesforce.core import pubsub as ps
        try:
            ps.parse_schema("s", '{"type": "record", "name": "E", "fields": []}', json.loads)
        except ImportError as exc:
            print(exc)
        """,
    )
    assert "[pubsub] extra" in out.stdout, out.stderr
    assert "fastavro" in out.stdout
