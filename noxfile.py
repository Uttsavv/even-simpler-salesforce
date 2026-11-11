"""Nox sessions for even-simpler-salesforce.

Run with uv as the venv backend, e.g.::

    uvx nox -db uv              # default sessions
    uvx nox -db uv -s tests     # full Python matrix
    uvx nox -db uv -s tests-3.12 -- -k query

Every session installs the project from ``pyproject.toml`` with ``uv sync`` into the
nox-managed virtualenv, so the dependency set matches CI exactly.
"""

from __future__ import annotations

import os

import nox

nox.options.default_venv_backend = "uv"
nox.options.reuse_existing_virtualenvs = True
nox.options.error_on_missing_interpreters = False
nox.options.sessions = ["lint", "types", "unasync_check", "tests"]

PYTHONS = ["3.10", "3.11", "3.12", "3.13", "3.14"]
SYNC_DIR = "src/even_simpler_salesforce/_sync"


def _sync(session: nox.Session, *extra: str) -> None:
    """Install the project (all extras + dev group) into the session venv with uv."""
    session.run_install(
        "uv",
        "sync",
        "--all-extras",
        "--group",
        "dev",
        *extra,
        env={"UV_PROJECT_ENVIRONMENT": session.virtualenv.location},
    )


@nox.session(python=PYTHONS)
def tests(session: nox.Session) -> None:
    """Unit + contract (+ docs) tests with branch coverage."""
    _sync(session, f"--python={session.python}")
    args = session.posargs or ["tests", "examples", "-m", "not sandbox"]
    env = {"COVERAGE_FILE": f".coverage.{session.python}"}
    # `coverage run` (not `pytest --cov`): the pytest11 plugin imports the package before
    # pytest-cov starts, which would hide module-level lines from coverage.
    session.run("coverage", "run", "-m", "pytest", *args, env=env)
    session.run("coverage", "report", env=env)
    session.run("coverage", "xml", env=env)


@nox.session(python="3.12")
def lint(session: nox.Session) -> None:
    """ruff check + ruff format --check (the locked ruff, same as CI and pre-commit)."""
    _sync(session)
    session.run("ruff", "check", ".")
    session.run("ruff", "format", "--check", ".")


@nox.session(python="3.12")
def types(session: nox.Session) -> None:
    """pyright (strict, configured in pyproject.toml)."""
    _sync(session)
    session.run("pyright", *session.posargs)


@nox.session(python="3.12")
def unasync(session: nox.Session) -> None:
    """Regenerate src/even_simpler_salesforce/_sync from _async."""
    session.run("python", "scripts/unasync.py", *session.posargs)


@nox.session(python="3.12")
def unasync_check(session: nox.Session) -> None:
    """Fail if the committed _sync/ tree is stale."""
    session.run("python", "scripts/unasync.py")
    session.run("git", "diff", "--exit-code", "--", SYNC_DIR, external=True)
    # Also catch newly generated files that were never committed.
    untracked = session.run(
        "git", "ls-files", "--others", "--exclude-standard", "--", SYNC_DIR, external=True, silent=True
    )
    if isinstance(untracked, str) and untracked.strip():
        session.error(f"untracked generated files in {SYNC_DIR}:\n{untracked}")


@nox.session(python="3.12")
def docs(session: nox.Session) -> None:
    """Build the documentation site into site/."""
    _sync(session, "--group", "docs")
    session.run("zensical", "build", "--clean", "--strict", *session.posargs)


@nox.session(python="3.12")
def bench(session: nox.Session) -> None:
    """pytest-benchmark suite against recorded responses."""
    _sync(session)
    args = session.posargs or []
    session.run(
        "pytest",
        "tests/benchmarks",
        "--benchmark-only",
        "--benchmark-autosave",
        *args,
    )


# Lowest versions of the [pubsub] extra (the protobuf/grpcio floors are the versions the
# vendored stubs were generated with; fastavro 1.12 is the last line with Python 3.10 wheels).
PUBSUB_MIN = ("grpcio==1.85.1", "protobuf==7.35.1", "fastavro==1.12.*")


@nox.session(python="3.10")
def min_deps(session: nox.Session) -> None:
    """Unit + contract tests against the lowest supported httpx and [pubsub] dependencies."""
    _sync(session)
    session.install("httpx==0.27.*", *PUBSUB_MIN)
    session.run("python", "-c", "import httpx; print('httpx', httpx.__version__)")
    session.run("pytest", "tests/unit", "tests/contract", "-m", "not sandbox", *session.posargs)


@nox.session(python="3.12")
def pubsub_proto(session: nox.Session) -> None:
    """Fail if the vendored Pub/Sub proto or its generated stubs drifted (``-- --upstream`` too)."""
    _sync(session)
    session.install("grpcio-tools==1.85.1")
    session.run("python", "scripts/pubsub_proto.py", "--check", *session.posargs)


@nox.session(python="3.12")
def smoke_wheel(session: nox.Session) -> None:
    """Build the wheel, install it into a clean venv without extras and smoke-test it."""
    session.run("python", "scripts/smoke_wheel.py", *session.posargs)


@nox.session(python=False)
def changelog(session: nox.Session) -> None:
    """Preview the next CHANGELOG section from changelog.d/ fragments."""
    version = os.environ.get("VERSION", "Unreleased")
    session.run("uvx", "towncrier", "build", "--draft", "--version", version, external=True)
