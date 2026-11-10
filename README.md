<h1 align="center">
  <img src="https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/assets/banner.png" alt="even-simpler-salesforce: async-first Salesforce on httpx. Skip the boilerplate." width="100%">
</h1>

<p align="center">
  <a href="https://uttsavv.github.io/even-simpler-salesforce/"><b>Documentation</b></a> ·
  <a href="https://pypi.org/project/even-simpler-salesforce/"><b>PyPI</b></a> ·
  <a href="https://uttsavv.github.io/even-simpler-salesforce/getting-started/"><b>Getting started</b></a> ·
  <a href="https://github.com/Uttsavv/even-simpler-salesforce/blob/main/CHANGELOG.md"><b>Changelog</b></a>
</p>

<p align="center">
  <a href="https://pypi.org/project/even-simpler-salesforce/"><img src="https://img.shields.io/pypi/v/even-simpler-salesforce?color=8b5cf6&label=pypi" alt="PyPI version"></a>
  <img src="https://img.shields.io/badge/python-3.10%E2%80%933.14-3b82f6" alt="Python 3.10 to 3.14">
  <a href="https://github.com/Uttsavv/even-simpler-salesforce/actions/workflows/ci.yml"><img src="https://github.com/Uttsavv/even-simpler-salesforce/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://uttsavv.github.io/even-simpler-salesforce/"><img src="https://img.shields.io/badge/docs-latest-18181b" alt="Docs"></a>
  <a href="https://github.com/Uttsavv/even-simpler-salesforce/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-71717a" alt="License: Apache-2.0"></a>
</p>

A typed Salesforce client for Python built on [httpx](https://www.python-httpx.org/). It is
async-first, and a sync client with the same methods is generated from the async code. OAuth
refresh applies to every call, failures raise specific exception classes, each call produces one
structured log event with its API usage, and SOQL values are quoted for you. You pick a login
flow (client credentials, JWT bearer, web server with PKCE, or an existing token). The client
then handles tokens, retries, pagination, chunking and error mapping.

> [!WARNING]
> **Project status: alpha (0.1).** Breaking changes are possible in 0.x minor releases. They
> are always listed under *Changed* / *Removed* in the [changelog](CHANGELOG.md) with migration
> notes. So far the library has been tested against **one production org** only:
>
> | Tested on | |
> |---|---|
> | Edition | _TBD_ |
> | API version | 68.0 (library default) |
> | Auth flows exercised | _TBD_ |
>
> If you can run it against another edition, a sandbox, a scratch org or another login setup,
> please share your results by opening an issue or pull request.

## Install

```sh
pip install even-simpler-salesforce
```

The only required dependency is `httpx`. Extras add optional features:

| Extra | Adds | Needed for |
|---|---|---|
| `jwt` | PyJWT, cryptography | `esf.JwtBearer` |
| `pydantic` | Pydantic 2 | `model=` with Pydantic models |
| `speed` | orjson | faster JSON encoding and decoding |
| `http2` | `httpx[http2]` | `http2=True` |
| `otel` | opentelemetry-api | `esf.observability.OpenTelemetry` |
| `structlog` | structlog | `esf.observability.Structlog` |
| `testing` | pytest, respx | the `esf.testing` helpers and pytest plugin |
| `fastapi`, `flask` | the framework | the `even_simpler_salesforce.contrib.fastapi` and `.contrib.flask` integrations |
| `all` | everything above | |

```sh
pip install "even-simpler-salesforce[jwt,speed]"
```

## Quick start

```python
import asyncio
import os

import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain=os.environ["SF_DOMAIN"],  # https://<your-domain>.my.salesforce.com
    client_id=os.environ["SF_CLIENT_ID"],
    client_secret=os.environ["SF_CLIENT_SECRET"],
)


async def main() -> None:
    async with esf.AsyncSalesforce(auth) as sf:
        lead_id = await sf.sobject("Lead").create({"LastName": "Lovelace", "Company": "Engines"})
        page = await sf.query("SELECT Id, Name FROM Lead WHERE Id = {}", lead_id)
        print(page.records[0]["Name"], sf.usage.snapshot().api_used)


asyncio.run(main())
```

The sync client has the same methods, without `await`:

```python
with esf.Salesforce(auth) as sf:
    lead_id = sf.sobject("Lead").create({"LastName": "Hopper", "Company": "Navy"})
    for lead in sf.query_iter("SELECT Id, Name FROM Lead WHERE Status = {}", "Open"):
        print(lead["Name"])
```

## What 0.1 includes

- **Clients:** `AsyncSalesforce` and the generated `Salesforce`. `with_auth()` and `tagged()`
  return child clients that share the connection pool, caches and usage counters.
- **Login flows:** `ClientCredentials`, `JwtBearer`, `WebServerFlow` (PKCE, code exchange,
  revoke, userinfo, introspection) and `StaticToken`. A refresh runs once even when many
  concurrent calls need it, and a rotated refresh token is saved before the new access token is
  used. Durable token stores use compare-and-swap, and a reusable contract test suite checks
  your own store.
- **Records:** get, get by external id, create, update, upsert, delete, describe (cached and
  revalidated), updated/deleted windows, and conditional requests.
- **Query and search:** pass values after the SOQL (`sf.query("... WHERE Email = {}", email)`)
  and they're quoted for you. `query_one` / `query_first`, pages with opaque cursors you can
  sign, `query_iter` (optionally following child subqueries), `count`, `explain` (query plans),
  `esf.flatten` for CSV/DataFrame export, SOSL `search` and `parameterized_search`.
- **sObject Collections:** create, update, upsert, delete and retrieve, chunked (200 rows per
  write, 2,000 ids per read) and run concurrently. Results keep the input order, and a failure
  in one chunk raises `PartialFailure` with every row's result.
- **Raw requests:** `sf.request()` sends any REST call through the same auth, retry, error and
  logging pipeline.
- **Errors:** one exception hierarchy for REST, OAuth and Collections errors, with opt-in
  custom error classes matched on `[TAG]` prefixes in validation-rule messages.
- **Usage and budgets:** API usage read from the `Sforce-Limit-Info` header of every response,
  counters by operation, sObject and caller, threshold events and a guard for low-priority
  calls.
- **Logging:** one `sf.request` event per call with secrets redacted, observers for stdlib
  logging, structlog and OpenTelemetry, a `capture()` summary per unit of work, and readable
  `ESF_DEBUG=1` output.
- **Testing:** `FakeSalesforce` / `AsyncFakeSalesforce`, a recorder and replayer for redacted
  traffic, and a pytest plugin.
- **CLI:** `esf whoami`, `esf limits` and `esf query`.

Composite, GraphQL, Bulk API 2.0, files, invocable actions, Apex REST helpers, lead convert and
merge, Tooling, UI API, reports, Metadata and Pub/Sub are planned for later 0.x releases; see the
[documentation](https://uttsavv.github.io/even-simpler-salesforce/). Until then, `sf.request()`
can call any REST resource.

## Documentation

**https://uttsavv.github.io/even-simpler-salesforce/** covers getting started, Salesforce setup
(External Client Apps), each login flow, the guides, errors, logging, testing and the API
reference.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md). Commits need a DCO sign-off.

## Licence

Apache License 2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Not affiliated with or endorsed by Salesforce, Inc. "Salesforce" is a trademark of
Salesforce, Inc.
