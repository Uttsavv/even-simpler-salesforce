# ADR 0009: Framework integrations, testing support and quality gates

- **Status:** accepted, 2026-11-09
- **Source:** [`big-bang.md`](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md) §2, decisions D17, D22, D28 (details in §13–§15, §18)

## Context

Users need to test their own code without an org, and the library must not repeat the bug
classes found in the older libraries' issue trackers.

## Decision

- Optional **FastAPI** and **Flask** integrations, a **test fake** and a **pytest plugin** (D17).
  The fakes, recorder/replayer and plugin ship in 0.1. The packaged integrations are planned
  for 0.2; example apps show the wiring today.
- Quality gates: ruff, pyright strict, Python 3.10–3.14, runnable documentation examples and a
  minimum coverage (D22).
- Every design-caused bug class from simple-salesforce's issue history gets a **guard test** (D28;
  spec §15.1).

## Consequences

- `tests/docs` runs every Python block of these docs against a synthetic org.
- See [Testing](../testing.md).
