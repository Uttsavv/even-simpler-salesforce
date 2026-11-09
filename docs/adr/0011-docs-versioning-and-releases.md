# ADR 0011: Docs site, versioning and releases

- **Status:** accepted, 2026-11-09
- **Source:** [`big-bang.md`](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md) §2, decisions D18, D19, D20, D21, D23, O1, O3, O4, O8, O13 (details in §16, §17)

## Context

The project needs reproducible releases and docs that one maintainer can keep up to date.

## Decision

- Docs are a static site on **GitHub Pages** (D18), built with **Zensical** from `zensical.toml`
  (O1). Pages live in `docs/`, and the build output in `site/` is gitignored (D19). On 0.x
  there are two copies, `latest` and `dev` (O13).
- **towncrier** fragments in `changelog.d/`, enforced by CI (D20).
- **SemVer**: 0.x until the interface settles; breaking changes on 0.x only in minor releases and
  always listed with migration notes; a written deprecation policy (D21).
- A **migration guide** from simple-salesforce, and no compatibility shim (D23).
- Apache-2.0 licence (O3). PyPI trusted publishing from version tags (O4).
- The Salesforce **API version is pinned per release** (68.0 for 0.1) and can be overridden per
  client. It is never "latest" (O8).

## Consequences

- 0.1.0 is released as **alpha** (`Development Status :: 3 - Alpha`).
- See the [changelog](../changelog.md).
