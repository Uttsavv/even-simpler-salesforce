"""Build the wheel, install it into a clean venv (no extras) and smoke-test it there.

    python scripts/smoke_wheel.py                 # build with `uv build --wheel`, then test
    python scripts/smoke_wheel.py dist/x.whl      # test an already built wheel

Stdlib only. Uses ``uv`` on PATH for the build and the venv when available (falls back to
``venv`` + ``pip`` for the install when a wheel is passed and uv is missing). Catches
packaging mistakes the in-repo test suite can't see: files missing from the wheel, a missing
``py.typed``, a runtime dependency that only the dev environment happens to provide.

simple-salesforce regressions this guards against (behaviour only, no code was read or
copied): #533, #641 (release missing modules / data files), #437 (``py.typed`` not shipped),
#715 (import failing without an optional dependency installed).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHECK = r"""
import asyncio
import importlib
import importlib.resources
import json

import httpx

import even_simpler_salesforce as esf

assert esf.__file__ and "site-packages" in esf.__file__, f"imported from the source tree: {esf.__file__}"
assert isinstance(esf.__version__, str) and esf.__version__, "missing __version__"
missing = [name for name in esf.__all__ if not hasattr(esf, name)]
assert not missing, f"__all__ names that don't resolve: {missing}"
assert importlib.resources.files("even_simpler_salesforce").joinpath("py.typed").is_file(), "py.typed missing"
for mod in ("_sync", "testing", "cli"):
    importlib.import_module(f"even_simpler_salesforce.{mod}")

INSTANCE = "https://smoke.my.salesforce.com"


def handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == "Bearer smoke-token"
    assert request.url.path.startswith("/services/data/v") and request.url.path.endswith("/limits")
    return httpx.Response(200, json={"DailyApiRequests": {"Max": 100, "Remaining": 90}})


transport = httpx.MockTransport(handler)
with esf.Salesforce(
    esf.StaticToken("smoke-token", INSTANCE), http_client=httpx.Client(transport=transport)
) as sf:
    limits = sf.limits()
assert limits["DailyApiRequests"].used == 10, limits


async def main() -> None:
    async with esf.AsyncSalesforce(
        esf.StaticToken("smoke-token", INSTANCE), http_client=httpx.AsyncClient(transport=transport)
    ) as sf:
        assert (await sf.limits())["DailyApiRequests"].remaining == 90


asyncio.run(main())
print(json.dumps({"ok": True, "version": esf.__version__, "public_names": len(esf.__all__)}))
"""


def run(*cmd: str | Path, **kw: object) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)  # type: ignore[arg-type]


def build(out: Path) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        sys.exit("smoke_wheel: `uv` not found on PATH (needed for `uv build --wheel`)")
    run(uv, "build", "--wheel", "--out-dir", out, cwd=ROOT)
    wheels = sorted(out.glob("*.whl"))
    if len(wheels) != 1:
        sys.exit(f"smoke_wheel: expected one wheel in {out}, found {wheels}")
    return wheels[0]


def main(argv: list[str]) -> int:
    with tempfile.TemporaryDirectory(prefix="esf-smoke-") as tmp:
        tmpdir = Path(tmp)
        wheel = Path(argv[0]).resolve() if argv else build(tmpdir / "dist")
        env_dir = tmpdir / "venv"
        bindir = env_dir / ("Scripts" if os.name == "nt" else "bin")
        python = bindir / ("python.exe" if os.name == "nt" else "python")
        uv = shutil.which("uv")
        if uv is not None:
            run(uv, "venv", "--quiet", "--python", sys.executable, env_dir)
            run(uv, "pip", "install", "--quiet", "--python", python, wheel)
        else:  # plain stdlib venv + pip
            venv.EnvBuilder(with_pip=True, clear=True).create(env_dir)
            run(python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", wheel)
        script = tmpdir / "check.py"
        script.write_text(CHECK, encoding="utf-8")
        # Run from the temp dir with -I so the source tree can't shadow the installed wheel.
        run(python, "-I", script, cwd=tmpdir)
        run(bindir / ("esf.exe" if os.name == "nt" else "esf"), "version", cwd=tmpdir)
    print(f"smoke_wheel: OK ({wheel.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
