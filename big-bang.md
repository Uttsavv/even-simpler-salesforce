# even-simpler-salesforce — Big-Bang Spec

> **Status:** draft 1, 2026-11-09. This is the single source of truth for building the
> library from an empty folder. Implement against it, then split it into `docs/adr/`,
> `docs/`, `CONTRIBUTING.md` and `AGENTS.md` once the first releases ship, and delete this file.
>
> **Conventions in this document**
> - **MUST / SHOULD / MAY** are used as in RFC 2119.
> - `Dn` = a decision taken during design. `On` = an open question that has since been resolved.
>   Both are listed in §2 so later readers can see *why*.
> - ⚠️ **VERIFY** marks a fact that was not confirmed against official Salesforce docs or a
>   live org. Each one is collected in §21 and MUST be checked before the feature that depends
>   on it is released.

---

## Table of contents

1. [What this is](#1-what-this-is)
2. [Decision log](#2-decision-log)
3. [Repository layout](#3-repository-layout)
4. [Public interface at a glance](#4-public-interface-at-a-glance)
5. [Architecture](#5-architecture)
6. [Authentication and tokens](#6-authentication-and-tokens)
7. [Transport: requests, retries, compression, limits](#7-transport)
8. [Errors](#8-errors)
9. [API areas](#9-api-areas)
10. [API usage and budgets](#10-api-usage-and-budgets)
11. [Observability: canonical logs](#11-observability-canonical-logs)
12. [Performance](#12-performance)
13. [Framework integrations](#13-framework-integrations)
14. [Testing support for users](#14-testing-support-for-users)
15. [The library's own test strategy](#15-the-librarys-own-test-strategy)
16. [Documentation site](#16-documentation-site)
17. [Versioning, changelog and releases](#17-versioning-changelog-and-releases)
18. [Quality gates and CI](#18-quality-gates-and-ci)
19. [Release plan](#19-release-plan)
20. [Migrating from simple-salesforce](#20-migrating-from-simple-salesforce)
21. [Verification backlog](#21-verification-backlog)
22. [Licence and governance](#22-licence-and-governance)
23. [Appendix A: Salesforce facts confirmed during design](#appendix-a-salesforce-facts-confirmed-during-design)

---

## 1. What this is

A Python client for the Salesforce platform APIs, async-first with a generated sync twin,
designed to replace `simple-salesforce` and its async forks.

### 1.1 Why it exists

Problems with the existing libraries:

| Problem | Consequence |
|---|---|
| Login is a single function with ~20 optional arguments; the auth type is inferred from which ones are `None` | Apps copy and patch the builder just to inject a proxy or timeout |
| No OAuth refresh-token support; "refresh" means logging in again with a password | Apps write their own refreshing wrappers with `__getattr__` pass-throughs, per-user locks and retries; refresh ends up covering only some methods |
| Relies on SOAP `login()` | `login()` is unavailable in API v65.0+ and retires in v31.0–64.0 with Summer '27. Before that: from 1 Dec 2026 it needs the "Use Any API Auth" permission in every org; from 15 Dec 2026 (sandboxes) / 31 Jan 2027 (production) it needs a security token for broad login IP ranges, and is switched off in orgs with no `login()` use in the previous 6 months |
| Objects reached through attribute magic (`sf.Lead`) | No types, no autocomplete, typos fail at runtime |
| Exceptions chosen by HTTP status, raw body in `.content` | Apps substring-match error bodies; the machine-readable `errorCode` is ignored |
| A new HTTP client per call (async fork) | No connection pooling, no lifespan management |
| Apps reach into internals (`.session_id`, `.base_url`, `.headers`) | Every upgrade is risky |
| Large gaps in API coverage | Bulk 2.0, GraphQL, UI API, Pub/Sub, invocable actions, Composite Graph and usage tracking are missing or partial |

`aiosalesforce` has the cleaner design and was used as a reference, but it has had no
release since June 2024, has a single maintainer, and also lacks refresh-token flows.

### 1.2 Goals

1. **One obvious way to connect** for each Salesforce OAuth flow, with refresh that covers every call.
2. **Full coverage of the modern Salesforce APIs**, shipped in stages (§19).
3. **Typed errors everywhere**, with one shape regardless of which API produced them.
4. **Async and sync with identical behaviour**, from one copy of the logic.
5. **Observable by default**: one canonical log event per Salesforce call, plus a per-request roll-up.
6. **Fast by default**: pooled connections, gzip, token caching, fewer round trips.
7. **Professionally maintained**: docs site, SemVer, changelog fragments, typed public surface, CI gates.
8. **General-purpose**: nothing specific to any one app or org.

### 1.3 Non-goals

- SOAP `login()`, username/password flows, zeep, Bulk API v1, CometD Streaming API.
- Evaluating validation-rule formulas or trigger logic on the client.
- A compatibility layer that mimics simple-salesforce's interface.
- Data Cloud, Einstein/Agentforce APIs (different hosts and auth; separate packages if ever needed).
- Event Monitoring log files (may be added later; not in the 1.0 scope).
- Configuring the app's logging (the library emits events; the app decides where they go).

### 1.4 Names

| Thing | Value |
|---|---|
| PyPI distribution | `even-simpler-salesforce` |
| Import package | `even_simpler_salesforce` |
| Conventional alias | `import even_simpler_salesforce as esf` |
| CLI | `esf` |
| Env var prefix | `ESF_` |
| Log/event namespace | `esf` (logger), `sf.*` (event names) |

---

## 2. Decision log

### 2.1 Decisions

| # | Decision |
|---|---|
| D1 | Build a **new library**, not a fork of simple-salesforce or its async fork. |
| D2 | **Separate public repository.** The owner manages git. |
| D3 | Name `even-simpler-salesforce`, imported as `even_simpler_salesforce`, aliased `esf`. |
| D4 | **General-purpose.** Nothing specific to any one app or org goes in. |
| D5 | **Full coverage of the modern Salesforce APIs**, shipped in stages (D24). |
| D6 | **Async first**, with a **sync twin** in the first release. |
| D7 | **httpx**, one shared connection pool per client family. |
| D8 | **Two plug-in points:** `Auth` (one class per login flow) and the HTTP transport. Long-lived tokens are loaded and saved through a `TokenStore`. |
| D9 | Login flows: `WebServerFlow` (PKCE), `ClientCredentials`, `JwtBearer`, `StaticToken` (plus `TokenExchange`, `DeviceFlow` later). The access token is held in memory. On `401 INVALID_SESSION_ID` the client refreshes **once** (concurrent callers wait on that one refresh) and retries. Proactive refresh is opt-in (`refresh_ahead=` or introspection). A rotated refresh token is saved before the new access token is used. The refresh POST is never blindly retried once it may have reached Salesforce. "Token request is already being processed" is handled by waiting and re-reading the store. |
| D10 | **No SOAP `login()`, no zeep, no attribute magic.** |
| D11 | **Typed errors for every source** (REST, OAuth, partial failures, Bulk, SOAP faults, HTML/proxy pages), all sharing one shape. |
| D12 | **Opt-in custom errors** by matching a tag in the error message; Salesforce has no custom error codes. |
| D13 | **No client-side validation-rule evaluation.** An opt-in describe-based field check (types, lengths, required, picklist values) may come later. |
| D14 | Lead convert and merge sit behind normal methods with SOAP hidden inside. REST has no convert or merge resource anywhere in Salesforce (platform-wide, not org-specific). |
| D15 | **Canonical logs:** one wide event per Salesforce call plus a roll-up into the app's own request log. Secrets always redacted. `ESF_DEBUG=1` prints readable lines. |
| D16 | **Performance:** token caching, connection reuse, gzip, usage from response headers, describe cache, fewer round trips, `Retry-After`-aware backoff, benchmark suite. |
| D17 | Optional **FastAPI** and **Flask** integrations, plus a test fake and pytest plugin. |
| D18 | Docs are a static site on **GitHub Pages**. No Read the Docs. |
| D19 | `docs/` holds the written pages; `site/` holds the build output and is gitignored. |
| D20 | **towncrier** changelog fragments in `changelog.d/`, enforced by CI. |
| D21 | **SemVer**, 0.x until the interface settles, with a written deprecation policy. |
| D22 | Quality gates: ruff, pyright strict, Python 3.10–3.14, runnable doc examples, minimum coverage. |
| D23 | A **migration guide** from simple-salesforce; no compatibility shim. |
| D24 | Release order: 0.1 core → 0.2 files, actions, integrations → 0.3 Bulk, Tooling, GraphQL, Composite → 0.4 UI API, Reports, Metadata → 0.5 Pub/Sub → 1.0. |
| D25 | The repo starts with only this file. |
| D26 | Org automation: generic calls to invocable actions (standard, Flow, Apex) and Apex REST; `esf generate actions` produces typed stubs from an org; anonymous Apex lives under Tooling and only runs when called explicitly. Triggers cannot be called directly (they fire on DML). |
| D27 | **Usage tracking:** read `Sforce-Limit-Info` on every response; counters by operation, object and caller; `sf.usage.snapshot()`; budget threshold events; optional guard that pauses low-priority callers; `/limits` polling opt-in. |
| D28 | **Lessons from simple-salesforce's issue tracker** (482 issues analysed, 2026-11-09): every design-caused bug class there gets a guard test here, and the decisions below cite the issue numbers. Summary in §15.1. |
| D29 | **One way per task.** A new public method or parameter must either do something no existing public API can do, or replace code users would otherwise write by hand and often get wrong. Aliases, shortcut parameters and format converters are rejected. `sf.request` is the one deliberate overlap (the escape hatch, §9.5). Positional values in SOQL methods (§9.2) and `esf.soql` share one quoting implementation; docs teach the first and use `esf.soql` only for building a query in pieces. Rejected under this rule (2026-11-09): `QueryResult.flat()` (use `esf.flatten`), `to_pandas()`/`to_polars()` and a `[dataframe]` extra (use `pandas.DataFrame([esf.flatten(r) for r in page])`), `esf shell` (use `python -i`), `Auth.from_env()` (infers the flow from which variables are set, the §1.1 problem), an `sf` CLI-backed auth (depends on an external binary). Deferred until users ask: `sf.with_options(timeout=, retry=, headers=)`. |
| D30 | **Request headers are keyword arguments**, on the method (per call) or the client (per integration) whose Salesforce resource documents the header. There are no generic header objects, because they would be a second way to set the same header (D29) and pyright could not tell which headers a method accepts. Each header kwarg cites the Salesforce page that names the resources accepting it; a header that is not documented for a resource is not sent there. Response headers are parsed into typed results (usage, warnings), never returned raw. Rejected (2026-11-09): typed header objects (`headers=[AutoAssign(...)]`), `If-Match`/`If-None-Match` ETags (Account rows only), `x-sfdc-packageversion-*` (niche; use `sf.request`). Bulk-only headers arrive with Bulk (0.3). Details: ADR 0012. |
| D31 | **Events are pulled through one async iterator.** Pub/Sub subscriptions are `async with sf.pubsub.subscribe(...) as events: async for ev in events` (a blocking iterator in the sync twin); iterating is the flow control. Change Data Capture (CDC) bitmaps are decoded to field names, gap and overflow events are typed, delivery is at least once through an optional `ReplayStore`, and Pub/Sub error codes map onto §8. Rejected under D29: a callback or decorator registry (`@sf.on(...)`), `before_request`/`on_refresh` hooks (observers and `TokenStore` cover them), and `sf.pubsub.publish` (use `sf.platform_events`, §9.10). `ManagedSubscribe` waits for GA and then becomes one parameter. gRPC is a second network path beside the httpx transport, with one module that owns it (§5.1). The Salesforce `.proto` file is vendored (CC0-1.0). Outbound Messaging receivers are unscheduled. ADR 0013. |

### 2.2 Resolved open questions

| # | Question | Resolution |
|---|---|---|
| O1 | Docs tool | **Zensical** with its native `zensical.toml`. No dual-compatibility with Material for MkDocs (Material is in maintenance, EOL 5 May 2027). |
| O2 | Lowest Python | **3.10**. Test 3.10–3.14. |
| O3 | Licence | **Apache-2.0**, `Copyright 2026 Utsav Singh`. |
| O4 | Publishing | **PyPI trusted publishing** from GitHub Actions on version tags. |
| O5 | Sync twin | **Generated from async code with unasync** at build time; CI fails if the generated code is stale. |
| O6 | Record type | **Plain `dict` by default**; optional Pydantic models via `model=`. |
| O7 | Dependencies | Core depends on **httpx only**. pydantic, orjson, h2, cryptography/pyjwt, grpcio, fastavro, opentelemetry, fastapi, flask are extras. |
| O8 | API version | **Pinned per library release** (v68.0 at design time), overridable per client. Never "latest" by default. |
| O9 | Query cursor for frontends | Opaque string, validated against the shape of a real query locator path, with optional HMAC signing using an app-supplied key. |
| O10 | Write retries | See §7.4. POST create retried only when the request provably never left the client. PATCH/PUT/GET/DELETE retried on 5xx and timeouts. Row-lock errors retried for every method. |
| O11 | Multi-pod refresh | `TokenStore` MAY provide `lock(key)` and compare-and-swap `save`. In-process single-flight is the default. |
| O12 | Describe cache | In-memory TTL by default, plus a `Cache` protocol for shared caches (e.g. Redis). |
| O13 | Docs versions on 0.x | Two copies: `latest` (last release) and `dev` (main). Per-version copies from 1.0. |
| O14 | Maintainers | **Utsav Singh, sole maintainer.** No co-maintainer is planned. Single-maintainer risk is reduced by automation instead: fully scripted releases, CI that catches regressions, docs for every process, and `AGENTS.md` so AI agents can do most maintenance work. |
| O15 | Out of scope | Data Cloud, Einstein, CometD, Event Monitoring log files. |

---

## 3. Repository layout

```
even-simpler-salesforce/
├── pyproject.toml               # metadata, version, extras, tool config (ruff, pyright, pytest, coverage; towncrier lives in towncrier.toml)
├── uv.lock
├── README.md                    # pitch, install, 10-line example, links
├── CHANGELOG.md                 # generated by towncrier at release time
├── changelog.d/                 # <issue>.<type>.md fragments
│   └── template.md.jinja
├── LICENSE                      # Apache-2.0, unmodified text
├── NOTICE                       # "even-simpler-salesforce\nCopyright 2026 Utsav Singh"
├── CONTRIBUTING.md  SECURITY.md  CODE_OF_CONDUCT.md
├── AGENTS.md                    # rules for AI agents (see §18.4)
├── CLAUDE.md                    # points to AGENTS.md
├── noxfile.py                   # sessions: tests (3.10–3.14), lint, types, docs, unasync-check, bench
├── zensical.toml                # docs site config
│
├── src/even_simpler_salesforce/
│   ├── __init__.py              # public exports; __all__ IS the SemVer surface
│   ├── py.typed
│   ├── _version.py
│   ├── _async/                  # hand-written async I/O code (source of truth)
│   │   ├── client.py            # AsyncSalesforce
│   │   ├── transport.py         # request pipeline
│   │   ├── auth_runtime.py      # single-flight refresh, store interaction
│   │   └── api/                 # one module per area (query, sobject, collections, ...)
│   ├── _sync/                   # GENERATED by scripts/unasync.py — never edit by hand
│   ├── auth/                    # Auth flow configs, Token, TokenStore protocol, InMemoryTokenStore
│   ├── core/                    # NO I/O: request builders, response/error parsing, SOQL formatting,
│   │                            # cursor encoding, limit-header parsing, chunking, CSV helpers
│   ├── errors.py                # full exception hierarchy
│   ├── models.py                # Token, QueryResult, RecordResult, BatchResult, UsageSnapshot, ...
│   ├── observability/           # Event types, observers, canonical summary, stdlib/structlog/otel adapters
│   ├── cache.py                 # Cache protocol + in-memory TTL cache
│   ├── _pubsub/                 # [pubsub]: vendored pubsub_api.proto (CC0-1.0) + generated *_pb2 stubs
│   ├── cli/                     # `esf` command (§9.18)
│   ├── testing/                 # FakeSalesforce, recorder, pytest plugin
│   └── contrib/
│       ├── fastapi.py
│       └── flask.py
│
├── tests/
│   ├── unit/                    # core/, errors, parsing — no network, no httpx mocking
│   ├── contract/                # respx + recorded Salesforce responses, both async and sync
│   ├── sandbox/                 # opt-in real-org tests (marker `sandbox`, skipped by default)
│   ├── docs/                    # executes every code block in docs/
│   ├── benchmarks/              # pytest-benchmark against recorded responses
│   └── fixtures/recorded/       # redacted JSON responses, one dir per API area
│
├── docs/                        # SOURCE pages (Markdown)
│   ├── index.md
│   ├── getting-started.md
│   ├── authentication/          # one page per flow + Salesforce setup steps + token stores
│   ├── guides/                  # records, query, collections, composite, graphql, bulk, files,
│   │                            # actions, apex, tooling, ui-api, reports, events, metadata, pubsub
│   ├── errors.md
│   ├── usage-and-budgets.md
│   ├── logging.md
│   ├── performance.md
│   ├── integrations/            # fastapi.md, flask.md
│   ├── testing.md
│   ├── migrating-from-simple-salesforce.md
│   ├── reference/               # API reference (generated from docstrings, see §16)
│   ├── changelog.md             # includes CHANGELOG.md
│   └── adr/                     # architecture decision records (seeded from §2)
│
├── site/                        # BUILD OUTPUT of `zensical build`; gitignored
│
├── scripts/
│   ├── unasync.py               # generates _sync/ from _async/
│   ├── pubsub_proto.py          # regenerates _pubsub/ stubs; --check fails on drift from the pinned SHA
│   └── release.py               # bump version, compile changelog, tag
└── .github/
    ├── workflows/               # ci.yml, docs.yml, release.yml, changelog-check.yml
    ├── ISSUE_TEMPLATE/
    ├── pull_request_template.md
    └── dependabot.yml
```

Rules:

- `core/`, `auth/` (config classes), `errors.py`, `models.py` MUST NOT perform I/O. They are shared by async and sync code unchanged.
- `_async/` is the only place that awaits. `_sync/` is generated from it (§5.4).
- Anything not exported from `even_simpler_salesforce/__init__.py` (or a documented public submodule such as `even_simpler_salesforce.testing`, `.contrib.fastapi`) is private, even without a leading underscore.

---

## 4. Public interface at a glance

```python
import even_simpler_salesforce as esf

# --- connect -----------------------------------------------------------------
auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com",
    client_id="...", client_secret="...",
)
async with esf.AsyncSalesforce(auth) as sf:          # or: sf = esf.Salesforce(auth)  (sync)

    # --- records --------------------------------------------------------------
    lead = await sf.sobject("Lead").get("00Q...", fields=["Id", "Name", "Status"])
    new_id = await sf.sobject("Lead").create({"LastName": "Doe", "Company": "Acme"})
    await sf.sobject("Lead").update(new_id, {"Status": "Working"})
    await sf.sobject("Account").upsert("External_Id__c", "A-42", {"Name": "Acme"})

    # --- query ----------------------------------------------------------------
    soql = esf.soql("SELECT Id, Name FROM Lead WHERE Email = {} AND Status IN {}",
                    email, ["Open", "Working"])                 # safe quoting
    page = await sf.query(soql, page_size=200)                  # one page + cursor
    async for row in sf.query_iter(soql):                       # all rows, lazily
        ...

    # --- many records in one round trip --------------------------------------
    results = await sf.collections.update("Lead", rows, all_or_none=False)
    results.raise_for_errors()                                  # or inspect results.failed

    # --- org automation -------------------------------------------------------
    out = await sf.actions.flow("Assign_Lead", inputs={"leadId": new_id})
    await sf.apex.post("/orders/submit", json={"orderId": "..."})

    # --- SOAP-only operations, hidden ----------------------------------------
    conv = await sf.leads.convert(new_id, converted_status="Qualified")

    # --- usage ----------------------------------------------------------------
    print(sf.usage.snapshot())          # api calls used / max, top operations, projection
```

Per-user clients in multi-user apps share the parent's pool, caches and observers:

```python
base = esf.AsyncSalesforce(admin_auth, store=my_store)      # app-wide
user_sf = base.with_auth(esf.WebServerFlow(..., token_key=f"user:{user_id}"))
```

Top-level exports (the 0.1 SemVer surface, as implemented): `AsyncSalesforce`, `Salesforce`,
all `Auth` classes and `AuthStart`, `Token`, `AsyncTokenStore` / `TokenStore`,
`AsyncInMemoryTokenStore` / `InMemoryTokenStore`, `soql`, `soql_like`, `soql_identifier`,
`Cursor`, all exceptions, result models (`QueryResult`, `BatchResult`, `RecordResult`,
`UpsertResult`, `UsageSnapshot`, `Limit`), config (`Budget`, `RetryPolicy`, `Timeouts`,
`DescribeCacheConfig`), `Cache` / `MemoryCache`, `Event`, `Observer`, `capture`,
`call_context`, `EsfDeprecationWarning`, `__version__`. (`DuplicateRules` and `SalesforceWarning` are planned, not shipped: §9.1, §7.1 step 7.) 0.5 adds `PubSubEvent`, `ChangeHeader`,
`ChangeType`, `AsyncReplayStore` / `ReplayStore` and `AsyncInMemoryReplayStore` /
`InMemoryReplayStore` (§9.16). Naming rule: the async variant carries the
`Async` prefix, like `AsyncSalesforce` / `Salesforce`.

---

## 5. Architecture

### 5.1 Layers

```
 app code
   │
   ▼
 Area facades (sf.sobject, sf.query, sf.collections, sf.bulk, ...)   ← thin; build Request specs
   │
   ▼
 Client (AsyncSalesforce / Salesforce)                                ← owns config, auth runtime,
   │                                                                     caches, observers, usage
   ▼
 Transport pipeline:  auth → budget guard → send → decompress → parse limits
                      → classify → retry/refresh → emit events
   │
   ▼
 httpx.AsyncClient / httpx.Client (injectable)
```

- **Area facades** never touch httpx. They produce a `RequestSpec` (method, path, params, body, headers, `idempotent`, `operation`, `sobject`, `priority`) using pure builders from `core/`, hand it to the client, and parse the `Response` with pure parsers from `core/`.
- **The transport pipeline is the only path to the network.** Every feature (refresh, retries, usage, logging, budget) lives there once, so it applies to every call. This is the fix for "refresh only covers three methods".
- **The one exception is Pub/Sub (D31).** gRPC streams cannot go through httpx. `_async/pubsub_transport.py` is the gRPC equivalent of the transport, and it and the `GrpcStream` adapter in `_compat` (§5.4) are the only modules that import `grpc`. It reuses the client's auth runtime (single-flight refresh), `RetryPolicy` backoff and observers. All decisions (flow-control accounting, replay rules, error classification, Avro and bitmap decoding) are pure functions in `core/pubsub.py`. The §15.1 AST guard is extended to cover this: only the transport imports httpx, and only `pubsub_transport.py` and `_compat` import grpc.
- **Auth runtime** sits between the client and the `Auth` config: it holds the current in-memory token per `token_key`, runs single-flight refresh and talks to the `TokenStore`.

### 5.2 Client lifecycle

- `AsyncSalesforce(auth, *, api_version="68.0", http_client=None, store=None, cache=None, observers=(), budget=None, retry=None, timeout=None, max_concurrency=None, user_agent=None, json=None, error_tags=None, cursor_key=None, instance_url=None, describe_cache=None, log_soql="hash", http2=False, client_name=None, pubsub_endpoint=None)` (`None` means the default config object). `default_namespace=` and `update_mru=` (§10.2) are planned, not shipped.
- If `http_client` is not given, the client creates one (`httpx.AsyncClient` with HTTP/1.1, pool limits, timeouts, `Accept-Encoding: gzip`) and **owns** it: `aclose()` / `async with` closes it. An injected client is never closed by the library.
- `with_auth(auth)` returns a child client sharing transport, caches, observers, usage counters and budget, with its own auth runtime. Children are cheap (no new pool) and SHOULD NOT be closed individually; closing the parent closes the pool.
- Clients are safe to share across tasks (async) or threads (sync).
- Instance URL is learned from the token response (`instance_url`) and MAY be overridden.

### 5.3 Configuration objects

All configuration is explicit dataclasses (frozen, slotted), never `**kwargs` guessing:
`Timeouts(connect=5, read=60, write=60, pool=5)`, `RetryPolicy(...)` (§7.4), `Budget(...)` (§10),
`DescribeCacheConfig(ttl=...)`. Environment variables are read **only** by `ESF_DEBUG` and the CLI;
library behaviour never changes silently from the environment.

### 5.4 Sync generation (unasync)

- Source of truth: `src/even_simpler_salesforce/_async/`. Generated: `_sync/`.
- `scripts/unasync.py` applies token replacements: `AsyncSalesforce→Salesforce`, `httpx.AsyncClient→httpx.Client`, `async def→def`, `await →`, `async with→with`, `async for→for`, `__aenter__/__aexit__→__enter__/__exit__`, `aclose→close`, `AsyncIterator→Iterator`, plus project-specific swaps through a small compatibility module:

| `_async/_compat.py` | `_sync/_compat.py` (hand-written, not generated) |
|---|---|
| `asyncio.Lock` | `threading.Lock` |
| `asyncio.Semaphore` | `threading.BoundedSemaphore` |
| `asyncio.sleep` | `time.sleep` |
| `gather_settled(n, funcs)` (semaphore + `asyncio.gather(return_exceptions=True)`; always waits for every call) | `ThreadPoolExecutor`, each task in a copy of the caller's context |
| `Periodic` (background task, for the limits poller) | daemon thread |
| `GrpcStream` over `grpc.aio` (`write()` / async `read()`) | `GrpcStream` over sync `grpc`: a queue-fed request iterator and a blocking response iterator (D31) |

Also replaced: `aread→read`, `StopAsyncIteration→StopIteration`, `asynccontextmanager→contextmanager`,
`AbstractAsyncContextManager→AbstractContextManager`, `store_lock_async→store_lock_sync`, and a
generic `Async<Name>→<Name>` rule (`AsyncTokenStore→TokenStore`, ...).

- CI runs the generator and fails if `git diff --exit-code src/even_simpler_salesforce/_sync` is non-empty.
- Contract tests are parameterised over both clients so behaviour stays identical.

### 5.5 Dependencies

| Extra | Pulls in | Enables |
|---|---|---|
| *(core)* | `httpx>=0.27` | everything not listed below |
| `[pydantic]` | `pydantic>=2` | `model=` typed results, generated action stubs as models |
| `[speed]` | `orjson` | faster JSON |
| `[http2]` | `h2` (via `httpx[http2]`) | HTTP/2 |
| `[jwt]` | `cryptography`, `pyjwt` | `JwtBearer` |
| `[pubsub]` | `grpcio`, `protobuf`, `fastavro` | Pub/Sub API (§9.16) |
| `[otel]` | `opentelemetry-api` | spans and metrics observer |
| `[structlog]` | `structlog` | structlog observer |
| `[fastapi]` | `fastapi` | `contrib.fastapi` |
| `[flask]` | `flask` | `contrib.flask` |
| `[testing]` | `respx`, `pytest` | `testing` module, pytest plugin |
| `[migrate]` | `libcst` | `esf migrate` (§9.19) |
| `[all]` | all of the above | |

The Metadata API (0.4) uses only the stdlib (`zipfile`, `xml.etree`) and needs no extra.

`[pubsub]` notes (checked on PyPI 2026-11-09):

- **Stubs are generated at build time, not install time.** `_pubsub/` ships the generated
  `*_pb2.py` / `*_pb2_grpc.py`, so users never need `grpcio-tools`. Stubs from `grpcio-tools`
  1.85 require `protobuf>=7.35` at runtime, so the `protobuf` minimum in `[pubsub]` MUST equal
  the version the stubs were generated with. `scripts/pubsub_proto.py --check` enforces this in
  CI.
- **fastavro 1.13 dropped Python 3.10.** On 3.10, `[pubsub]` resolves fastavro 1.12.x, the
  last line with 3.10 wheels. Python 3.10 reached end of life on 2026-10-01, so whether ESF keeps
  3.10 (O2) needs a separate decision. The `min_deps` session pins the lowest supported
  `grpcio`, `protobuf` and `fastavro`.
- **Types:** pyright strict uses `types-grpcio` and `types-protobuf` (dev group), plus `.pyi`
  files generated with `--pyi_out`.

---

## 6. Authentication and tokens

### 6.1 Flow classes

All are frozen config objects implementing the `Auth` protocol. They hold no tokens themselves.

| Class | Use | Inputs | Refresh behaviour |
|---|---|---|---|
| `WebServerFlow` | A human user logged in via the browser; app holds their refresh token | `domain`, `client_id`, `client_secret` (optional for PKCE public clients), `redirect_uri`, `scopes`, `token_key` (**required**: one per Salesforce identity), `refresh_ahead=None`, `expiry="unknown"\|"introspect"` | Refresh token grant |
| `ClientCredentials` | The app acting as its integration user | `domain` (**must be My Domain**), `client_id`, `client_secret` | Re-request a token (no refresh token issued) |
| `JwtBearer` | Server-to-server with a certificate | `domain`, `client_id`, `username`, `private_key`, `private_key_passphrase=None` (encrypted PEM; decrypted locally, never logged), `audience` (`login`/`test`/custom) | Re-sign and re-request |
| `StaticToken` | Tests, scripts, tokens obtained elsewhere | `access_token`, `instance_url` | None: a 401 raises `SessionExpired` |
| `TokenExchange` *(later)* | RFC 8693 exchange | | |
| `DeviceFlow` *(later)* | CLIs and devices | | |

`domain` accepts `"login"`, `"test"`, a full `https://<mydomain>.my.salesforce.com` URL, or an Experience Cloud site URL with a path prefix (`https://<site>.my.site.com/<prefix>`; no query, fragment, `.`/`..` or empty segments). The OAuth endpoints (authorize, token, revoke, introspect) are built under that base; `userinfo` and API calls use the token's `instance_url`. For a site base, the JWT `aud` is the site URL. `ClientCredentials` rejects a site path (documented on My Domain only).

**WebServerFlow helpers** (pure + one I/O call each):

```python
start = flow.authorization_url(state="...")       # returns AuthStart(url, state, code_verifier)
token = await sf.auth.exchange_code(flow, code, code_verifier=start.code_verifier)  # saves to store
await sf.auth.revoke(token_key)                  # POST /services/oauth2/revoke, clears store
info  = await sf.auth.userinfo()                 # /services/oauth2/userinfo
intro = await sf.auth.introspect(token_key)      # /services/oauth2/introspect
token = await sf.auth.refresh()                  # forced refresh (single-flight); StaticToken → SessionExpired
```

PKCE (S256) is on by default for `WebServerFlow`.

### 6.2 `Token`

```python
@dataclass(frozen=True, slots=True)
class Token:
    access_token: str
    instance_url: str
    issued_at: datetime                 # from Salesforce's `issued_at` (epoch ms), UTC
    refresh_token: str | None = None
    id_url: str | None = None           # `id` field: https://login.../id/<orgId>/<userId>
    org_id: str | None = None           # parsed from id_url
    user_id: str | None = None          # parsed from id_url
    scope: frozenset[str] = frozenset()
    signature: str | None = None
    expires_at: datetime | None = None  # only if learned (refresh_ahead or introspection)
    version: int = 0                    # store-managed, for compare-and-swap
```

`repr()` MUST redact `access_token` and `refresh_token` (show last 4 characters at most).

### 6.3 `TokenStore` protocol

Implemented as `AsyncTokenStore` (below) and the sync `TokenStore` with the same members.

```python
class AsyncTokenStore(Protocol):
    async def load(self, key: str) -> Token | None: ...
    async def save(self, key: str, token: Token, *, expected_version: int | None) -> Token:
        """Persist `token`. If expected_version is not None and the stored version differs,
        raise TokenConflict. Returns the stored token with its new version."""
    async def delete(self, key: str) -> None: ...
    def lock(self, key: str) -> AbstractAsyncContextManager[None]:
        """Optional cross-process lock. Default implementation (in the base class) is a
        no-op; in-process single-flight is always applied regardless."""
```

- The sync twin has the same protocol without `async`.
- `InMemoryTokenStore` ships in core: dict + version counter + per-key lock. Lost on restart, which is fine for `ClientCredentials` and `JwtBearer`.
- **Contract for durable stores** (documented in `docs/authentication/token-stores.md`, enforced by a reusable test suite `esf.testing.token_store_contract`):
  1. One record per **Salesforce identity**, not per app user. Two app users that share a Salesforce connection MUST share one record; copying refresh tokens into several records breaks under rotation.
  2. `save` with `expected_version` MUST be atomic (compare-and-swap or row lock).
  3. Refresh tokens SHOULD be encrypted at rest.
  4. `delete` MUST remove both tokens.
- Docs ship two example stores (not in the package): SQLAlchemy (Postgres `SELECT … FOR UPDATE` / version column) and Redis (`SET NX` lock + Lua CAS).

### 6.4 Refresh algorithm

Salesforce facts this design rests on (confirmed against the official *OAuth 2.0 Refresh Token
Flow* help page, 2026-11-09; see Appendix A):

- The token response has `issued_at` but **no `expires_in`**. Access-token lifetime is the app's session timeout, which the client is not told.
- With **refresh-token rotation** on, each refresh returns a new refresh token and invalidates the previous one. **Reusing a rotated-out token invalidates the current refresh token and its access tokens**; the user must log in again.
- **Org-observed** (Developer Edition, API v68.0, 2026-11-12; the help page and the rotation article https://help.salesforce.com/s/articleView?id=005316711&language=en_US&type=1 are silent): with rotation on, **every refresh revokes all earlier access tokens** of the grant (11 sequential refreshes: access tokens 1–10 → 401, 11 → 200). So a token one process has just refreshed can be revoked by another process's refresh before it is used. Same org: reusing a rotated-out refresh token was accepted 8 times before the whole chain (and its access tokens) was revoked; the article says the revocation is immediate. The client never reuses one, so this does not change the design.
- Simultaneous refreshes with the same refresh token fail intermittently with **"Token request is already being processed"**. Salesforce advises caching and reusing tokens, and retrying when this happens.

Algorithm for a call on a client with `token_key = K`:

```
1. tok = memory[K] or await store.load(K) (or initial acquisition for ClientCredentials/JwtBearer)
2. If refresh_ahead is set and tok.issued_at + refresh_ahead <= now  → go to REFRESH(tok)
   If expiry == "introspect" and tok.expires_at known and <= now + skew → go to REFRESH(tok)
3. Send request with tok.access_token.
4. If response is 401 with errorCode INVALID_SESSION_ID (or OAuth `invalid_token`):
       new = REFRESH(tok); resend once with new.
5. If that resend gets a 401 too (and step 4 allowed the resend, ADR 0001):
       under store.lock(K) re-load the store; if it holds a token other than the rejected one
       (another process refreshed after us, superseding ours), adopt it and resend. At most
       MAX_ADOPTS = 2 adopts per call, never a second token POST. The last adopted resend is
       sent while holding store.lock(K) (taken inside the concurrency gate, without the
       in-process single-flight lock, so the lock order stays gate → single-flight → store),
       so no other refresh can revoke its token in flight. Each adopt emits `sf.retry`
       with reason `token_superseded`. Otherwise raise SessionExpired; if the store was still
       moving on, its message and hint say the token was superseded by another refresh
       (not a scope / IP / session-policy problem).

REFRESH(stale):
  a. In-process single-flight on K: if a refresh for K is already running, await its result.
  b. async with store.lock(K):
       current = await store.load(K)
       if current and current.access_token != stale.access_token:
           memory[K] = current; return current          # someone else already refreshed
       resp = POST /services/oauth2/token (grant per flow)
         - connect error before the request was sent  → retry with backoff (safe)
         - timeout / connection reset after send       → DO NOT resend. Sleep briefly, re-load
                                                         the store; if it changed, use it; else
                                                         raise RefreshFailed(ambiguous=True)
         - 400 "Token request is already being processed" → backoff, re-load store, retry
                                                         (bounded, default 3 attempts)
         - 400 invalid_grant (expired/revoked/rotated-out) → raise ReauthorizationRequired,
                                                         emit `sf.auth.reauth_required`
         - other 4xx/5xx → typed AuthError
       new = merge(resp, stale)   # keep old refresh_token if the response has none;
                                  # TokenIdentityChanged if org id or user id differs
       saved = await store.save(K, new, expected_version=current.version if current else None)
         - TokenConflict → re-load and use the stored token (another process won)
       memory[K] = saved; emit `sf.auth.refreshed`; return saved
```

Rules:
- The new refresh token MUST be saved **before** the new access token is used for any request.
- The refresh POST itself is never retried blindly. This is a deliberate exception to the general retry policy.
- `ClientCredentials` / `JwtBearer` have no refresh token; "refresh" is a fresh token request, and blind retries are safe.
- Default skew for `expires_at` checks: 60 s.
- Identity check: every token the client adopts in place of a previous one (the refresh response, a store re-read after a concurrent refresh, a timeout, a busy answer or a `TokenConflict`) MUST have the same org id and user id as the previous token. Otherwise raise `TokenIdentityChanged` (an `AuthError`; no token in the message) and don't use it. Ids missing on either side aren't compared; 15-character ids compare with the first 15 characters of an 18-character id. The refresh-token, JWT bearer and client-credentials responses all carry `id` (help pages in Appendix A).

### 6.5 Proactive refresh

- `refresh_ahead=timedelta(...)`: the app states its session timeout (minus a margin). Refresh happens when `issued_at + refresh_ahead` has passed.
- `expiry="introspect"`: after each token acquisition, the client calls `/services/oauth2/introspect` once, and stores `exp` in `expires_at` if present. ⚠️ **VERIFY** that Salesforce returns `exp`; the official page defers response fields to RFC 7662, where `exp` is optional. If absent, the client falls back to 401-driven refresh and logs once at INFO.
- Default (`expiry="unknown"`, no `refresh_ahead`): 401-driven only. This is always correct because admins can kill sessions at any time.

---

## 7. Transport

### 7.1 Request pipeline (in order)

**One path, one version.** Every request (REST, OAuth, and later Bulk, SOAP, Metadata, Files) goes
through the transport, so refresh, timeouts, proxies and logging can't be skipped by a new API area
(simple-salesforce #619, #686, #91). Every URL takes its API version from the client's pinned
`api_version`, including the SOAP endpoint used for convert/merge (D14); no version is ever
hard-coded (#506, #78, #268). Both rules are enforced by an AST test (`tests/unit/test_architecture.py`).


1. **Build**: absolute URL from `instance_url` + `/services/data/v{api_version}/...` unless the spec says otherwise (OAuth, Apex REST `/services/apexrest`, SOAP `/services/Soap/u/{v}`, Pub/Sub host).
2. **Auth**: attach `Authorization: Bearer <token>`.
3. **Budget guard** (§10.4): may raise `BudgetExceeded` for low-priority calls.
4. **Concurrency gate**: optional `max_concurrency` semaphore per client family.
5. **Compression**: always send `Accept-Encoding: gzip`. Gzip request bodies over 64 KiB (`Content-Encoding: gzip`) for Bulk ingest, Collections and Composite.
6. **Send** via httpx.
7. **Parse `Sforce-Limit-Info`** (`api-usage=used/max`, and `api-bursts=used/max` when present) into usage counters. Parse a `Warning` response header (`<code> - "<text>"`; code `299` = deprecated API version) and emit `warnings.warn(text, esf.SalesforceWarning)` **once per distinct text per client family** (**planned, not shipped** (spec-vs-code check, 2026-11-13): the client doesn't read `Warning` today), so a deprecation notice is not lost or repeated on every call. `SalesforceWarning` subclasses `UserWarning`; users silence or escalate it with the standard `warnings` filters.
8. **Classify** the response (§8.2) into success or a typed error.
9. **Refresh** on `INVALID_SESSION_ID` (§6.4).
10. **Retry** per policy (§7.4).
11. **Emit** one `sf.request` event (§11) with the final outcome.

### 7.2 Timeouts and pool

Defaults: connect 5 s, read 60 s, write 60 s, pool 5 s. Pool: `max_connections=50`,
`max_keepalive_connections=20`, keep-alive 30 s. Bulk result downloads and file transfers use a
streaming read with read-timeout per chunk. All overridable via `Timeouts` / injected client.

### 7.3 JSON

Use `orjson` when the `[speed]` extra is installed, else stdlib `json`. Results are plain
`dict`/`list` (never `OrderedDict`). Datetimes stay as Salesforce strings unless `model=` is used.

Both backends MUST behave identically (implemented in `core/jsonlib.py`):

- **NaN and ±Infinity are rejected** (`float` or `Decimal`, at any depth) with a `ValueError`
  before anything is sent. orjson would send `null` and silently blank the field; the stdlib would
  send `NaN`, which is not JSON (simple-salesforce #442, #309).
- **`Decimal` is encoded as a JSON number** with its exact digits, never via `float` and never as a
  string, so currency/percent values keep their precision on the way in (#135).
- **Reading numbers as `Decimal`** is opt-in: pass `json=` a codec whose `loads` uses
  `parse_float=decimal.Decimal` (stdlib). orjson can't parse to `Decimal`, so the default stays
  `float`; docs show the codec. With `model=`, Pydantic `Decimal` fields are the recommended route.

### 7.4 Retry policy

`RetryPolicy(max_attempts=3, base_delay=0.25, max_delay=8.0, jitter=True, respect_retry_after=True)`.

| Situation | GET / HEAD reads | PATCH / PUT / DELETE, upsert, code-running GETs | POST create & other POSTs | Refresh POST |
|---|---|---|---|---|
| Connect error (request never sent) | retry | retry | retry | retry |
| Timeout / reset after send | retry | **no** unless `idempotent=True` (ADR 0001) | **no** (ambiguous) unless `idempotent=True` | **no** (§6.4) |
| 5xx, `SERVER_UNAVAILABLE` | retry | **no** unless `idempotent=True` (ADR 0001) | **no** unless `idempotent=True` | n/a |
| 429 / `REQUEST_LIMIT_EXCEEDED` with `Retry-After` | retry after header | retry after header | retry after header | n/a |
| `UNABLE_TO_LOCK_ROW` | retry | retry | retry (the write was rejected, so safe) | n/a |
| 4xx other | no | no | no | no |

- **Only reads are resent after a request may have reached Salesforce** (`docs/adr/0001-retry-safety.md`):
  a write that timed out may have committed, and a resend re-runs triggers/flows or turns success
  into a false `Conflict`/`NotFound`. GET/HEAD count as reads except Apex REST and
  `tooling/executeAnonymous` (they run code); `dml=True` specs never do. Reads sent as POST set
  `idempotent=True` explicitly (query by POST, `parameterizedSearch`, Collections retrieve,
  composite with only GETs, Bulk job status polls). Upsert by external id is **not** idempotent here.
- Each retry is recorded in the final `sf.request` event (`retries`, `retry_reasons`).
- Exponential backoff with full jitter, capped by `max_delay`; `Retry-After` wins when present.

---

## 8. Errors

### 8.1 Shape

Every error raised by the library is a subclass of `SalesforceError`:

```python
class SalesforceError(Exception):
    message: str                         # human-readable, from Salesforce when available
    error_code: str | None               # Salesforce errorCode / OAuth error, e.g. "INVALID_FIELD"
    fields: tuple[str, ...]              # affected fields when Salesforce reports them
    status: int | None                   # HTTP status
    operation: str | None                # e.g. "sobject.update", "query", "bulk.ingest"
    sobject: str | None
    method: str | None
    path: str | None                     # path with ids replaced by ":id"
    request_id: str | None               # Salesforce request id header if present
    retryable: bool
    errors: tuple[ErrorDetail, ...]      # all errors when Salesforce returned several
    raw: bytes | str | None              # original body, for debugging (never logged by default)
```

`ErrorDetail(error_code, message, fields, extended)` holds each entry of a multi-error body.

### 8.2 Hierarchy

```
SalesforceError
├── TransportError                       (no usable HTTP response)
│   ├── ConnectError
│   └── RequestTimeout
├── AuthError
│   ├── SessionExpired                   (401 that survived one refresh, or StaticToken)
│   ├── ReauthorizationRequired          (invalid_grant: refresh token expired/revoked/rotated out)
│   ├── RefreshFailed                    (.ambiguous=True when the outcome is unknown)
│   ├── TokenConflict                    (store CAS failure; normally handled internally)
│   ├── TokenIdentityChanged             (new or re-read token has another org id or user id)
│   └── OAuthError                       (other OAuth endpoint errors; .error, .error_description)
├── RequestError                         (4xx caused by the request)
│   ├── MalformedQuery                   MALFORMED_QUERY, INVALID_QUERY_LOCATOR
│   ├── InvalidField                     INVALID_FIELD, INVALID_FIELD_FOR_INSERT_UPDATE, ...
│   ├── InvalidType                      INVALID_TYPE, NOT_FOUND on sObject type
│   ├── NotFound                         404 / NOT_FOUND / ENTITY_IS_DELETED
│   ├── Forbidden                        INSUFFICIENT_ACCESS*, API_DISABLED_FOR_ORG, ...
│   ├── MultipleMatches                  300: external id matched several records (.matches)
│   ├── Conflict                         409 / 412 / 428
│   ├── Gone                             410: resource retired or removed
│   ├── RequestTooLarge                  414 / 431: URI or headers over 16,384 bytes
│   └── UnsupportedApiVersion            UNSUPPORTED_API_VERSION from Salesforce (error_code set), or raised by
│                                        the library (error_code="API_VERSION_TOO_OLD") when a feature needs a
│                                        newer api_version than configured
├── RecordError                          (DML rejected by the org)
│   ├── ValidationRuleFailed             FIELD_CUSTOM_VALIDATION_EXCEPTION (rules, Flow custom errors, addError())
│   ├── RequiredFieldMissing             REQUIRED_FIELD_MISSING
│   ├── DuplicateDetected                DUPLICATES_DETECTED (.duplicates when provided)
│   ├── InvalidReference                 INVALID_CROSS_REFERENCE_KEY, FIELD_INTEGRITY_EXCEPTION
│   ├── StringTooLong                    STRING_TOO_LONG
│   ├── InvalidPicklistValue             INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST
│   ├── RowLocked                        UNABLE_TO_LOCK_ROW (retryable)
│   ├── RecordSaveBlocked                CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY (official text: no permission to
│   │                                    create/update/activate; in practice also unhandled trigger exceptions — V5b)
│   ├── FlowFailed                       CANNOT_EXECUTE_FLOW_TRIGGER
│   └── StorageLimitExceeded             STORAGE_LIMIT_EXCEEDED
├── LimitError
│   ├── RequestLimitExceeded             REQUEST_LIMIT_EXCEEDED (HTTP 403; covers both the concurrent-request
│   │                                    and the rate limit), 429 if ever returned (.retry_after)
│   ├── BudgetExceeded                   raised by the library's own guard (§10.4)
│   └── PubSubLimitExceeded              Pub/Sub RESOURCE_EXHAUSTED codes (§9.16.5)
├── ServerError                          500, 502, 503, SERVER_UNAVAILABLE (502/503 retryable; 420 = Edge routing,
│                                        not retryable)
├── PartialFailure                       allOrNone=false batches; .results: BatchResult
├── BulkJobFailed                        (.job_id, .state, .error_message)
├── BulkJobTimeout                       wait() ran out of time; the job keeps running (.job_id, .state)
├── BulkJobNotComplete                   query results asked for before JobComplete (.job_id, .state)
├── ApexError                            APEX_ERROR / Apex REST errors (.status, .body)
├── GraphQLError                         (.errors from the GraphQL `errors` array)
├── PubSubError                          other Pub/Sub gRPC errors; error_code = trailer code, request_id = rpcId
│   └── ReplayUnavailable                stored/requested replay id rejected (replayid.corrupted, §9.16.4)
├── SoapFault                            parsed SOAP faults (.fault_code), mapped to the classes above when possible
└── UnexpectedResponse                   HTML pages (proxy, maintenance), non-JSON bodies, schema surprises
```

Unknown `errorCode` values map by HTTP status to the nearest parent (`RequestError`,
`RecordError` for DML endpoints, `ServerError`) and keep `error_code` set, so new Salesforce codes
never cause crashes. Every code string above appears in the official `StatusCode` list
(*Core Data Types Used in API Calls*), and the HTTP statuses match *Status Codes and Error
Responses* (V5, done). The official one-line descriptions for `FIELD_CUSTOM_VALIDATION_EXCEPTION`
and `CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY` don't describe how they show up in practice, so the
mapping of validation rules / trigger exceptions to those codes needs recorded sandbox responses (V5b).

### 8.3 Sources normalised to this shape

| Source | Body shape | Handling |
|---|---|---|
| REST resources | `[{"message","errorCode","fields"}]` | first entry → class; all → `errors` |
| OAuth endpoints | `{"error","error_description"}` | `OAuthError` / `ReauthorizationRequired` |
| Collections / Composite (allOrNone=false) | per-record `success`, `errors[]` | `BatchResult`; `PartialFailure` only via `raise_for_errors()` |
| Composite / Graph subrequests | `compositeResponse[].body` | each subresult classified; `CompositeResult.raise_for_errors()` |
| Bulk 2.0 | job `state=Failed`, `errorMessage`; failed-results CSV `sf__Error` | `BulkJobFailed`; failed rows parsed into `ErrorDetail` |
| SOAP | `<soapenv:Fault>` or `<errors><statusCode>` | mapped by status code to the same classes |
| GraphQL | 200 with `errors[]` | `GraphQLError` |
| Pub/Sub (gRPC) | status + `error-code` trailer + `rpcId` | table in §9.16.5 |
| Apex REST | whatever the class returns; `APEX_ERROR` on unhandled exceptions | `ApexError` |
| HTML / non-JSON | proxy or maintenance pages | `UnexpectedResponse` with a truncated `raw` |

### 8.4 Custom errors (opt-in)

Salesforce has no custom error codes: validation rules, Flow "Custom Error" elements and Apex
`addError()` all return `FIELD_CUSTOM_VALIDATION_EXCEPTION` with the message text. The library
supports a **tag convention** so orgs can give errors stable identities:

```python
class AddressRequired(esf.ValidationRuleFailed): ...

sf = esf.AsyncSalesforce(auth, error_tags={"ADDR_REQUIRED": AddressRequired})
# Org message: "[ADDR_REQUIRED] Street is required"
# Raises AddressRequired(message="Street is required", tag="ADDR_REQUIRED", ...)
```

- Pattern: `^\[(?P<tag>[A-Z0-9_]{2,64})\]\s*` at the start of the message.
- Registered classes MUST subclass the class the error would otherwise have been.
- Unmatched tags still populate `.tag` on the default class. Nothing changes for users who don't opt in.

### 8.5 Hints and docs links (0.2)

- `SalesforceError` gains `hint: str | None` and `docs_url: str | None`; both are appended to
  `str(error)` and survive pickling (§15.1).
- `docs_url` points to the troubleshooting page for the error class (§16), e.g.
  `.../errors/#invalidfield`.
- `hint` is computed only from data the client already has; building an error never makes an API
  call. `InvalidField`: closest field names from the **cached** describe of that sObject
  (`difflib`), e.g. `did you mean 'Email'?`; nothing when describe isn't cached. Others where the
  cause is common and fixed: `INVALID_SESSION_ID` after refresh (connected app policy),
  `API_DISABLED_FOR_ORG`, `REQUEST_LIMIT_EXCEEDED` (link to `sf.usage`), `MALFORMED_QUERY` with an
  unescaped quote (suggest values in SOQL methods).
- Hints never include record data or secrets (same redaction as messages).

### 8.6 What the library will not do

No evaluation of validation-rule formulas or trigger logic on the client (D13). Copies drift from
the org. An opt-in describe-based pre-check (types, lengths, required, restricted picklists) MAY
be added after 1.0 as `sf.sobject(...).check(data)`.

---

## 9. API areas

Every area exists on both clients with identical names. Signatures below are async; drop
`await`/`async` for sync. "Ships" refers to §19.

### 9.1 Records — `sf.sobject(name)` (0.1)

| Method | REST | Notes |
|---|---|---|
| `get(id, fields=None, model=None)` | `GET sobjects/{o}/{id}` | |
| `get_by_external_id(field, value, fields=None)` | `GET sobjects/{o}/{field}/{value}` | |
| `create(data) -> str` | `POST sobjects/{o}` | returns id |
| `update(id, data)` | `PATCH sobjects/{o}/{id}` | |
| `upsert(field, value, data) -> UpsertResult` | `PATCH sobjects/{o}/{field}/{value}` | `created: bool`, `id` |
| `delete(id)` | `DELETE sobjects/{o}/{id}` | no conditional headers (see below) |
| `describe(cached=True)` | `GET sobjects/{o}/describe` | uses describe cache (§12) |
| `metadata()` | `GET sobjects/{o}` | |
| `updated(start, end)` / `deleted(start, end)` | `GET sobjects/{o}/updated|deleted` | |
| `sf.describe_global(cached=True)` | `GET sobjects` | |

**External id values in paths** are percent-encoded as one path segment (`url_segment`: space →
`%20`, `/` → `%2F`, `+` → `%2B`; httpx keeps them as sent). `quote_plus` is never used (it turns
spaces into `+`, which Salesforce reads literally; simple-salesforce #801). A value ending in
`.json`, `.xml` or `.html` is read by Salesforce as a response format (#22; confirmed on a v68.0
org, V19: 404 `NOT_FOUND`, also with the dot as `%2E`; `.JSON`, `.csv`, `.htm`, `a.b` work). Format
suffixes are documented only up to API 57.0 (*REST Resources and Requests*). For values ending in
one of the three suffixes (matched case-insensitively), `get_by_external_id` runs
`SELECT Id FROM {o} WHERE {field} = {value}` and then `GET sobjects/{o}/{id}` (two calls), and
`upsert` sends one record through `PATCH composite/sobjects/{o}/{field}` with the same headers.
Return types and errors match the path: `NotFound` (`NOT_FOUND`, 404), `MultipleMatches` (300,
`.matches` rebuilt as record URLs; Collections reports `DUPLICATE_EXTERNAL_ID`), `InvalidField`
when `data` also sets `field` (raised locally, as the path upsert rejects it), other DML errors
classified as usual.

**Request headers (D30).** Keyword-only arguments; `None` means the header is not sent and
Salesforce's default applies.

| Kwarg | Methods | Header | Notes |
|---|---|---|---|
| `assignment_rule_id: str \| bool \| None` | `create`, `update`, `upsert` | `Sforce-Auto-Assign` | `True`/`False` → `TRUE`/`FALSE`; a string must be an AssignmentRule id (validated with `check_record_id`). Salesforce defaults to `TRUE` and applies it to Accounts, Cases and Leads, including ones written indirectly by triggers. |
| `allow_save: bool \| None` | `create`, `update`, `upsert` | `Sforce-Duplicate-Rule-Header` | API 52.0+. Sends `allowSave=…; includeRecordDetails=false; runAsCurrentUser=false`. See below. |
| `if_modified_since: datetime \| None` | `get` | `If-Modified-Since` | 304 handling as before. The describe cache sends it internally (§12). |
| `if_unmodified_since: datetime \| None` | `update` | `If-Unmodified-Since` | `Conflict` on 412. |

**Shipped:** `allow_save=` (spec-vs-code check, 2026-11-13). `core/records.duplicate_rule_header` already takes all
three flags, but only `allowSave` is exposed. **Planned, not shipped:** `esf.DuplicateRules`, a
frozen dataclass in `config.py` (no I/O) that would replace `allow_save=` as `duplicate_rules=`.
Because 0.5 ships `allow_save=`, the change then needs a deprecation (D29):

```python
@dataclass(frozen=True)
class DuplicateRules:
    allow_save: bool = False             # save despite a duplicate-rule alert
    include_record_details: bool = False # DuplicateDetected.duplicates carries the matched records' fields, not just ids
    run_as_current_user: bool = False    # match with the running user's sharing rules, not system sharing
```

All three fields are always sent (`allowSave=true; includeRecordDetails=false;
runAsCurrentUser=false`); the defaults equal Salesforce's documented defaults, so
`DuplicateRules()` behaves like sending no header.

**No conditional headers on delete.** The REST API Developer Guide (*Conditional Request
Headers*) says DELETE requests don't support them. An earlier 0.1 draft accepted
`delete(id, if_unmodified_since=)`; the docs suggested Salesforce would ignore the header and
delete anyway, which is worse than not offering it. It is removed (fix). There is no atomic "delete only if unchanged"
in REST; the records guide says so and shows a read-then-delete pattern with its race window. V25
(sandbox): Salesforce does not ignore the header, it rejects the DELETE (500 `UNKNOWN_EXCEPTION`
for a past date, 400 `BAD_HEADER` for a future one) and keeps the record.

**Client-wide headers** (§10.2): `client_name` (`Sforce-Call-Options`) ships.
`default_namespace` (`Sforce-Call-Options`) and `update_mru` (`Sforce-Mru`) are planned, not
shipped (spec-vs-code check, 2026-11-13).

### 9.2 Query and search (0.1)

```python
page = await sf.query(soql, page_size=2000, include_deleted=False, model=None) -> QueryResult
#   QueryResult(records: list[dict], total_size: int, done: bool, cursor: Cursor | None)
page2 = await sf.query_more(page.cursor)
async for rec in sf.query_iter(soql, page_size=2000): ...
n = await sf.count(soql_count)                    # SELECT COUNT() ...
res = await sf.search(sosl)                        # GET search?q=
res = await sf.parameterized_search(q, sobjects=[...], fields=[...])
```

- `include_deleted=True` uses `queryAll`.
- **Values in SOQL methods (D29):** every method that takes SOQL (`query`, `query_one`, `query_first`,
  `query_iter`, `count`, `explain`, later `tooling.query`, `bulk.query`) accepts positional values
  after the query: `sf.query("SELECT Id FROM Lead WHERE Email = {}", email)`. They are quoted by
  the same code as `esf.soql`, so the two can't behave differently. The number of `{}`
  placeholders must equal the number of values (`ValueError` otherwise), so a forgotten
  placeholder can't silently drop a filter. The template is formatted only
  when at least one value is given; without values the string is sent unchanged (no `{{` handling),
  so existing queries keep working. Named placeholders (`{name}`) and `{:like}` are only available
  through `esf.soql`, because keyword arguments are taken by options (`page_size=`, `model=`, ...).
  Docs teach this form and warn that an f-string (`f"... = '{email}'"`) looks similar but escapes
  nothing. `esf.soql` stays public for building a query in pieces.
- **`query_one(soql, *values, include_deleted=False, model=None)`:** returns exactly one row.
  Reads only the first page and decides from `totalSize`: 0 → `NotFound`, more than 1 →
  `MultipleMatches` (`.matches` = the `Id`s of the rows on that page). Both are raised by the
  client, so `status=None` and `operation="query"`. The SOQL is never rewritten (no added `LIMIT`).
- **`query_first(soql, *values, include_deleted=False, model=None)`:** returns the first row or
  `None`; never raises for "no rows". For "one if any" (newest record, any match), where
  `query_one` is for "must be exactly one". Reads only the first page; add `LIMIT 1` yourself to
  avoid fetching more rows than needed.
- **`explain(soql, *values) -> list[QueryPlan]`:** `GET query?explain=`; returns the query plans
  ordered as Salesforce sends them (cheapest first), each with `leading_operation`,
  `relative_cost`, `cardinality`, `sobject_cardinality`, `sobject_type`, `fields` and `notes`. The
  query itself is not run. ⚠️ **VERIFY** the response fields (V22).
- **`esf.flatten(row) -> dict`:** pure function in `core/records.py` for exporting rows to CSV,
  DataFrames or JSON. Drops `attributes` at every level; parent lookups become dotted keys
  (`"Owner.Name"`, `"Owner.Manager.Email"`); a null lookup stays as one key (`"Owner": None`), so
  read with `.get("Owner.Name")`; a child subquery becomes a list of flattened child rows (only the
  rows present, so use `expand_children=True` for all of them). Raises `TypeError` for model
  instances. The CLI's CSV output uses it and adds its own text formatting on top.
- `page_size` sets `Sforce-Query-Options: batchSize=N` (200–2000).
- Nested subquery pagination: `query_iter` follows child `nextRecordsUrl`s only when `expand_children=True`.

**Safe SOQL building** (`esf.soql`): `{}` placeholders, values quoted and escaped per SOQL rules:
strings (escape `\ ' " \n \r \t \b \f`), numbers, bools, `None→null`, `date`/`datetime` (ISO,
UTC `Z`), sequences → `('a','b')`. `esf.soql_like(value)` additionally escapes `%` and `_`.
Field and object names are **not** placeholders: pass them through `esf.soql_identifier(name)`,
which validates `^[A-Za-z][A-Za-z0-9_]*(__[a-z]+)?$` (dotted paths allowed).

**Cursor (O9):** `Cursor` is an opaque string safe to hand to a frontend.
- Encoding: base64url of the `nextRecordsUrl` path.
- Decoding validates the path against `^/services/data/v\d+\.\d/query(All)?/[0-9A-Za-z]{15,18}-\d+$`; anything else raises `MalformedQuery`. This prevents a client-supplied cursor from making the server call arbitrary URLs.
- Optional signing: `AsyncSalesforce(..., cursor_key=b"...")` appends an HMAC-SHA256 tag; tampered or foreign cursors are rejected.

### 9.3 Collections — `sf.collections` (0.1)

| Method | REST | Client behaviour |
|---|---|---|
| `create(rows, all_or_none=False)` | `POST composite/sobjects` | chunks of 200; each row needs `attributes.type` or `sobject=` arg |
| `update(sobject, rows, all_or_none=False)` | `PATCH composite/sobjects` | chunks of 200 |
| `upsert(sobject, field, rows, all_or_none=False)` | `PATCH composite/sobjects/{o}/{field}` | chunks of 200 |
| `delete(ids, all_or_none=False)` | `DELETE composite/sobjects?ids=` | chunks of 200 |
| `retrieve(sobject, ids, fields)` | `POST composite/sobjects/{o}` | chunks of 2000 |

- **Write headers** (D30): `create`, `update` and `upsert` take `assignment_rule_id=` and
  `duplicate_rules=` with the same types and serialisation as §9.1, sent on every chunk. The
  *Headers* pages don't limit these two headers to particular resources, and the Collections
  example responses show `DUPLICATES_DETECTED`, but no page says Collections honours them. V24
  (sandbox) confirmed both on create, update and upsert, so they ship as
  `assignment_rule_id=` and `allow_save=`, like §9.1 (`duplicate_rules=` is planned, §9.1). `delete` and `retrieve` take neither.
- Returns `BatchResult` (ordered as input): `.results: list[RecordResult(ok, id, created, errors)]`, `.ok`, `.failed`, `.raise_for_errors()` → `PartialFailure`.
- **Failures can't go unnoticed** (simple-salesforce #817, #544, #502, #442): every
  Collections `sf.request` event carries `records_failed`, so partial failures show up in logs and
  the `capture()` roll-up even if the app never calls `raise_for_errors()`. A "never inspected"
  warning from `__del__` was considered and rejected: it fires at unpredictable times (or never, on
  interpreter exit), is noisy in tests, and can't name the call site.
- Chunks run with bounded concurrency (`concurrency=4` default). With `all_or_none=True`, a single failing chunk raises a typed error; earlier chunks are **already committed**. This is documented prominently, and `all_or_none=True` with more than 200 rows logs a warning.

### 9.4 Composite family (0.3; `sf.request` in 0.1)

- `sf.composite(...)`: up to 25 subrequests, `all_or_none`, references (`@{ref.id}`). Builder:
  ```python
  c = sf.composite.builder(all_or_none=True)
  acc = c.create("Account", {"Name": "Acme"})
  c.create("Contact", {"LastName": "Doe", "AccountId": acc.ref("id")})
  result = await c.send()            # CompositeResult; .raise_for_errors()
  ```
- `sf.composite.graph(...)`: multiple graphs, up to 500 subrequests per graph; a failing graph rolls back only itself.
- `sf.composite.tree(sobject, records)`: up to 200 records across all trees, up to 5 record types, up to 5 levels deep.
- `sf.composite.batch(...)`: independent subrequests (25).
- Composite: up to 25 subrequests, **of which at most 5 may be sObject Collections or query operations**.
- Limits are enforced on the client with a clear `ValueError` before sending.

### 9.5 Raw requests — `sf.request` (0.1)

`await sf.request(method, path, *, params=None, json=None, content=None, headers=None, idempotent=None, operation="raw")`.
Goes through the full pipeline (auth, refresh, retries, errors, logging). `path` may be relative
to `/services/data/vXX.X/` or absolute starting with `/services/`. Absolute URLs to other hosts are
rejected. This is the escape hatch that replaces simple-salesforce's `restful()`.

### 9.6 GraphQL — `sf.graphql` (0.3)

`await sf.graphql(query, variables=None) -> dict` (the `data` member). `errors[]` → `GraphQLError`.
Helper `sf.graphql_iter(query, variables, path="uiapi.query.Lead")` follows `pageInfo.endCursor`.

### 9.7 Org automation — actions and Apex (0.2, D26)

| Call | REST |
|---|---|
| `sf.actions.standard(name, inputs)` | `POST actions/standard/{name}` |
| `sf.actions.flow(api_name, inputs)` | `POST actions/custom/flow/{api_name}` |
| `sf.actions.apex(class_name, inputs)` | `POST actions/custom/apex/{class_name}` |
| `sf.actions.list(kind)` / `sf.actions.describe(kind, name)` | `GET actions/...` |
| `sf.apex.get/post/put/patch/delete(path, ...)` | `/services/apexrest{path}` |
| `sf.tooling.execute_anonymous(code)` (0.3) | `GET tooling/executeAnonymous?anonymousBody=` |

- `inputs` may be a dict (one invocation) or a list of dicts (bulk invocation); results are returned in the same shape. Per-invocation `isSuccess=false` → typed error (single) or `BatchResult` (list).
- `execute_anonymous` returns `AnonymousResult(compiled, success, compile_problem, exception_message, exception_stack_trace, line, column, log)`. It **cannot** return values. It is never called implicitly.
- `execute_anonymous(code, capture_log=False)`: with `capture_log=True` the client makes the
  `System.debug` output visible. It creates a short-lived `TraceFlag` (with an `esf` `DebugLevel`)
  for the running user, runs the code, reads the new `ApexLog` body into `AnonymousResult.log`, and
  deletes the trace flag, also when the run fails. This costs several extra API calls, all counted
  in usage. Another session of the same user running Apex at the same moment can produce a log too;
  the client picks the log by operation and start time and documents the limitation.
  ⚠️ **VERIFY** the Tooling objects, required permissions and log-matching fields (V23).
- **Typed stubs:** `esf generate actions --org <alias|env> --include flow:Assign_Lead --include apex:* --out myapp/sf_actions.py` reads `describe` for each action and writes one class per action:
  ```python
  @dataclass(frozen=True)
  class AssignLead(esf.Action[AssignLeadResult]):
      __action__ = ("flow", "Assign_Lead")
      lead_id: str
      region: str | None = None
  result = await sf.run(AssignLead(lead_id="00Q...", region="North"))
  ```
  Field names are snake_case with the original API names kept in metadata. With `[pydantic]`, `--pydantic` emits models instead of dataclasses. The generated file has a header noting the org, API version and timestamp.
- Triggers cannot be invoked; they run as a side effect of DML. Docs say so.

### 9.8 Lead convert and merge (0.2, D14)

```python
res = await sf.leads.convert(lead_id, converted_status="Qualified", account_id=None,
                             contact_id=None, opportunity_name=None, do_not_create_opportunity=False,
                             owner_id=None, send_email=False, overwrite_lead_source=False)
# -> LeadConvertResult(account_id, contact_id, opportunity_id, lead_id)
results = await sf.leads.convert_many([LeadConvert(...), ...])   # up to 100 per call, chunked
await sf.merge(master_id, [dup1, dup2], sobject="Account")      # Lead, Contact, Account; ≤2 dups per call
```

Implementation: hand-built SOAP envelopes for the Partner API (`/services/Soap/u/{v}`) with the
OAuth access token as `SessionHeader.sessionId`, parsed with `xml.etree`. Faults map to §8 classes.
Verified in a Developer Edition org (API v68.0, V11/V35): `convertLead` and `merge` work with an
OAuth access token; the retirement covers `login()` only. Call-level errors and a bad session
are HTTP 500 faults (`INVALID_SESSION_ID`, never a 401); `LeadConvert` elements must keep the WSDL
order (out of order, the row fails with `UNKNOWN_EXCEPTION`).

### 9.9 Files (0.2)

```python
cv_id = await sf.files.upload(path_or_bytes_or_stream, title="Invoice", filename="inv.pdf",
                              link_to="001...", share_type="V", visibility="AllUsers")
async with sf.files.download(content_version_id) as stream:   # streamed, no full buffering
    async for chunk in stream: ...
data = await sf.files.download_bytes(content_version_id)       # convenience
await sf.files.link(content_document_id, record_id, share_type="V")
await sf.files.list_for(record_id)
```

Uploads always use a **multipart** `POST sobjects/ContentVersion` and stream the file, so the
official limit applies: up to **2 GB** per ContentVersion (500 MB for other blob objects; 500 MB
total per sObject Collections request). Non-multipart JSON uploads are limited to 37.5 MB of
base64 and are never used. Legacy `Attachment` download is supported via
`sf.files.download_attachment(id)`. Org-observed (API v68.0, V37–V39): uploads with
`Content-Length` and with chunked transfer encoding are both accepted; `VersionData` comes as
`application/octetstream` with `Content-Length` and no gzip; the link `FirstPublishLocationId`
creates on a record is `ShareType=V`, `Visibility=AllUsers`.

### 9.10 Platform events — `sf.platform_events` (0.2)

`await sf.platform_events.publish("Order_Event__e", {...})` and `publish_many(name, rows)` (via
Collections). Returns publish results; `EventBus` errors map to `RecordError` subclasses.

### 9.11 Bulk API 2.0 — `sf.bulk` (0.3)

```python
job = await sf.bulk.ingest("Lead", operation="upsert", external_id_field="Ext__c",
                           rows=iterable_of_dicts | csv_path | csv_bytes,
                           line_ending="LF", column_delimiter="COMMA")
await job.wait(poll=esf.Backoff(start=2, max=30), timeout=3600)
async for row in job.successful_results(): ...
async for row in job.failed_results(): ...          # includes sf__Error → ErrorDetail
async for row in job.unprocessed_records(): ...
await job.abort()

async for row in sf.bulk.query("SELECT Id, Name FROM Lead", parallel_downloads=4):
    ...                                              # streams result locators, CSV → dict

job = await sf.bulk.start_query("SELECT Id, Name FROM Lead")   # returns at once; job.id
job = await sf.bulk.query_job(job_id)                # open again, e.g. in a worker
await job.wait(); await job.abort(); await job.refresh()
async for row in job.rows(parallel_downloads=4): ...
async for page in job.pages(locator=saved, max_records=50_000):
    async for row in page.rows(): ...
    saved = page.next_locator                        # None after the last page
job = await sf.bulk.ingest_job(saved_ids)            # every id of a split upload
```

- **Job handles for long-running work**: `sf.bulk.query()` is `start_query()` + `wait()` +
  `rows()` in one call; the handle exists so a job can outlive the process that created it
  (submit in a web request, collect in a worker, resume after a restart), which `query()`
  can't do (D29). `rows()` and `pages()` need `JobComplete` (*Get Results for a Query Job*:
  "The job must have the state JobComplete") and raise `BulkJobNotComplete` otherwise; they
  never poll, because `wait()` already does. `Failed`/`Aborted` raise `BulkJobFailed`.
- **Reopen by id**: `query_job(id)` and `ingest_job(ids)` `GET jobs/{kind}/{id}` and keep the
  `apiVersion` of the job for later calls. `ingest_job` takes a sequence (a string is a
  `TypeError`), because an upload over 100 MB is several jobs; pass all of `job.ids`. An
  unknown or deleted job, and the id of the other job kind, are `NotFound` (404 `NOT_FOUND`,
  *Get Information About a Query Job*; other kind org-observed, V88). The status call works in
  any API version, so a client on another version can open the job (V88). Salesforce deletes jobs in a terminal state after 7 days
  and serves query results within 7 days of completion (*Developer Limits and Allocations
  Quick Reference*, Bulk API 2.0 "Batch and job lifespan", "Results lifespan").
- **Resumable pages**: `pages()` downloads each page (`?locator=&maxRecords=`) to a spooled
  temporary file, then yields a `ResultPage` with `locator`, `next_locator` and `size`
  (`Sforce-NumberOfRecords`). The file is closed when the iteration moves on. "As long as the
  associated job exists, the locator string for a set of results does not change. You can use
  the locator to retrieve a set of results multiple times" (*Get Results for a Query Job*), so a
  saved `next_locator` is a safe checkpoint. Queues, storage and checkpoint persistence stay in
  the application.

- **Parameter names match REST** (#510, #760): `sf.bulk.query(soql, include_deleted=False)` uses the
  `queryAll` operation when `True`; iterators are named `*_iter` like `query_iter` where they are
  plain iterators.
- **Encoding** (#706, #710): uploads are sent as `Content-Type: text/csv; charset=UTF-8`; result
  CSVs are always decoded as UTF-8, whatever charset httpx guesses.
- **Nulls** (#309, #769): in Bulk 2.0 CSV an empty cell leaves the field unchanged on update and `#N/A`
  sets it to null. On insert (and an upsert that inserts) both give the field its default (e.g.
  `Lead.Status`) or null (org-observed, V18). Rows given as dicts map `None` → `#N/A` and a missing
  key → empty cell; a literal string `"#N/A"` value is rejected with `ValueError` (ambiguous). A
  UTF-8 BOM is stripped from uploads: the org reads it as part of the first field name and fails
  the job (`InvalidBatch : Field name not found : \ufeffLastName`, V18).
- **Streaming uploads** (#738, #629): `rows=csv_path` and iterables are streamed in ≤ 100 MB parts;
  the client never reads a whole file into memory before uploading. Benchmarks assert flat memory.
- **Job states** (#517): states parse into an enum (`Open`, `UploadComplete`, `InProgress`,
  `JobComplete`, `Failed`, `Aborted`). An unknown state raises `UnexpectedResponse` instead of being
  polled forever, and `wait()` always has a timeout (default 3600 s).
- **Compound fields** (#182): Bulk query rejects address and geolocation compound fields at job
  creation with `400 API_ERROR` "Selecting compound data not supported in Bulk Query" (V42). The
  client adds a hint naming the component fields to that `RequestError` (or to `BulkJobFailed`), and
  offers `sf.bulk.expand_compound_fields(sobject, fields)` (describe-based) to rewrite a field list.
- **`FIELDS()`**: `sf.bulk.query` raises `ValueError` before creating a job when the SOQL selects `FIELDS(ALL)` or `FIELDS(CUSTOM)` (string literals ignored). *Create a Query Job* says Bulk 2.0 doesn't support `FIELDS()` at all, while the SOQL *SELECT* page lists `FIELDS(STANDARD)` as supported; both agree on ALL and CUSTOM, so `FIELDS(STANDARD)` is passed to Salesforce. The org runs it on objects without compound fields (`Campaign`, `Opportunity`) and rejects it like a compound field on `Account` or `Lead`; the hint then says so (V81).
- Ingest uploads: one request may carry at most 150 MB **after** Salesforce's base64 conversion, so the client splits raw CSV at **100 MB** per job, as the docs advise. A part over 64 KiB is gzipped on the wire and sent chunked; the org accepts gzipped uploads with `Content-Length` and chunked (V40).
- Results are fetched with `?locator=&maxRecords=` using the **same API version that created the job**; a different version returns 409, so the job object pins its version.
- Query results use `Sforce-Locator` pagination; with `parallel_downloads>1`, locators are fetched ahead with bounded concurrency while preserving order. `resultPages` answers `resultPages[].resultUrl` (full path, job's version) from API 63.0 and `resultChunks[].resultLink` (relative to `/services/data/vXX.X`) at 58.0–62.0; the client reads both (V43).
- `BulkJobFailed` on `state=Failed`; `job.info` exposes counts and timing.

### 9.12 Tooling — `sf.tooling` (0.3)

`query`, `query_iter`, `sobject(name)` (same interface as §9.1), `describe_global(cached=True)`
(`GET tooling/sobjects`, cached like `sf.describe_global()` under its own key), `execute_anonymous`,
`run_tests_async(class_ids)`, `completions(type)`. Uses `/services/data/vXX/tooling/`.

### 9.13 UI API — `sf.ui` (0.4)

`record(id, fields=None, layout_types=None, modes=None)`, `object_info(name)`,
`picklist_values(name, record_type_id)`, `create_defaults(name, record_type_id=None)`,
`clone_defaults(id)`, `list_views(name)`, `list_records(name, list_view)`. Cached like describe.
`record()` takes `fields` or `layout_types`, not both: Salesforce answers both with 400
`INVALID_INPUT_COMBINATION` unless `childRelationships` is also sent (org-observed, V63). An
unknown object on `object-info` is 403 `INSUFFICIENT_ACCESS` (`Forbidden`), not 404 (V61).

### 9.14 Reports and dashboards — `sf.reports` (0.4)

`recent()` (`GET analytics/reports`: at most 200 recently viewed reports; to find any report,
query the `Report` object with SOQL), `describe(report_id)`, `run(report_id, filters=None,
include_details=True)` (sync run), `run_async(...)` + `instance(report_id, instance_id, *,
poll=None, timeout=3600)` polling, `dashboards.get(id)`, `dashboards.refresh(id)` (201 with
only `statusUrl`; 409 while a refresh runs, 403 within a minute of the last one, V68/V69).
Result helpers turn fact maps into rows; empty matrix cells come with a zero count (V67).

### 9.15 Metadata API — `sf.metadata` (0.4)

`deploy(zip_bytes | directory, options=DeployOptions(...))` via REST
`metadata/deployRequest`, `deploy_status(id, *, wait=False, poll=None, timeout=3600)`,
`quick_deploy(validated_id)` (`POST metadata/deployRequest/{validatedId}` with
`{"validatedDeployRequestId": id}`; never resent; the docs warn that `PATCH` there starts a
new deployment), `cancel_deploy(id)` (`PATCH metadata/deployRequest/{id}` with
`{"deployResult": {"status": "Canceling"}}`; never resent; from API 65.0 a deployment in
`FinalizingDeploy` can't be canceled and the refusal raises `DeployNotCancelable`, V85),
`retrieve(package_xml_or_types, *, poll=None, timeout=3600)` via SOAP Metadata API with
polling. `list_metadata(type)`, `describe_metadata()`. Stdlib only.

**Waiting for long-running jobs.** Bulk jobs (`job.wait()`, `sf.bulk.query()`), deployments
(`deploy_status(wait=True)`), retrieves and asynchronous report runs (`instance()`) all wait
the same way: `poll=esf.Backoff(...)` (default `Backoff()`: 2 s doubling up to 30 s) and
`timeout=` seconds (default 3600), ending with the area's timeout error (`BulkJobTimeout`,
`MetadataTimeout`, `ReportTimeout`) while the job keeps running in Salesforce. There is no
shared job-handle type with a `.wait()` method outside Bulk: deployments and report runs are
waited for by id, so a later process can resume a wait (open design question, not 0.4).

### 9.16 Pub/Sub API — `sf.pubsub` (0.5, `[pubsub]`, D31)

Subscribing is the one way to receive events (D31). There is no callback registry and no
`sf.pubsub.publish`: publishing stays with `sf.platform_events` (§9.10) until users ask (D29).

```python
store = MyReplayStore()                                   # optional; AsyncReplayStore protocol
async with sf.pubsub.subscribe("/data/AccountChangeEvent",
                               replay="latest",           # "latest" | "earliest" | replay_id (bytes)
                               replay_store=store,
                               on_invalid_replay="raise", # "raise" | "latest" | "earliest"
                               batch_size=100) as events:
    async for ev in events:
        ev.topic, ev.replay_id, ev.event_id, ev.schema_id
        ev.payload                                        # decoded Avro → dict
        if ev.change is None:                             # platform event
            ...
        elif ev.change.is_gap:                            # GAP_* or GAP_OVERFLOW: no field data
            await resync(ev.change.entity_name, ev.change.record_ids)
        else:
            apply(ev.change.change_type, ev.change.record_ids, ev.change.changed_fields, ev.payload)
```

The sync twin is the same code generated by unasync: `with sf.pubsub.subscribe(...) as events:
for ev in events:`. It blocks, so the docs recommend running it in a dedicated thread.

#### 9.16.1 Connection and auth

- gRPC to `api.pubsub.salesforce.com:443` (7443 is also accepted; the host is configurable). The
  EU-only endpoint `api.deu.pubsub.salesforce.com` is selectable when data in transit must stay
  in the EU.
- Auth metadata comes from the client's own `Auth` and auth runtime (§6): `accesstoken`,
  `instanceurl`, and `tenantid` (the org id from `Token.org_id`). A token with no org id (for
  example `StaticToken`) gets one `GET /services/oauth2/userinfo` call, cached for the client's
  lifetime.
- One gRPC channel per client, shared by all subscriptions and closed with the client. A channel
  carries at most 1,000 concurrent streams; gRPC queues any beyond that in the client, and the
  docs page says so.
- `subscribe()` is an async context manager. The stream closes when the block exits, including on
  `break` and on an exception. A bare async generator would leave the stream open until garbage
  collection. `sf.files.download` (§9.9) uses the same `async with … as …: async for` shape.

#### 9.16.2 Flow control

- The iterator pulls events, so backpressure follows the consumer's speed. The client keeps at
  most `batch_size` events requested but not yet consumed (maximum 100, the server's limit;
  larger values raise `ValueError`) and sends another `FetchRequest` only after the consumer has
  taken events from the buffer.
- When nothing is pending, Salesforce closes a stream that gets no new `FetchRequest` within 60
  seconds. A consumer slower than that therefore sees a transparent resubscribe from the last
  processed replay id (§9.16.4), reported as a `sf.pubsub.reconnect` event with
  `reason="idle"`. No events are lost or skipped. Recorded (V76): the stream ends cleanly
  (gRPC `OK`, no trailers) about 68 s after the last requested event was delivered.
- Server keepalives (an empty `FetchResponse` carrying `latest_replay_id`, sent within 270 s) are
  consumed internally. They are never yielded to the consumer. Recorded (V76): with one event
  requested on a quiet topic they came every ~120 s, with `pending_num_requested` = 1.

#### 9.16.3 Events and decoding

`PubSubEvent(topic, replay_id: bytes, event_id: str | None, schema_id, payload: dict,
change: ChangeHeader | None)`:

- **Schemas** are fetched with `GetSchema` and cached by schema id for the client's lifetime.
  Payloads are decoded with fastavro. An event whose payload does not decode against its schema
  raises `UnexpectedResponse` with `event_id` and `replay_id` set. Salesforce does not validate
  Avro on publish, so such events can exist.
- **`event_id`** is `ConsumerEvent.event.id` as sent (`None` when empty). On platform events it
  is the `EventUuid`, the documented de-duplication key. *Event Message Durability* says the id
  is present on platform events and not on change events, but a recorded change event carried a
  UUID id, stable on redelivery (DISCREPANCY, V79). The client passes it through unchanged and
  the docs keep `(change.transaction_key, change.sequence_number)` as the de-duplication key for
  change events, because only that is documented. Replay ids are not unique across org
  migrations and must never be used as a de-duplication key.
- **Dates**: Date and DateTime values arrive as a `long` of epoch milliseconds with no logical
  type; only the Avro `doc` marks them (recorded, V28/V77): `"CreatedDate:DateTime"`,
  `"Data:DateTime"` or `"Data:DateTime:00N…"` becomes an aware UTC `datetime`, and
  `"Data:DateOnly:00N…"` (midnight UTC) becomes a `date`. `timestamp-millis` and `date` logical
  types are honoured too. Any other `long` stays an `int`.
- **`ChangeHeader`** is built from `ChangeEventHeader` for every CDC event and is `None` for
  platform events:
  - `entity_name`, `change_type` (an enum: `CREATE`, `UPDATE`, `DELETE`, `UNDELETE`,
    `GAP_CREATE`, `GAP_UPDATE`, `GAP_DELETE`, `GAP_UNDELETE`, `GAP_OVERFLOW`; an unknown value
    raises `UnexpectedResponse`), `is_gap` (true for every `GAP_*` value), `is_overflow`;
  - `record_ids: tuple[str, ...]`, which holds several ids when Salesforce merged changes. An
    overflow event carries the empty id `000000000000000AAA`, which is dropped, so
    `record_ids == ()`;
  - `changed_fields`, `diff_fields`, `nulled_fields`: tuples of **field names** decoded from the
    bitmaps;
  - `transaction_key`, `sequence_number`, `commit_timestamp` (`datetime`), `commit_user`,
    `change_origin`.
- **Bitmap decoding** (`core/pubsub.py`, pure, 100 % covered) follows *Event Deserialization
  Considerations*:
  - The first array element, `0x…`, is a bitmap over the top-level schema fields. A set bit *i*
    means schema field *i*.
  - Each further element `{pos}-0x…` is a bitmap over the nested fields of the compound field at
    top-level position `pos`. Those fields are named `Parent.Child` (for example
    `BillingAddress.City`), matching how the docs list changed compound components.
  - When only compound nested fields were nulled, the array holds just `{pos}-…` entries and no
    leading `0x…`.
  - Bits for fields the subscriber cannot see are already cleared, and hidden compound entries
    are omitted, so the decoder never invents names.
  - Bit order: bit 0 = least-significant bit of the hex value, confirmed on a recorded CDC
    event (V27). The decoder is original code, not a port of the official Python sample.
- **Gap events are not resolved automatically.** Re-fetching costs API calls and the fields to
  fetch are app-specific. The docs page has a replication recipe: buffer by `transaction_key`;
  on a gap event re-read `record_ids`; on an overflow event re-sync `entity_name`.

#### 9.16.4 Replay and checkpointing

- **Start position:** a value in `replay_store` wins. Otherwise `replay=` applies, defaulting to
  `"latest"`. Only the first `FetchRequest` of a stream can set the position; later ones are
  ignored by Salesforce, so the client never sends a position after the first request.
- **`AsyncReplayStore` / `ReplayStore`** (protocols, like `TokenStore`):
  `load(key) -> bytes | None`, `save(key, replay_id)`. The key is `f"{org_id}:{topic}"`, so a
  sandbox refresh or a second org never resumes from another org's position. Shipped:
  `AsyncInMemoryReplayStore` / `InMemoryReplayStore`, plus a parametrisable
  `replay_store_contract` test suite in `esf.testing`.
- **Delivery is at least once.** An event counts as processed when the consumer asks for the
  next one, or when the `async with` block exits normally. If the block exits with an exception,
  the current event does not count. The client saves the last processed replay id after each
  fully consumed batch and on normal exit. A crash can redeliver at most one batch.
- **Keepalive positions:** the docs recommend saving the `latest_replay_id` of empty responses so
  that a restart does not seek far back. The client saves it only when every delivered event has
  been processed and nothing is buffered. Saving it earlier would skip events.
- **Rejected replay id** (`sfdc.platform.eventbus.grpc.subscription.fetch.replayid.corrupted`,
  for example older than the 72-hour retention window): `on_invalid_replay="raise"` (default)
  raises `ReplayUnavailable`. `"latest"` or `"earliest"` resubscribes from that preset and emits
  `sf.pubsub.reconnect` with `reason="invalid_replay"`. The default raises because silently
  skipping or replaying three days of events is the app's decision. `"earliest"` can be slow on a
  busy bus, as the docs warn.
- **Retention:** events are kept for 72 hours (not guaranteed beyond). Retained events can be
  reset when an org moves instance.

#### 9.16.5 Errors and reconnects

The gRPC status and the Pub/Sub `error-code` trailer are classified in `core/pubsub.py` into the
§8 hierarchy with the usual shape:

- `error_code` is the trailer value, or the gRPC status name when there is no trailer;
- `request_id` is the `rpcId` that Salesforce Support asks for;
- `operation` is `"pubsub.subscribe"`, `path` is the topic, `status` is `None`.

| Pub/Sub code (gRPC status) | Raised / done |
|---|---|
| `…service.auth.error`, `…auth.headers.invalid` (UNAUTHENTICATED) | refresh once through the shared single-flight refresh (§6.4), then resubscribe; a second failure raises `SessionExpired` |
| `…subscription.fetch.replayid.corrupted` (INVALID_ARGUMENT) | `ReplayUnavailable` or the `on_invalid_replay` fallback (§9.16.4) |
| `…topic.not.found` (NOT_FOUND) | `NotFound` |
| `…topic.cannot.subscribe`, `…topic.meta.permission`, `…tenant.license` (PERMISSION_DENIED) | `Forbidden` (`tenant.license` is retried once first; the docs call it sometimes intermittent) |
| `…concurrent.client.limit.exceeded`, `…subscription.limit.exceeded`, `…service.protection.*` (RESOURCE_EXHAUSTED) | `PubSubLimitExceeded` (a `LimitError`) |
| `RST_STREAM` / UNAVAILABLE, `…service.unavailable`, `…subscription.internal.error`, `…replay.repeated` (INTERNAL) | reconnect with backoff from the last processed replay id |
| `…org.migrated` (UNAVAILABLE) | reconnect no sooner than 30 minutes apart, as the docs require |
| anything else | `PubSubError` (a `SalesforceError`) with the fields above |

- Reconnects use the client's `RetryPolicy` backoff (§7.4). The attempt counter resets after the
  first event delivered on a new stream. When attempts run out, the last error is raised.
- Every reconnect resumes from the last **processed** replay id held in memory, whether or not a
  `replay_store` is set. Events already buffered but not consumed are dropped and redelivered.
- `PubSubError`, `PubSubLimitExceeded` and `ReplayUnavailable` follow every §15.1 rule
  (pickling, `str()`, odd bodies).

#### 9.16.6 Limits that shape the interface

`num_requested` is at most 100 per stream. A channel carries at most 1,000 concurrent streams. An
org has at most 200 managed subscriptions. The CDC, platform-event and real-time event
monitoring delivery allocations apply to Pub/Sub subscribers.

#### 9.16.7 Not in 0.5

- **`ManagedSubscribe`** (Salesforce stores the replay position) is **Beta**. When it is GA it
  becomes one parameter, `subscribe(..., managed_subscription="<id or name>")`, never a second
  method. The client would then hide its keepalive rules: request within 60 s, commit within
  30 min, commit the `latest_replay_id` of empty batches. It must not be combined with
  `replay_store`.
- **Publishing over gRPC** (`Publish`, `PublishStream`): deferred under D29; see the intro above.
  If it is added, batches are split under 3 MB and 200 events (1 MB per event).
- Event filters and enrichment on channels are org configuration (Metadata/Tooling), not client
  features.

### 9.17 Limits — `sf.limits()` (0.1)

`GET limits` → `dict[str, Limit(max, remaining)]`. Not called implicitly (it costs an API call). See §10 for header-based tracking.

### 9.18 Command line — `esf`

The CLI is a terminal front end over the public API; it adds no library API of its own. Org
settings come from `ESF_*` environment variables (§5.3).

| Command | Ships | Does |
|---|---|---|
| `esf query "SELECT ..." [--json\|--csv] [--all] [--include-deleted] [--page-size N]` | 0.1 | runs SOQL (first page unless `--all`); table by default, CSV output uses `esf.flatten` |
| `esf whoami`, `esf limits [--json]`, `esf version` | 0.1 | identity, `/limits`, versions |
| `esf generate ...` | 0.1 placeholder | prints that code generation arrives in 0.2 |
| `esf describe <sObject>` | 0.2 | fields with type, length, required, picklist values |
| `esf request <METHOD> <path> [--json ...]` | 0.2 | `sf.request` from the terminal |
| `esf generate actions ...` | 0.2 | typed action stubs (§9.7) |
| `esf generate models <sObject>... --out file.py [--pydantic]` | 0.2 | typed sObject models |
| `esf migrate <path>... [--write] [--report FILE] [--check]` | 0.1 | rewrites simple-salesforce code; marks what it can't (§9.19) |

- **`generate models`** reads `describe` for each sObject and writes one `TypedDict`
  (`total=False`; nullable fields as `X | None`; picklists as `Literal[...]` when restricted) or,
  with `--pydantic`, one model. `model=` accepts a `TypedDict` (typing only, no runtime conversion),
  so pydantic is not required for autocomplete. The file header names the org, API version and
  timestamp, like action stubs.
- **`migrate`**: see §9.19.

### 9.19 Migration codemod — `esf migrate` (0.1, `[migrate]`)

`esf migrate` rewrites simple-salesforce code (sync `simple_salesforce.Salesforce` and async
`simple_salesforce.aio.AsyncSalesforce`) to this library. It changes only what it can prove is
safe. Everything else is found and marked, so a person or an agent can finish the job from a
checklist. It is built on libcst (keeps comments and formatting) behind the `[migrate]` extra, and
it is original code (AGENTS.md licensing).

```
esf migrate <path>... [--write] [--report FILE] [--check]
            [--client-names NAME,...] [--rules RULE,...] [--exclude GLOB,...]
```

| Flag | Effect |
|---|---|
| *(default)* | dry run: prints a unified diff and a summary; changes no files |
| `--write` | applies rewrites and inserts `esf-migrate` markers in place |
| `--report FILE` | writes the machine-readable report (below); `-` for stdout |
| `--check` | changes nothing; exit 1 if any marker or unconverted pattern remains, else 0 (for CI) |
| `--client-names` | names (last attribute segment, e.g. `sf_client` matches `self.sf_client`) that are Salesforce clients; matches on other names become `not-a-client` |
| `--rules` | run only these rule ids |
| `--exclude` | skip paths (`.venv`, `site-packages`, `node_modules`, `.git` are always skipped) |

Exit codes: `0` nothing left to do; `1` items need review or are TODO (with `--check`, or after a
run that left markers), or a dry run with rewrites still pending; `2` usage error or a file that
doesn't parse (reported, other files still processed). A second run over already-migrated code makes no changes (idempotent).

#### 9.19.1 Detection

A site is **client-shaped** when it is `<expr>.<Name>.<method>(...)` and `<method>` is one of
simple-salesforce's per-object methods: `get`, `get_by_custom_id`, `create`, `update`, `upsert`,
`delete`, `describe`, `describe_layout`, `metadata`, `updated`, `deleted`. `<expr>` is any
expression (`sf`, `self.sf_client`, `deps.admin_client`, a call result). `<Name>` is any
identifier, so standard, custom (`__c`), event (`__e`) and metadata (`__mdt`) objects are all
covered without a list of object names. The same shape with `await` in front is the async form.

A site is **confirmed** (not just shaped) when any of these hold, in order:
1. the last attribute segment of `<expr>` is in `--client-names`;
2. `<expr>` is a name or attribute annotated with, assigned from, or returned by a
   simple-salesforce client type or constructor in the same module (libcst scope analysis; no
   cross-module type inference);
3. no `--client-names` was given, `<Name>` starts with an uppercase letter, **and** the module
   imports simple-salesforce or holds a client confirmed by case 2 (the *fallback*).

Shaped but unconfirmed sites:

- case 3 holds → rewritten with rule `not-a-client`, status `needs-review`;
- otherwise, if `<Name>` **looks like an sObject** (a custom suffix `__c`, `__e`, `__mdt`, `__x`,
  `__b`, `__kav`, or one of a fixed list of common standard objects: `Account`, `Contact`, `Lead`,
  `Opportunity`, `Case`, `Task`, `Event`, `User`, ...) → not rewritten; a `not-a-client` **TODO**
  with the suggested rewrite. This also applies with `--client-names` when the name doesn't match,
  so every client-shaped site is either rewritten or marked, and `--check` counts it;
- anything else → left alone (`config.Settings.get(...)` is not a Salesforce call).

In test files, mock-shaped chains (`X.Obj.method.assert_*`, ...) on sObject-looking names use the
fallback without the import condition (tests rarely import simple-salesforce).

#### 9.19.2 Rules

Every finding carries a stable rule id. Ids are part of the public interface (docs pages link to
`.../migrate/#<rule>`); new rules may be added, ids are never renamed.

| Rule | Finds | Action | Status |
|---|---|---|---|
| `sobject-attr` | `X.Obj.method(...)` | → `X.sobject("Obj").method(...)` | `rewritten` |
| `sobject-getattr` | `getattr(X, name)` where the result is used as an object type (a per-object method is called on it, or it is assigned and then called) | → `X.sobject(name)` | `rewritten` |
| `upsert-path` | `upsert("Ext__c/42", data)` | → `upsert("Ext__c", "42", data)`; f-strings split on the first literal `/` | `rewritten`; `todo` if the path isn't a literal or splittable f-string |
| `get-by-custom-id` | `get_by_custom_id(field, value)` | → `get_by_external_id(field, value)` | `rewritten` |
| `create-return` | result of `create()` used only as `r["id"]` / `r.get("id")` in the same function | assignment kept, those subscripts → `r` | `rewritten` |
| `create-return` | result of `create()` used any other way (passed on, returned, `r["success"]`, stored) | marker only | `todo` |
| `stored-ref` | `v = X.Obj` (an object accessor stored in a name or attribute) | → `v = X.sobject("Obj")` if every use of `v` in scope is a per-object method call | `rewritten`; otherwise `todo` |
| `mock-sobject` | in test files (`test_*.py`, `*_test.py`, `conftest.py`, under `tests/`): `X.Obj.method.<mock attr>` where `<mock attr>` is `assert_*`, `return_value`, `side_effect`, `call_args*`, `call_count`, `await_count`, `await_args*`, `mock_calls` | text rewritten like `sobject-attr` | `needs-review` (see below) |
| `return-value` | result of `update()` / `upsert()` / `delete()` used (they returned HTTP status codes; now `None` / `UpsertResult`) | marker only | `needs-review` |
| `query-result` | `X.query(q)` result used as `["records"]` / `.get("records")`, `["totalSize"]`, `["done"]` (confirmed client) | → `.records`, `.total_size`, `.done` | `rewritten`; `todo` for other keys |
| `query-all` | `X.query_all(q)` / `X.query_all_iter(q, ...)` | → `list(X.query_iter(q))` (sync) / `[r async for r in X.query_iter(q)]` (async) / `X.query_iter(q, ...)` | `rewritten` when the result is used only as `["records"]`; `needs-review` for other uses of the bare result; `todo` for an un-awaited async call |
| `query-more` | `X.query_more(url, True)` | marker only (needs a `Cursor`, not a URL) | `todo` |
| `format-soql` | `format_soql("... {} ...", v)` passed directly to a query method | → values in SOQL methods | `rewritten`; `todo` when the formatted string is built elsewhere |
| `restful` | `X.restful(path, method=..., json=...)` | → `X.request(method, path, json=...)` | `rewritten` |
| `apexecute` | `X.apexecute(path, method=, data=)` | → `X.request(METHOD, "/services/apexrest/<path>", json=data)` (until `sf.apex` ships in 0.2) | `rewritten` for a literal path; else `todo` |
| `exceptions` | simple-salesforce exceptions in `except`, `isinstance`, `raise` and the imports | 1:1: `SalesforceError` → `esf.SalesforceError`, `SalesforceResourceNotFound` → `esf.NotFound`, `SalesforceMoreThanOneRecord` → `esf.MultipleMatches`, `SalesforceRefusedRequest` → `esf.Forbidden`, `SalesforceAuthenticationFailed` → `esf.AuthError`; `import even_simpler_salesforce as esf` added once | `rewritten`; `todo` (with a suggestion) for `SalesforceExpiredSession` (§20: refresh is automatic), `SalesforceMalformedRequest`, `SalesforceGeneralError` |
| `client-internals` | `X.sf_version` | → `X.api_version` | `rewritten` |
| `client-internals` | `X.session_id`, `X.base_url`, `X.sf_instance`, `X.headers`, `X.session` | marker only, with the §20 replacement in the message | `todo` |
| `client-methods` | `X.describe()`; `X.quick_search()`, `X.is_sandbox()`, `X.search(s)["searchRecords"]`, `X.limits()[k]["Remaining"]` | → `X.describe_global()`; others marker only | `rewritten`; `todo` |
| `login` | `Salesforce(...)` / `AsyncSalesforce(...)` / `SalesforceLogin(...)` construction | marker only (auth is a design choice, §6) | `todo` |
| `not-a-client` | a shaped site that couldn't be confirmed (§9.19.1) | case 3: rewritten like its rule; sObject-looking: marker with suggestion | `needs-review` / `todo` |

More cases:

- **Method references** (`fn = sf.Lead.create`) on a confirmed client → `fn = sf.sobject("Lead").create`
  (`sobject-attr`, `rewritten`). **Monkeypatching** (`sf.Lead.create = x`) → `sobject-attr` `todo`
  (`sobject()` returns a new object each time).
- `describe_layout()`, the `headers=` / `raw_response=` keywords, and `upsert(record_id=...)` as a
  keyword have no direct equivalent → `todo` (`unsupported` / `upsert-path`).
- Rules not listed (Bulk v1, `bulk2`, `toolingexecute`, `mdapi`, SOAP-only calls) produce a
  generic `unsupported` `todo` pointing at §20.
- A simple-salesforce import that is still needed after the run (some name from it is still used)
  gets an `unsupported` `todo` on the import statement; imports no longer used are removed.

**Why `mock-sobject` is review, not done.** `MagicMock().sobject("Lead")` and
`MagicMock().sobject("Task")` return the same child mock, so after the text rewrite an assertion no
longer proves *which* object was touched. The marker message recommends
`esf.testing.FakeSalesforce` (§14), which stores records per object.

#### 9.19.3 Markers

Every `todo` and `needs-review` finding gets a comment on the line above the statement:

```python
# esf-migrate: TODO[create-return] result is passed to save_audit(); create() now returns the id (str), not a dict
res = await sf_client.sobject("Lead").create(data)

# esf-migrate: REVIEW[mock-sobject] sobject() mocks don't distinguish objects; prefer esf.testing.FakeSalesforce
sf_client.sobject("Lead").update.assert_awaited_once_with(LEAD_ID, {"Status": "Working"})
```

- Format: `# esf-migrate: (TODO|REVIEW)[<rule>] <message>`. One marker per finding; identical
  marker texts on one statement are written once, and a rerun never duplicates a marker that is
  already there.
- Markers go above the statement that contains the site; for a site in a decorator, default
  argument, base class or `match` subject, above the decorator / `def` / `class` / `match` header.
- A person or agent resolves a finding by fixing the code and deleting the marker.
  `# esf-migrate: ignore[<rule>]` on the line above suppresses that rule for that statement
  (e.g. a real false positive). Ignoring `sobject-attr` also suppresses its sub-rules
  (`create-return`, `upsert-path`, `get-by-custom-id`, `return-value`, `not-a-client`), and
  `--rules` with a sub-rule implies `sobject-attr`.
- `--check` counts remaining `TODO`/`REVIEW` markers plus any client-shaped site still in old form.

#### 9.19.4 Report

`--report` writes JSON (one object, stable key order) so an agent can work through it:

```json
{
  "version": 1,
  "esf_version": "0.1.0",
  "mode": "write",
  "summary": {"files": 18, "rewritten": 47, "needs_review": 9, "todo": 6, "errors": 0},
  "findings": [
    {
      "file": "app/follow_ups/repository.py",
      "line": 42, "col": 15, "end_line": 42, "end_col": 58,
      "rule": "sobject-attr",
      "status": "rewritten",
      "before": "await self.task_client.Task.update(task_id, body)",
      "after": "await self.task_client.sobject(\"Task\").update(task_id, body)",
      "message": "object accessed through sobject()",
      "suggestion": null,
      "docs_url": "https://.../migrate/#sobject-attr"
    }
  ],
  "errors": [{"file": "...", "message": "parse error: ..."}]
}
```

- `status` is one of `rewritten`, `needs-review`, `todo`. Line numbers refer to the file **after**
  the run (with `--write`, mapped through a line diff of the original and the written file) or the
  original file (dry run and `--check`); `mode` (`"write"` or `"dry-run"`; `--check` reports
  `"dry-run"`) says which.
- `suggestion` holds suggested replacement code when the tool has one but can't prove it safe.
- The report never contains string literal contents longer than 80 characters (including
  multi-line strings) or anything that looks like a secret (same redaction as §11): bearer tokens,
  session ids, and the values of `password`, `secret`, `token`, `session_id`, `private_key`, `key`
  and similar names.

#### 9.19.5 Acceptance

- Golden-file tests per rule: input, expected output, expected report entries; covering sync and
  async, `self.`-prefixed clients, custom objects, nested calls, already-migrated code (no change),
  and the `ignore` marker.
- Idempotence property test: `migrate(migrate(x)) == migrate(x)`.
- Formatting test: comments and blank lines outside rewritten nodes are byte-identical.
- Run against a real production codebase that uses `simple_salesforce.aio`: every client-shaped
  site is either rewritten or marked, `--check` is green after the marked items are resolved, and
  the project's tests pass.

---

## 10. API usage and budgets

### 10.1 Sources

| Source | Gives | Cost | Default |
|---|---|---|---|
| `Sforce-Limit-Info: api-usage=used/max` on every response | org-wide daily API calls | free | on |
| Library counters | calls by operation, sObject, caller tag, status; retries; refreshes | free | on |
| `GET /limits` | every limit (Bulk, async Apex, platform events, storage) | 1 call | opt-in poller, min interval 5 min |
| `ApiTotalUsage` event log | per-app/per-user usage | needs Event Monitoring | out of scope (O15) |

### 10.2 Caller tags and priority

**Integration name for admins** (#726): `AsyncSalesforce(..., client_name="acme-sync")` sends
`Sforce-Call-Options: client=acme-sync` on every API call, so the org's API usage can be
attributed to this integration on the Salesforce side. Opt-in; validated to
`[A-Za-z0-9_.:-]{1,64}`.

**Default namespace** (managed-package orgs; **planned, not shipped** (spec-vs-code check, 2026-11-13)): `AsyncSalesforce(..., default_namespace="battle")`
adds `defaultNamespace=battle`, so SOQL and records can name `botId__c` instead of
`battle__botId__c`. Per the REST API Developer Guide (*Call Options Header*): Salesforce strips the
prefix from response field names when this is set, and ignores it for describe calls; it is not
supported by Bulk API 1.0. Validated as a namespace prefix (1–15 letters, digits or underscores,
starting with a letter, no `__`; *Register a Namespace for a First-Generation Managed Package*
says "up to 15 alphanumeric characters", begins with a letter, no two consecutive underscores).
V26: an org without a namespace accepts any `defaultNamespace` value (even `1bad`, `a__b`, 16
characters) and ignores it, so only an org with a registered namespace can confirm the
server-side rule; validate client-side from the docs. The docs page for each client
option states the response-name change, because it alters dict keys.

**Merging and separator (fix).** Options are joined with `"; "`, the separator in Salesforce's
only documented example (`client=caseSensitiveToken; defaultNamespace=battle`). The 0.1 draft
joined with `", "` and split the caller's header on `,` only, so a caller's
`defaultNamespace=ns; client=x` was read as one option. A caller's own `Sforce-Call-Options` passed
through `sf.request` is split on `;` **and** `,`, merged with ours (ours first, header name matched
case-insensitively), and an option key the caller sets for one call (`client=`,
`defaultNamespace=`) wins for that call. Pure function in `core/http.py`, unit-tested.

Salesforce documents `Sforce-Call-Options` for sObject Basic Information, sObject Rows, sObject
Rows by External ID, Query, QueryAll, Search and Bulk. We send it on every REST data call; V20
checks it is accepted (or ignored, not rejected) on Collections and Composite and where admins see
the `client` value.

**MRU updates** (**planned, not shipped** (spec-vs-code check, 2026-11-13)): `AsyncSalesforce(..., update_mru=False)` would send `Sforce-Mru: updateMru=false`
(API 60.0+; `ValueError` at construction if `api_version` is older). Salesforce defaults to
`true` and recommends `false` for integration users, who never see Recent Items. Default `None`
(no header, Salesforce's behaviour) so the library doesn't silently change what users see in
the UI; the records guide recommends `update_mru=False` for integrations. Sent only on the
resources Salesforce documents for it: `sf.sobject(...)` create, get, get-by-external-id,
update and upsert (sObject Basic Information POST, Rows, Rows by External ID). SOQL never updates
the MRU, so it is not sent on query.


`sf.tagged(caller="job:backfill", priority="low")` returns a child view whose calls carry those
attributes (for counters, logs and the guard). Default `priority="normal"`. A contextvar alternative
exists: `with esf.call_context(caller="route:/leads", priority="high"): ...`. Precedence:
per-call spec priority, then the `tagged()` view, then `call_context`, then `"normal"`.

### 10.3 Snapshot

`sf.usage.snapshot() -> UsageSnapshot`:
`api_used`, `api_max`, `api_fraction`, `as_of`, `calls_by_operation`, `calls_by_caller`,
`errors_by_code`, `retries`, `refreshes`, `projected_exhaustion` (linear projection from the rate
over the last hour of observed header values; `None` if insufficient data). Counters are
per-process; the header values are org-wide.

### 10.4 Budget events and guard

```python
sf = esf.AsyncSalesforce(auth, budget=esf.Budget(
    warn_at=(0.5, 0.8, 0.95),             # emit sf.usage.threshold once per threshold per day
    pause_low_priority_above=0.8,          # raise BudgetExceeded for priority="low" calls
    block_all_above=None,                  # optional hard stop for everything except priority="high"
))
```

The guard acts on the latest header value (no extra calls). `BudgetExceeded` carries the current
usage so jobs can sleep and resume.

---

## 11. Observability: canonical logs

### 11.1 Principles

- The library **never configures logging** and never prints, except with `ESF_DEBUG=1`.
- It emits structured **events** to **observers**. Adapters turn events into stdlib logs, structlog events or OpenTelemetry spans/metrics.
- **One wide event per logical Salesforce call** (`sf.request`), emitted once after retries and refreshes complete, carrying everything needed to debug that call.
- **One roll-up per unit of work** (HTTP request, job), merged into the app's own canonical log line.

### 11.2 Events

| Event | When | Key fields |
|---|---|---|
| `sf.request` | each logical call finishes | see 11.3 |
| `sf.retry` | before each retry | attempt, reason, delay_ms |
| `sf.auth.refreshed` | token refreshed | token_key_hash, flow, duration_ms, rotated: bool, proactive: bool |
| `sf.auth.reauth_required` | invalid_grant | token_key_hash |
| `sf.usage.threshold` | budget threshold crossed | fraction, used, max |
| `sf.bulk.job` | bulk job created, state changes, or a `wait()` poll sees `numberRecordsProcessed`/`numberRecordsFailed` change | job_id, operation, sobject, state, records_processed, records_failed (`None` for query jobs: their job info has no `numberRecordsFailed`) |
| `sf.pubsub.subscribed` | a stream opens (first and every reconnect) | topic, replay_preset, resumed_from (`store`/`memory`/`none`), stream_no |
| `sf.pubsub.batch` | a non-empty `FetchResponse` is received | topic, events, pending, lag_ms (newest event's commit/created time vs now, when the payload has one) |
| `sf.pubsub.reconnect` | before each resubscribe | topic, attempt, reason (`idle`/`error`/`auth`/`invalid_replay`/`org_migrated`), error_code, rpc_id, delay_ms |
| `sf.pubsub.closed` | the `subscribe()` block exits | topic, events, reconnects, duration_ms, outcome (`ok`/`error`), error_class |

Pub/Sub streams are long-lived, so they emit no `sf.request` events. `GetSchema` / `GetTopic`
calls are counted in `sf.pubsub.subscribed` instead. Replay ids are logged as hex (they are
opaque positions, not secrets). Payloads and decoded field values are never logged.

### 11.3 `sf.request` fields

`operation` (`sobject.get`, `query`, `query_more`, `collections.update`, `bulk.ingest.upload`, ...),
`sobject`, `method`, `path_template` (ids → `:id`), `status`, `outcome` (`ok`/`error`),
`error_code`, `error_class`, `duration_ms`, `attempts`, `retry_reasons`, `refreshed`,
`records` (returned or sent), `bytes_in`, `bytes_out`, `api_used`, `api_max`, `caller`,
`priority`, `soql_hash` (SHA-256 of normalised SOQL, first 12 hex), `soql` (only if
`log_soql="redacted"`: string/number literals and SOSL `FIND {...}` terms replaced by `?`),
`request_id`, `api_version`, `client_id_hash`, `records_failed` (Collections only),
`sf_warning` (text of a `Warning` response header, when present; planned with `SalesforceWarning`),
`error_message` (only with `ESF_DEBUG`: Salesforce messages can quote record values).

**Never logged:** access/refresh tokens, `Authorization` headers, client secrets, private keys,
record bodies, SOQL literals (unless explicitly enabled), file contents.

### 11.4 Observers

```python
class Observer(Protocol):
    def on_event(self, event: esf.Event) -> None: ...   # MUST NOT raise; exceptions are swallowed and counted
```

Shipped: `esf.observability.StdlibLogging(logger="esf", level=INFO)`,
`esf.observability.Structlog()` (`[structlog]`), `esf.observability.OpenTelemetry()` (`[otel]`:
a span per `sf.request` with semantic attributes, counters `esf.requests`, `esf.errors`, histogram
`esf.duration`, gauge `esf.api_usage`; for Pub/Sub, counter `esf.pubsub.events` and histogram
`esf.pubsub.lag`).

### 11.5 Roll-up summary

```python
with esf.capture() as summary:          # contextvar-scoped; works across awaits and child clients
    ... do work ...
summary.as_fields()
# {"sf_calls": 7, "sf_ms": 820, "sf_errors": ["INVALID_FIELD"], "sf_retries": 1,
#  "sf_refreshed": False, "sf_api_used": 12000, "sf_api_max": 100000, "sf_ops": {"query": 3, ...}}
```

The FastAPI and Flask integrations wrap each request in `capture()` and hand the fields to a
user-supplied callback (e.g. the app's log-context binder), so they land on the app's existing
`request.completed` line.

### 11.6 `ESF_DEBUG=1`

Installs a stderr observer that prints one line per call:

```
esf GET  query        Lead      200  115ms  2000 rows  api 12.0k/100k
esf PATCH sobject.update Lead   400   88ms  FIELD_CUSTOM_VALIDATION_EXCEPTION  "[ADDR_REQUIRED] Street is required"
esf POST oauth.refresh          200  240ms  rotated
```

`ESF_DEBUG=2` adds request/response headers (redacted) and timing per attempt.

---

## 12. Performance

| Measure | Design | Default |
|---|---|---|
| Token reuse | In-memory per process; refresh only on 401 (or opt-in proactive) | on |
| Connection reuse | One pooled httpx client per client family; children share it | on |
| Compression | `Accept-Encoding: gzip` (Salesforce returns gzip-compressed responses when asked); gzip large request bodies | on |
| HTTP/2 | `[http2]` extra; `http2=True` | off |
| Usage tracking | From headers, never by polling `/limits` | on |
| Describe cache | In-memory TTL (default 1 h, max 500 entries, LRU); `Cache` protocol for shared caches; keyed by org id + api version + sObject + user id hash (FLS differs by user) | on |
| Conditional describe | When a cached describe is stale, revalidate with `If-Modified-Since`, and with `If-None-Match` when Salesforce sent an `ETag` (the UI API sends no `Last-Modified` and ignores `If-Modified-Since`, V62); Salesforce documents `304 Not Modified` with no body for describe | on |
| Round trips | Collections (200 writes / 2,000 reads), Composite (25), Graph, GraphQL nested reads, query `batchSize` up to 2,000, Bulk 2.0 with parallel result download | n/a |
| Concurrency | Bounded per client (`max_concurrency`), chunk concurrency for Collections | none / 4 |
| Backoff | Full jitter, honours `Retry-After` | on |
| JSON | orjson when installed; plain dicts | stdlib |

**Benchmarks** (`tests/benchmarks/`, pytest-benchmark, recorded responses through `httpx.MockTransport`):
client overhead per call (target < 0.3 ms over raw httpx), JSON parse of a 2,000-row page,
`query_iter` over 10 pages, Collections chunking of 10,000 rows, Bulk CSV parse of 100k rows,
refresh single-flight with 100 concurrent callers (exactly one token POST). CI stores results and
fails on > 20% regression against `main`. An opt-in `nox -s bench-sandbox` runs the same set
against a real sandbox.

---

## 13. Framework integrations

### 13.1 FastAPI — `even_simpler_salesforce.contrib.fastapi` (0.2)

```python
from even_simpler_salesforce.contrib.fastapi import SalesforceIntegration

esf_app = SalesforceIntegration(
    auth=esf.ClientCredentials(...),          # app-level client
    store=MyTokenStore(),
    auth_for=lambda request: esf.WebServerFlow(..., token_key=f"user:{request.state.user_id}"),
    on_summary=lambda fields: structlog.contextvars.bind_contextvars(**fields),   # canonical log roll-up
    error_handlers=True,                       # optional mapping of typed errors to HTTP responses
)
app = FastAPI(lifespan=esf_app.lifespan)       # creates and closes the client

AdminSF = Annotated[esf.AsyncSalesforce, Depends(esf_app.admin)]
UserSF  = Annotated[esf.AsyncSalesforce, Depends(esf_app.user)]   # uses auth_for → with_auth

@app.get("/leads/{id}")
async def get_lead(id: str, sf: UserSF): ...
```

- `error_handlers=True` registers handlers that map: `ReauthorizationRequired` → 401 with a stable `{"code": "salesforce_reauth_required"}`; `RecordError` → 422 with `error_code`, `fields`, `message`; `NotFound` → 404; `LimitError` → 503 + `Retry-After`; `TransportError`/`ServerError` → 502/503. Apps can pass their own mapping instead. Disabled by default.
- A middleware wraps each request in `esf.capture()` and calls `on_summary`.

### 13.2 Flask — `even_simpler_salesforce.contrib.flask` (0.2)

```python
ext = esf.contrib.flask.Salesforce(app, auth=esf.ClientCredentials(...), on_summary=...)
# in a view:
rows = ext.client.query(soql).records
```

One sync client per process, shared across threads; `teardown_appcontext` emits the roll-up.

---

## 14. Testing support for users

`even_simpler_salesforce.testing` (`[testing]` extra):

- **`FakeSalesforce`**: in-memory implementation of both client interfaces for unit tests.
  - Stores records per sObject; supports `sobject().get/create/update/upsert/delete`, `collections.*`, `actions.*` (registered handlers), `apex.*` (registered handlers).
  - **Query is scripted, not executed**: `fake.on_query("SELECT Id FROM Lead WHERE ...", rows=[...])` or a predicate/callable. No SOQL engine (a fake engine would lie).
  - Error injection: `fake.fail_next("sobject.update", esf.ValidationRuleFailed(...))`.
  - Records every call for assertions: `fake.calls`.
- **Recorder**: `esf.testing.record(path)` captures real responses (redacted: tokens, ids optional, emails/phones masked) into JSON fixtures for contract tests; `esf.testing.replay(path)` serves them via `httpx.MockTransport`.
- **pytest plugin** (auto-registered entry point): fixtures `esf_fake`, `esf_async_fake`, `esf_replay`; marker `@pytest.mark.esf_sandbox` skipped unless `ESF_SANDBOX_*` env vars are set.
- **`token_store_contract`**: a parametrisable test suite apps run against their own `TokenStore` (CAS, concurrency, delete, round-trip).
- **`replay_store_contract`** (0.5): the same for `ReplayStore` (round-trip, per-org keys, overwrite, missing key → `None`).
- **Pub/Sub fake** (0.5): `fake.pubsub.emit(topic, payload)` and `fake.pubsub.emit_change(entity, change_type, record_ids, changed_fields=...)` feed `sf.pubsub.subscribe` on `FakeSalesforce`; `fake.pubsub.fail_next(esf.ReplayUnavailable(...))` injects errors. No gRPC involved.

---

## 15. The library's own test strategy

| Layer | Location | Tools | What |
|---|---|---|---|
| Unit | `tests/unit` | pytest | `core/` builders and parsers, SOQL escaping (property tests with hypothesis), cursor encode/validate/sign, error classification for every recorded error body, limit-header parsing, unasync output |
| Contract | `tests/contract` | pytest + respx, parametrised over async/sync | every public method against recorded responses: success, each error family, pagination, chunking, retries (fake clock), refresh single-flight and rotation, "already being processed", ambiguous refresh |
| Pub/Sub contract | `tests/contract/pubsub` | in-process `grpc.aio` / `grpc` fake server implementing the vendored `.proto`, parametrised over async/sync | flow control (never more than `batch_size` outstanding), keepalive handling, idle-close resubscribe, every §9.16.5 error row, replay-store save points (batch end, normal exit, not on exception, keepalive only when drained), auth refresh on UNAUTHENTICATED |
| Concurrency | `tests/contract/test_refresh_concurrency.py` | asyncio + threads | 100 concurrent callers on an expired token → exactly 1 token POST; rotated token saved before use; CAS conflict path |
| Sandbox | `tests/sandbox` | opt-in, real org | smoke per API area; never runs on PRs from forks |
| Docs | `tests/docs` | pytest + replay | every fenced `python` block in `docs/` runs against replayed responses |
| Benchmarks | `tests/benchmarks` | pytest-benchmark | §12 |
| Types | — | pyright strict on `src/` and `tests/`; `reveal_type` checks for overloads (`model=`) | |

Coverage gate: **≥ 90 % lines and branches** overall, **100 %** for `core/` and `errors.py`.

### 15.1 Guard tests from simple-salesforce's issue history (D28)

| Guard | Protects against | simple-salesforce issues |
|---|---|---|
| Both JSON backends reject NaN/±Inf; `Decimal` is an exact number | fields silently blanked; precision loss | #442, #309, #135 |
| Every exception class pickles, `str()`s and keeps its fields; odd error bodies never raise `KeyError` | Celery/multiprocessing breakage; crashes while building an error | #208, #827, #794, #791, #768 |
| Refresh happens at most once per call; token-endpoint 401/`invalid_grant` never triggers another refresh | infinite refresh recursion | #569, #804, #709 |
| AST test: only the transport imports httpx and sends requests; no hard-coded API versions | API areas that skip refresh/timeouts/proxies/logging; stale versions | #619, #686, #91, #523, #148, #726, #506, #78, #268 |
| Property tests for every chunker (Collections 200/2000, Bulk 100 MB) | off-by-one chunking, lost or duplicated rows | #590, #693, #739, #280 |
| Snapshot of public result types | return shapes changing in minor releases | #469, #557 |
| `sf.request`: any 2xx is success, 204/empty → `None`, non-JSON → text, GET never sends a body, no `X-PrettyPrint` | crashes on valid responses | #146, #122, #501, #386, #596 |
| Wheel smoke test in CI (build, clean venv, import, one call) | files missing from the published package | #533, #641, #437, #715 |

---

## 16. Documentation site

- **Tool:** Zensical, native `zensical.toml` (O1). Commands: `zensical serve` (localhost:8000), `zensical build --clean` → `site/`.
- **Hosting:** GitHub Pages, source "GitHub Actions". `.github/workflows/docs.yml` builds on push to `main` (→ `dev`) and on release tags (→ `latest`), publishing with `actions/deploy-pages`.
- **Versions on 0.x:** `latest` and `dev` only (O13). Per-release copies from 1.0, using whatever versioning Zensical supports by then (currently only a temporary `mike` fork).
- **API reference:** Zensical has a native mkdocstrings implementation (since 0.0.11; install `mkdocstrings-python`). Zensical does **not** support `gen-files`; generate reference pages and navigation with its `autoapi` or `api-autonav` plugins instead. Configure under `[project.plugins.mkdocstrings]` in `zensical.toml`.
- **Docstrings:** Google style, every public symbol, with an example where useful.
- **Structure** (mirrors simple-salesforce's docs, extended): Home · Getting started · Authentication (one page per flow, Salesforce setup steps for External Client Apps, token stores, "which flow do I need?") · Guides per API area · Common tasks (cookbook) · Errors (one troubleshooting entry per error class, the target of `docs_url`, §8.5) · Usage & budgets · Logging · Performance · Integrations · Testing · Migrating from simple-salesforce · Reference · Changelog · ADRs.
- The query guide opens with one table that says which of `query` / `query_one` / `query_first` / `query_iter` /
  `query_more` / `count` to use for which job, so they don't read as alternatives (D29).
- Every code example is executed by `tests/docs` (§15).

---

## 17. Versioning, changelog and releases

### 17.1 SemVer policy (D21)

- 0.x until the interface is stable; breaking changes allowed in minor releases on 0.x but always listed under **Changed/Removed** with migration notes.
- From 1.0: breaking changes only in majors.
- **Public surface** = names in `__all__` of `even_simpler_salesforce` and the documented public submodules. Everything else is private.
- **Deprecation:** `DeprecationWarning` (subclass `esf.EsfDeprecationWarning`) for at least one minor release before removal; documented in the changelog and docs.
- **Salesforce API versions:** each minor release pins a default API version (O8). The library supports configured versions back to **v59.0**; features that need a newer version raise `UnsupportedApiVersion` early. Raising the minimum is a minor (0.x) / major (≥1.0) change.
- **Python:** support follows CPython's lifecycle; dropping a version is a minor release with a changelog entry.

### 17.2 Changelog fragments (D20)

- towncrier, `changelog.d/<issue-or-pr>.<type>.md`, types: `added`, `changed`, `deprecated`, `removed`, `fixed`, `security`, `performance`.
- `changelog-check.yml` fails a PR without a fragment unless labelled `skip-changelog`.
- Format: Keep a Changelog headings in `CHANGELOG.md`.

### 17.3 Release process

1. `uv run scripts/release.py 0.2.0`: bumps `_version.py`, runs `towncrier build`, commits, tags `v0.2.0`.
2. Pushing the tag triggers `release.yml`: full test matrix → `uv build` → PyPI **trusted publishing** (O4) → GitHub Release with the changelog section → docs deploy to `latest`.
3. Pre-releases use `0.2.0rc1` tags and publish to PyPI as pre-releases; docs stay on `dev`.

---

## 18. Quality gates and CI

### 18.1 Tooling

uv (env + lock), nox (sessions), ruff (lint + format), pyright strict, pytest, coverage,
hypothesis, respx, pytest-benchmark, pre-commit, Dependabot (pip + actions).

### 18.2 `ci.yml` on every PR

1. ruff check + format check
2. pyright strict
3. unasync check (§5.4)
4. tests: Python 3.10, 3.11, 3.12, 3.13, 3.14 × {ubuntu}; plus 3.12 × {macOS, windows}
5. minimum-dependency job (lowest supported httpx)
6. coverage gate (§15)
7. docs build + docs examples
8. benchmarks compare (non-blocking on PRs, blocking on release)
9. changelog fragment check

### 18.3 Security

`SECURITY.md` with private reporting via GitHub Security Advisories. CodeQL workflow. No
secrets in fixtures (recorder redaction + a CI grep for token patterns `00D[A-Za-z0-9]{12,15}!`).

### 18.4 `AGENTS.md` (for AI-driven development)

Must state: read this spec/ADRs first; `_sync/` is generated, edit `_async/` and run `nox -s unasync`;
`core/` has no I/O; every public change needs docs + changelog fragment + tests in both clients;
never log secrets; record new Salesforce responses with the recorder, never hand-write them unless
marked `synthetic`; verify Salesforce behaviour against official docs and add the source URL to the
ADR or test docstring.

---

## 19. Release plan

### 0.1 — core (target: first usable release)

Scope:
- Clients: `AsyncSalesforce`, generated `Salesforce`, `with_auth`, `tagged`.
- Auth: `WebServerFlow` (PKCE, exchange, revoke, userinfo, introspect), `ClientCredentials`, `JwtBearer` (`[jwt]`), `StaticToken`; `Token`, `TokenStore`, `InMemoryTokenStore`; full §6.4 algorithm.
- Transport: §7 complete.
- Errors: §8 complete including custom error tags.
- Areas: Records (§9.1), Query/Search + `soql` + Cursor (§9.2) incl. values in SOQL methods, `query_one`, `query_first`, `explain`, `esf.flatten`, Collections (§9.3), raw `request` (§9.5), `limits()` (§9.17).
- CLI: `esf query`, `whoami`, `limits`, `version`, and `esf migrate` (§9.19) so users moving off simple-salesforce have it from the first release.
- Request headers (D30, ADR 0012): fixes (no conditional headers on delete; `"; "` in `Sforce-Call-Options`); Collections write headers (V24 passed: `assignment_rule_id=`, `allow_save=`). Not shipped, moved to planned (2026-11-13): `DuplicateRules`, `default_namespace`, `update_mru`, `Warning` → `SalesforceWarning`.
- Usage (§10) and observability (§11) complete, stdlib observer; structlog/otel adapters.
- Testing module (§14) for the areas above.
- Docs: getting started, auth pages, records/query/collections guides, errors, logging, usage, migration guide (partial).
- Repo tooling, CI and release pipeline (§17–18).

Acceptance criteria:
- Every §21 item tagged *0.1* verified.
- Concurrency test: 100 callers, expired token → 1 token POST; with rotation enabled in a sandbox, no `invalid_grant` across 1,000 refresh cycles from 4 processes sharing a CAS store.
- `esf migrate` meets §9.19.5.
- A sample FastAPI app and a sample Flask app in `examples/` use only public APIs for query, records and collections, with tests passing.

### 0.2 — files, actions, integrations
Files, platform events publish, actions + Apex REST + `esf generate actions`, lead convert/merge
(SOAP), FastAPI and Flask integrations. Error hints and docs links (§8.5); CLI `describe`,
`request`, `generate models` (§9.18).

### 0.3 — Bulk, Tooling, GraphQL, Composite
Bulk 2.0 ingest/query, Tooling incl. execute anonymous (with `capture_log`), GraphQL, Composite/Graph/Tree/Batch builders.

### 0.4 — UI API, Reports, Metadata
### 0.5 — Pub/Sub API
Subscribe (§9.16) with CDC decoding, `ReplayStore`, typed errors, reconnects, `sf.pubsub.*`
events, the Pub/Sub fake and `replay_store_contract`, a vendored `.proto` with a drift check,
and a docs guide (subscribing, a replication recipe that handles gap events, moving from the
official Salesforce sample). Acceptance: V27 and V28 verified; a `tests/sandbox` run receives a
platform event and a CDC update on a sandbox, then resumes after a forced disconnect without
losing or duplicating events beyond one batch.
### 1.0 — stabilise
Interface freeze, per-version docs, migration guide complete, all
deprecations resolved.

### Unscheduled
- `sf.with_options(timeout=, retry=, headers=)`: deferred under D29 until users ask.
- A one-time warning when the pinned API version is close to Salesforce's retirement schedule.
- `esf.testing` data factories for sandbox tests, with cleanup after each test.
- `ManagedSubscribe` as `subscribe(..., managed_subscription=)`, once Salesforce makes it GA (§9.16.7).
- `sf.pubsub.publish` over gRPC: deferred under D29 until users ask (§9.16.7).
- **Outbound Messaging receiver** for `contrib.fastapi` / `contrib.flask`: parse the SOAP
  `notifications()` message with hardened XML (no entities), check `OrganizationId` against the
  client's org, de-duplicate on the notification `Id` (Salesforce may deliver more than once and
  out of order, retrying for up to 24 hours), return one ACK for the whole message, and redact
  the optional `SessionId` everywhere. Record-triggered flows can still send outbound messages
  (release 234), so the feature outlives workflow rules. Scheduled when users ask.

---

## 20. Migrating from simple-salesforce

The docs page contains this table (extended per release). No compatibility shim (D23).

| simple-salesforce | even-simpler-salesforce |
|---|---|
| `Salesforce(username=, password=, security_token=)` | `esf.Salesforce(esf.ClientCredentials(...))` or `JwtBearer` (password login is not supported) |
| `Salesforce(instance_url=, session_id=)` | `esf.Salesforce(esf.StaticToken(access_token, instance_url))` |
| `sf.Lead.get(id)` | `sf.sobject("Lead").get(id)` |
| `sf.Lead.create(data)` | `sf.sobject("Lead").create(data)` (returns the id, not a dict) |
| `sf.Lead.update(id, data)` | `sf.sobject("Lead").update(id, data)` |
| `sf.Lead.upsert("Ext__c/42", data)` | `sf.sobject("Lead").upsert("Ext__c", "42", data)` |
| `sf.Lead.describe()` | `sf.sobject("Lead").describe()` (cached) |
| `sf.query(q)` / `sf.query_more(url, True)` | `sf.query(q)` / `sf.query_more(page.cursor)` |
| `sf.query_all(q)` | `[r async for r in sf.query_iter(q)]` or `list(sf.query_iter(q))` |
| `sf.query_all_iter(q, include_deleted=True)` | `sf.query_iter(q, include_deleted=True)` |
| `format_soql(...)` | values in SOQL methods: `sf.query("... = {}", value)`; `esf.soql(...)` to build a query in pieces |
| a whole codebase | `esf migrate src/` rewrites the mechanical rows of this table and marks the rest (§9.19) |
| `sf.restful("composite/sobjects", method="PATCH", json=...)` | `sf.collections.update(...)` |
| `sf.restful(path, ...)` | `sf.request(method, path, ...)` |
| `sf.apexecute("path", method="POST", data=...)` | `sf.request("POST", "/services/apexrest/path", json=...)` (0.1); `sf.apex.post("/path", json=...)` from 0.2 |
| `sf.bulk.Lead.insert(rows)` (Bulk v1) | `sf.bulk.ingest("Lead", "insert", rows=rows)` (Bulk 2.0) |
| `sf.bulk2.Lead.query(q)` | `sf.bulk.query(q)` |
| `sf.toolingexecute(...)` | `sf.tooling.request(...)` / `sf.tooling.query(...)` |
| `sf.limits()` | `sf.limits()`; usage also in `sf.usage.snapshot()` for free |
| `SalesforceMalformedRequest` (inspect `.content`) | specific classes (`InvalidField`, `ValidationRuleFailed`, ...) with `.error_code`, `.fields` |
| `SalesforceExpiredSession` | handled automatically; `ReauthorizationRequired` if the refresh token is dead |
| `OrderedDict` results | plain `dict` |
| `sf.sf_version` | `sf.api_version` |
| `sf.sf_instance` / `sf.base_url` (to build links or URLs) | `sf.instance_url` (read-only; the instance of the current token, `None` before the first call) and `sf.request(...)` for calls |
| `sf.session_id`, `sf.headers`, `sf.session` (to make your own HTTP calls) | `sf.request(...)` / `sf.apex.*`; the access token is not public by design |

---

## 21. Verification backlog

Each item MUST be verified (official doc URL or recorded sandbox evidence linked in the ADR/test)
before the release named.

| # | Claim | Needed by | How |
|---|---|---|---|
| V1 | Token response has no `expires_in` | 0.1 | **Done** — *OAuth 2.0 Refresh Token Flow* (help) |
| V2 | Rotation invalidates the previous refresh token; reuse invalidates the chain | 0.1 | **Done** — same page. **Org-observed** (DE org, API v68.0, 2026-11-12): every refresh also revokes all earlier access tokens of the grant (11 refreshes: ATs 1–10 → 401, 11 → 200); the client adopts a newer stored token after a post-refresh 401 (§6.4). **Discrepancy:** reusing a rotated-out refresh token was accepted 8× before the chain was revoked; the article says "immediate revocation". The client never reuses one |
| V3 | "Token request is already being processed" on concurrent refresh | 0.1 | **Done** — same page. Still record the exact error body in a sandbox |
| V4 | Introspection response contains `exp` | 0.1 | **Done** (DE org, API v68.0, 2026-11-12): `exp` present; fields `active, client_id, exp, iat, nbf, scope, sub, token_type, username`; access token `exp - iat` = 7200 s. Evidence: `tests/fixtures/recorded/verification/2026-11-12.json` |
| V5 | Error code strings in §8.2 exist; HTTP status meanings | 0.1 | **Done** — *Core Data Types Used in API Calls* (`StatusCode`) and *Status Codes and Error Responses* (REST). Added 300/410/414/428/431; `REQUEST_LIMIT_EXCEEDED` is 403 and also covers concurrent limits |
| V5b | Validation rules / Flow custom errors / `addError()` → `FIELD_CUSTOM_VALIDATION_EXCEPTION`; unhandled trigger exception → `CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY` | 0.1 | **Done** (DE org, API v68.0, 2026-11-12): validation rule, Flow Custom Error and Apex `addError()` → 400 `FIELD_CUSTOM_VALIDATION_EXCEPTION` (`ValidationRuleFailed`); unhandled trigger exception → 400 `CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY` (`RecordSaveBlocked`). Evidence: `verification/2026-11-12.json` |
| V6 | Expired/invalid token → 401 with `INVALID_SESSION_ID` | 0.1 | **Done** — REST status page (401 = session ID or OAuth token expired/invalid, body has `errorCode`) and `StatusCode` (`INVALID_SESSION_ID` = malformed or expired) |
| V7 | Client credentials needs the My Domain token endpoint; no refresh token | 0.1 | **Done** — *OAuth 2.0 Client Credentials Flow* (help): login/test.salesforce.com "aren't supported"; "doesn't support refresh tokens" |
| V8 | Composite family limits | 0.1/0.3 | **Done** — Collections create 200; retrieve by POST 2,000 (GET ≈ 800 ids before HTTP 414); Composite 25 with ≤ 5 Collections/query subrequests; Graph 500; Tree 200 records / 5 types / 5 levels; Batch 25 |
| V9 | `Sforce-Query-Options` batch size | 0.1 | **Done** — default 2,000, min 200, max 2,000, not guaranteed |
| V10 | `Sforce-Limit-Info` format | 0.1 | **Done** — `api-usage=10018/100000; api-bursts=1/750` |
| V11 | SOAP API (Partner) still works with OAuth access tokens after `login()` retirement | 0.2 | **Done** (DE org, API v68.0, 2026-11-12): Partner SOAP `convertLead`/`merge` work with an OAuth token in `SessionHeader`; the retirement covers only `login()`. Evidence: `tests/sandbox/test_files_soap.py` |
| V12 | ContentVersion upload limit | 0.2 | **Done** — *Insert or Update Blob Data*: multipart 2 GB ContentVersion; non-multipart 37.5 MB base64 |
| V13 | Bulk 2.0 upload size and result locators | 0.3 | **Done** — 150 MB base64 per upload (≈100 MB raw); `locator`/`maxRecords`; same API version or 409 |
| V14 | Describe returns 304 with `If-Modified-Since` | 0.1 | **Done** — *sObject Describe* reference |
| V15 | Zensical renders mkdocstrings API reference | first docs release | **Done** — Zensical *MkDocs plugins* compatibility page (native mkdocstrings; no gen-files; use autoapi/api-autonav) |
| V16 | Pub/Sub host/port, auth metadata keys, flow control | 0.5 | **Done** — ports 443/7443, EU endpoint, `accesstoken`/`instanceurl`/`tenantid`, `num_requested` (default 100) |
| V17 | Tooling `executeAnonymous` result fields | 0.3 | **Done** (DE org, API v68.0, 2026-11-12): fields `line, column, compiled, success, compileProblem, exceptionStackTrace, exceptionMessage`; runtime exceptions take line/column from the stack trace. Fixtures `tooling/execute_*.json` |
| V18 | Bulk 2.0 CSV: empty cell = unchanged, `#N/A` = null; UTF-8 handling | 0.3 | **Done (org-observed)** (DE org, API v68.0, 2026-11-12): update: empty cell = unchanged, `#N/A` = null; insert / upsert-insert: both give the default or null; non-ASCII round-trips; a UTF-8 BOM fails the job (`Field name not found : \ufeffLastName`), so the client strips it. Evidence: `tests/sandbox/test_bulk.py` |
| V19 | External id values containing `.` (e.g. `x.json`) in `sobjects/{o}/{field}/{value}` are read as a format suffix | 0.1 | **Done (fix)** (DE org, API v68.0, 2026-11-12): values ending in lower-case `.json`, `.xml` or `.html` return 404 on `sobjects/{o}/{field}/{value}`, also with `%2E`; the client falls back to SOQL (get) or a Collections upsert. Evidence: `tests/sandbox/test_core_rest.py`, `sobject/external_id_suffix.json` |
| V20 | `Sforce-Call-Options: client=<name>` on REST calls: accepted on every resource, and where admins see it | 0.1 | **Partly done** (DE org, API v68.0, 2026-11-12): `client=` accepted on limits, query, search, describe, describe-global, collections retrieve, `/services/data/` and userinfo. Where admins see it remains a manual check |
| V21 | HTTP status and body for `UNSUPPORTED_API_VERSION` (configured version not offered by the org); message seen elsewhere: "Invalid Api version specified on URL" | 0.1 | **Done (discrepancy)** (DE org, API v68.0, 2026-11-12): a version newer than the org's returns 404 `NOT_FOUND`, not `UNSUPPORTED_API_VERSION`; retired versions are refused by the client before sending. Fixture `errors/api_version_not_offered.json` |
| V22 | `GET query?explain=` response fields (`plans[]`: `cardinality`, `fields`, `leadingOperationType`, `relativeCost`, `sobjectCardinality`, `sobjectType`, `notes`) | 0.1 | **Done** (DE org, API v68.0, 2026-11-12): `plans[]` fields as listed; `leadingOperationType` `Index`/`TableScan`; ordered by cost. Fixture `query/explain.json` (recorded) |
| V23 | Debug-log capture for anonymous Apex: `TraceFlag`/`DebugLevel` fields and permissions, `ApexLog` body endpoint, fields to match the log to the run | 0.3 | **Done** (DE org, API v68.0, 2026-11-12): log `Operation` is the request path, `Request=Api`; queryable within ~0.3 s; a second overlapping `USER_DEBUG` flag → 400 `FIELD_INTEGRITY_EXCEPTION`. Non-admin permissions not tested. Evidence: `tests/sandbox/test_tooling_metadata.py` |
| V24 | `Sforce-Auto-Assign` and `Sforce-Duplicate-Rule-Header` take effect on sObject Collections create/update/upsert | 0.1 | **Done (shipped)** (DE org, API v68.0, 2026-11-12): `Sforce-Auto-Assign` and `allowSave` take effect via `composite/sobjects`; Collections `create/update/upsert` take `assignment_rule_id=` and `allow_save=` |
| V25 | `If-Unmodified-Since` on `DELETE sobjects/{o}/{id}` is ignored (record deleted) or rejected | 0.1 | **Done (discrepancy)** (DE org, API v68.0, 2026-11-12): not ignored — a past date gives 500 `UNKNOWN_EXCEPTION`, a future one 400 `BAD_HEADER`; the record is kept. Fixture `errors/delete_if_unmodified_since.json` |
| V26 | Namespace prefix format accepted by `defaultNamespace` | 0.1 | **Not testable** in an org without a namespace: the DE org accepts and ignores any value. Needs an org with a registered namespace or a managed package |
| V27 | CDC bitmap bit order (bit 0 = least-significant bit of the hex value) and compound `{pos}-0x…` decoding | 0.5 | **Done** (DE org, API v68.0, 2026-11-12): bit 0 = least-significant bit; compound entries `{pos}-0x…`; a nulled-only compound field sends no leading `0x…` entry. Fixture `pubsub/events_account_change_update.json` |
| V28 | How Pub/Sub Avro schemas mark Date/DateTime fields (logicalType or otherwise), so epoch values are converted only where the type is known | 0.5 | **Done (org-observed)** (DE org, API v68.0, 2026-11-12): DateTime is a `long` (epoch ms) with `doc` `…:DateTime[:00N…]`; no logical types. Fixtures `pubsub/schema_*.json` |
| V29 | Error hints (§8.5): exact `INVALID_FIELD` message shapes ("No such column 'X' on entity 'Y'" for queries, "... on sobject of type Y" for DML), the `API_DISABLED_FOR_ORG` body, and the common causes of `INVALID_SESSION_ID` right after a successful refresh (missing `api` scope, IP or session policies) | 0.2 | **Partly done** (DE org, API v68.0, 2026-11-12): query "No such column 'X' on entity 'Lead'…", DML "No such column 'X' on sobject of type Lead" (both 400); bad token → 401 `INVALID_SESSION_ID`; post-refresh 401s explained by rotation (V2). `API_DISABLED_FOR_ORG` not reproducible (stays synthetic) |
| V30 | `esf describe` "required" column = `nillable=false`, `createable=true`, `defaultedOnCreate=false`; `restrictedPicklist` and `picklistValues[].active` in REST describe | 0.2 | **Done** (DE org, API v68.0, 2026-11-12): the `required` rule matches Lead and a custom object; REST describe omits inactive picklist values, so `active` is always true. Fixtures `sobject/describe_lead.json`, `describe_custom_object.json` |
| V31 | Invocable action invocation: HTTP status when an invocation fails (400 with the same result list when all fail? 200 for a mix?) and the `errors[]` entry keys (`statusCode`, `message`, `fields`) | 0.2 | **Done (org-observed)** (DE org, API v68.0, 2026-11-12): all inputs fail → 400 with the result list; partial Flow failure → 200; an Apex exception fails every input (`APEX_FAILED`); `errors[]` keys `statusCode, message, fields`. Fixtures `tests/fixtures/recorded/actions` |
| V32 | Action results come back in input order (meaning of `sortOrder`), and the limit on inputs per request for standard, Flow and Apex actions | 0.2 | **Done (org-observed)** (DE org, API v68.0, 2026-11-12): results in input order; `sortOrder` is the index for Apex, -1 otherwise; Apex and Flow accept at most 2,000 inputs (with 2,001 the first 2,000 run and the extra fails `UNKNOWN_EXCEPTION`). Standard-action limit unverified |
| V33 | Action describe shape: `inputs[]`/`outputs[]` with `name`, `type` (upper-case values such as `STRING`, `BOOLEAN`, `SOBJECT`), `required`, `maxOccurs`; list response `{"actions": [{name, label, type, url}]}` | 0.2 | **Done** (DE org, API v68.0, 2026-11-12): list `{"actions":[{label,name,type,url}]}` with upper-case `type`; describe inputs/outputs shapes recorded; `esf generate actions` run against the org |
| V34 | Apex REST: unhandled exception → HTTP 500 `[{"errorCode": "APEX_ERROR", ...}]`; a URL no class maps → 404 `NOT_FOUND` | 0.2 | **Done** (DE org, API v68.0, 2026-11-12): unhandled exception → 500 `APEX_ERROR`; unmapped URL → 404 `NOT_FOUND`; both raise `ApexError` |
| V35 | Partner SOAP `convertLead`/`merge` wire shape: `LeadConvert` element order (alphabetical, as in the Partner WSDL), `merge` parameter name `request`, `masterRecord` as an `urn:sobject` sObject; faults arrive as HTTP 500 with `detail/*Fault/exceptionCode`; `INVALID_SESSION_ID` is a fault (not a 401) on an OAuth token | 0.2 | **Done** (DE org, API v68.0, 2026-11-12): shapes match the WSDL; refused rows are `success=false` in a 200; call errors are 500 faults with `exceptionCode`; a bad session is a 500 `INVALID_SESSION_ID` fault (treated like a 401). Out-of-order `LeadConvert` elements fail the row (`UNKNOWN_EXCEPTION`), org-observed |
| V36 | Platform event publish over REST: `POST sobjects/{Event__e}` returns 201 with `success: true` and one `OPERATION_ENQUEUED` entry in `errors` whose `message` is the EventUuid; `composite/sobjects` accepts events with the same per-row shape; the HTTP status and body of `LIMIT_EXCEEDED` and `PLATFORM_EVENT_PUBLISHING_UNAVAILABLE` | 0.2 | **Partly done** (DE org, API v68.0, 2026-11-12): 201 with `OPERATION_ENQUEUED` carrying the EventUuid; `composite/sobjects` same per row; bad field 400 `INVALID_FIELD`. `LIMIT_EXCEEDED` / `PLATFORM_EVENT_PUBLISHING_UNAVAILABLE` statuses still synthetic |
| V37 | Multipart `POST sobjects/ContentVersion`: part names `entity_content` / `VersionData`, a quoted `boundary="..."` parameter, and whether Salesforce accepts `Transfer-Encoding: chunked` (used for iterator and non-seekable uploads, where the size is unknown) | 0.2 | **Done** (DE org, API v68.0, 2026-11-12): part names and quoted boundary accepted; both `Content-Length` and chunked uploads → 201 |
| V38 | `GET sobjects/ContentVersion/{id}/VersionData` and `Attachment/{id}/Body` with `Accept: */*`: response `Content-Type`, `Content-Length`, gzip, and the error body for a missing id | 0.2 | **Done (org-observed)** (DE org, API v68.0, 2026-11-12): `VersionData` is `application/octetstream`, usually with `Content-Length` (one run answered chunked), no gzip (small file); `Attachment` Body uses its own ContentType; missing id → 404 `NOT_FOUND` |
| V39 | Share type and visibility of the `ContentDocumentLink` that Salesforce creates for `FirstPublishLocationId` on a record | 0.2 | **Done (org-observed)** (DE org, API v68.0, 2026-11-12): the link created for `FirstPublishLocationId` has `ShareType=V`, `Visibility=AllUsers` |
| V40 | Bulk 2.0 upload `PUT jobs/ingest/{id}/batches` accepts `Content-Encoding: gzip`, and the 150 MB limit applies to the compressed or the uncompressed body | 0.3 | **Done** (DE org, API v68.0, 2026-11-12): `Content-Encoding: gzip` accepted (sized and chunked); the client gzips parts over 64 KiB. Whether the 150 MB limit counts compressed size is untested (the client splits at 100 MB raw) |
| V41 | Format of the `sf__Error` cell in Bulk 2.0 failed results (`STATUS_CODE:message:Field1,Field2 --`) and of `sf__Created` (`true`/`false`) | 0.3 | **Done (fix)** (DE org, API v68.0, 2026-11-12): `CODE:message:Field1 Field2 --` (fields separated by spaces), `:--` without fields; `sf__Created` is `"true"`/`"false"` |
| V42 | How a Bulk query that selects a compound field fails: `400` on job creation (which `errorCode`) or a job in state `Failed` with an `errorMessage` | 0.3 | **Done** (DE org, API v68.0, 2026-11-12): 400 `API_ERROR` "Selecting compound data not supported in Bulk Query" at job creation (no job); the client adds a hint |
| V43 | `resultPages` response field name `nextRecordsUrl` (example) or `nextRecordUrl` (field table), and whether its `resultUrl` paths keep the job's API version | 0.3 | **Done (fix, discrepancy)** (DE org, API v68.0, 2026-11-12): v63.0+ `resultPages[].resultUrl` (full path, job version); v58–62 `resultChunks[].resultLink` (relative); field is `nextRecordsUrl`. The client reads both |
| V44 | `apiVersion` in Bulk 2.0 job info is a JSON number (`68.0`) and `createdDate`/`systemModstamp` use `2026-11-10T10:00:00.000+0000` | 0.3 | **Done** (DE org, API v68.0, 2026-11-12): `apiVersion` is a JSON number; dates like `2026-11-11T22:38:50.000+0000` |
| V45 | `GET tooling/executeAnonymous/?anonymousBody=` response: `line`/`column` are `-1` when there is no position, empty `compileProblem`/`exception*` are `null`; the longest body the URL accepts (414 above it) | 0.3 | **Done (fix)** (DE org, API v68.0, 2026-11-12): no position → -1, empty fields `null`; ~18,500 encoded characters run; longer bodies get HTML 431/414 from the web tier, now `RequestTooLarge` |
| V46 | `POST tooling/runTestsAsynchronous/` with `{"classids": "id,id"}` returns the `AsyncApexJob` id as a JSON string (HTTP 200) | 0.3 | **Done** (DE org, API v68.0, 2026-11-12): HTTP 200, body a JSON string with the `707…` job id |
| V47 | `GET tooling/completions?type=apex\|visualforce` response shape (`publicDeclarations` keyed by namespace) | 0.3 | **Done** (DE org, API v68.0, 2026-11-12): `apex` → `publicDeclarations` keyed by namespace (~29 MB, 30 s+); `visualforce` → `completions` |
| V50 | GraphQL error responses: `200 OK` with `errors[]` for an unknown object or field; each entry has `message`, `locations`, `path` and `extensions.ErrorType` (`InvalidSyntax`, `ValidationError`, ...); the HTTP status and body for a syntax error; whether a successful response carries `"errors": []` | 0.3 | **Done (fix)** (DE org, API v68.0, 2026-11-12): all errors are 200 with `data: {}`; entries have `message` (HTML-escaped), `locations`, `paths` (not `path`) and `extensions.ErrorType`; the client reads `paths` and unescapes |
| V51 | GraphQL partial data: whether Salesforce returns `data` next to `errors` (for example a field the user can't read), and the `path` it reports. `sf.graphql` raises `GraphQLError` with `.data` either way | 0.3 | **Done (org-observed)** (DE org, API v68.0, 2026-11-12): a field without FLS is a `ValidationError` with no data; partial data only with `DataFetchingException` (failed field `null`) |
| V52 | GraphQL paging: `first` maximum and default (10), `after: endCursor`, `pageInfo.hasNextPage`, and the record limit without `upperBound` (4,000; more with `upperBound` from v60.0) | 0.3 | **Done** (DE org, API v68.0, 2026-11-12): `first` default 10, max 2,000 with `upperBound`; offset + `first` ≤ 4,000 without it; `after`/`pageInfo` as documented |
| V53 | GraphQL mutations (`mutation { uiapi { LeadCreate ... } }`): the API version they need and whether a timed-out mutation may have committed (the client never resends one) | 0.3 | **Discrepancy** (DE org, API v68.0, 2026-11-12): mutations accepted from v54.0 (docs: v59.0+); `allOrNone` via `uiapi(input: …)`, default true; failures give the cause plus rollback entries |
| V55 | Composite response: top-level 200 with one `compositeResponse` entry per subrequest (`body`, `httpHeaders`, `httpStatusCode`, `referenceId`); with `allOrNone` the other subrequests report 400 `PROCESSING_HALTED`; `@{ref.field}` works in the URL, in the body and inside a quoted SOQL literal of a `query?q=` URL | 0.3 | **Done** (DE org, API v68.0, 2026-11-12): one entry per subrequest; with allOrNone the others get 400 `PROCESSING_HALTED` and `raise_for_errors` raises the real cause |
| V56 | The Composite "at most 5 of 25 sObject Collections or query operations" rule: which subrequests count. The client counts `composite/sobjects...`, `query`, `queryAll` and `query/{locator}` | 0.3 | **Done** (DE org, API v68.0, 2026-11-12): `query`, `queryAll`, `query/{locator}` and `composite/sobjects` count; the 6th gets 400 `PROCESSING_HALTED` |
| V57 | Composite Graph: response `graphs[]` with `graphId`, `isSuccessful` and `graphResponse.compositeResponse`; a failed graph rolls back only itself; query subrequests are rejected in a graph | 0.3 | **Done** (DE org, API v68.0, 2026-11-12): `graphs[{graphId, graphResponse.compositeResponse, isSuccessful}]`; a failed graph rolls back only itself; query subrequests → 404 `OPERATION_NOT_ALLOWED` |
| V58 | sObject Tree: 201 `{hasErrors: false, results: [{referenceId, id}]}`; on failure 400 `{hasErrors: true, results: [{referenceId, errors: [{statusCode, message, fields}]}]}` with nothing saved; 200 records, 5 types and 5 levels across the request | 0.3 | **Done** (DE org, API v68.0, 2026-11-12): 201 `{hasErrors:false, results:[{referenceId,id}]}`; failure 400 lists only the failed records; nothing saved |
| V59 | Batch: subrequest `url` starts at `vXX.X/`, writes take `richInput`; response `{hasErrors, results: [{statusCode, result}]}`; skipped subrequests with `haltOnError` get 412 `BATCH_PROCESSING_HALTED` | 0.3 | **Done** (DE org, API v68.0, 2026-11-12): `vXX.X/…` and full paths both work; `richInput`; failed `result` is `[{errorCode,message}]`; halted subrequests 412 `BATCH_PROCESSING_HALTED` |
| V60 | UI API list views of an object: `GET ui-api/list-info/{objectApiName}` (not the deprecated `list-ui/{objectApiName}`), the `lists[]` entry keys (`apiName`, `id`, `label`, `url`) and its paging tokens | 0.4 | **Done** (DE org, API v68.0, 2026-11-12): `lists[]` with `apiName, id, label, url`; `__Recent` first. Fixture `ui/list_views_lead.json` |
| V61 | UI API errors: HTTP status and body for an unknown object on `object-info` (the client maps `NOT_FOUND` there to `InvalidType`), a record the user can't see (404 `NOT_FOUND`?), an unreadable or unknown field in `fields` (code?), and a malformed id (400 `ILLEGAL_QUERY_PARAMETER_VALUE`) | 0.4 | **Done (org-observed)** (DE org, API v68.0, 2026-11-12): unknown object 403 `INSUFFICIENT_ACCESS`; unsupported object 400 `INVALID_TYPE`; missing record 404; malformed id 400 `UNKNOWN_EXCEPTION`. Fixtures `ui/error_*.json` |
| V62 | `If-Modified-Since` on `ui-api/object-info/{o}` and `.../picklist-values/{rt}`: answered with 304, ignored (200), or rejected. The describe cache revalidates with it; 200 and 304 both work, a 4xx would not | 0.4 | **Done (org-observed)** (DE org, API v68.0, 2026-11-12): `If-Modified-Since` ignored (always 200); `If-None-Match` with the ETag gives 304, so the describe cache also sends `If-None-Match` (2026-11-13) |
| V63 | `GET ui-api/records/{id}`: from API 45.0 one of `fields`, `optionalFields`, `layoutTypes` is required; whether `fields` with `layoutTypes` also needs `childRelationships` (one search excerpt says so); `modes` defaults to `View` | 0.4 | **Done (fix)** (DE org, API v68.0, 2026-11-12): no parameter → 400 `INVALID_INPUT_COMBINATION`; `fields` + `layoutTypes` rejected, so `sf.ui.record()` raises `ValueError` |
| V64 | UI API response shapes the docs and the fake rely on: record `fields` as `{value, displayValue}`, `record-defaults/create` and `clone` with `record`, `layout`, `objectInfos` (clone `record.id` is `null`), `list-records` with `records`, `count`, `nextPageToken` (`null` on the last page), `pageSize` 1 to 2,000 (default 50), `picklistFieldValues` with `values`, `defaultValue`, `controllerValues`, `validFor` | 0.4 | **Done (fix)** (DE org, API v68.0, 2026-11-12): record and page shapes recorded; `displayValue` null for text; the `esf.testing` fake aligned |
| V65 | Report run with filters: `POST analytics/reports/{id}?includeDetails=` with `{"reportMetadata": {"reportFilters": [...]}}` runs the report with those filters instead of the saved ones, changes nothing in the org (so the client resends it like a read), and a synchronous run returns at most 2,000 detail rows with `allData: false` | 0.4 | **Done** (DE org, API v68.0, 2026-11-12): filters in `reportMetadata` apply to the run only; sync runs stop at 2,000 rows (`allData: false`) |
| V66 | Asynchronous runs: `POST .../instances` returns `id` and `status` at the top level; `GET .../instances/{id}` returns `attributes.status` (`New`, `Running`, `Success`, `Error`) and the results only on `Success`; the body of a `Running` and an `Error` instance; `404` for an unknown instance id | 0.4 | **Done** (DE org, API v68.0, 2026-11-12): instance `New` → `Success` shapes recorded; unknown instance 404. `Running`/`Error` fixtures stay synthetic |
| V67 | Fact map details the row helpers rely on: detail `rows` sit only in leaf entries (deepest row grouping `!` deepest column grouping); empty matrix cells are left out; a currency cell's `value` is `{"amount", "currency"}`; a date grouping's `value` is an ISO date | 0.4 | **Done (discrepancy, corrected)** (DE org, API v68.0, 2026-11-12): rows only in leaves; empty matrix cells are sent; currency detail cells are objects, aggregates plain numbers |
| V68 | Dashboards: `PUT analytics/dashboards/{id}` with no body starts a refresh and returns `id` and `statusUrl` (HTTP status not stated); `GET` returns `dashboardMetadata`, `componentData[].reportResult` in the report run format and `componentData[].status.refreshStatus` (`IDLE` or `RUNNING`) | 0.4 | **Done (fix)** (DE org, API v68.0, 2026-11-12): `PUT` → 201 `{"statusUrl"}` only; `reportResult` null until the first refresh; fake and docs fixed |
| V69 | Error responses of the Reports API: the status and `errorCode` for a report the user can't see, a malformed filter, and a refresh of a dashboard that is already refreshing (or refreshed too often) | 0.4 | **Partly done** (DE org, API v68.0, 2026-11-12): unknown report 404; bad filter 400 `BAD_REQUEST`; unknown dashboard 400 `INVALID_CROSS_REFERENCE_KEY`; refresh conflicts 409/403. A report the user can't access needs a second user |
| V70 | REST deploy: `POST metadata/deployRequest` multipart part names `json` (body `{"deployOptions": {...}}`) and `file` (`application/zip`); response `201` with `id` and `deployResult`; the 39 MB / 10,000-file zip limits; whether Salesforce accepts a quoted `boundary="..."` | 0.4 | **Done** (DE org, API v68.0, 2026-11-12): multipart `json` + `file` with a quoted boundary → 201 `Pending`. Size limits untested |
| V71 | `GET metadata/deployRequest/{id}?includeDetails=true`: the `deployResult` keys, the `status` values (including `FinalizingDeploy`, `FinalizingDeployFailed`, `Canceling`), whether `details.componentFailures` and `runTestResult.failures` arrive as one object instead of a list of one, and the error body of an unknown id | 0.4 | **Done (fix, discrepancy)** (DE org, API v68.0, 2026-11-12): lists come back as lists; unknown id 400 `INVALID_CROSS_REFERENCE_KEY`. With `rollbackOnError` off a failed test validation returns `success=true`; the client counts it as failed |
| V72 | SOAP Metadata API with an OAuth access token: `/services/Soap/m/{v}` accepts it in `SessionHeader`; envelope shapes of `retrieve` (`retrieveRequest` with `apiVersion`, `singlePackage`, `unpackaged`), `listMetadata` (`queries`, `asOfVersion`) and `describeMetadata`; an expired token is a `INVALID_SESSION_ID` fault (HTTP 500), not a 401 | 0.4 | **Done** (DE org, API v68.0, 2026-11-12): OAuth token accepted in `SessionHeader`; bad session and `INVALID_TYPE` are 500 faults; faults are no longer retried |
| V73 | `checkRetrieveStatus(id, includeZip)`: `includeZip=false` returns the state without the zip (API 34.0+); the zip is deleted from the server once returned, so a second call with `includeZip=true` fails; `status` values `Pending`, `InProgress`, `Succeeded`, `Failed`; `messages` for missing members | 0.4 | **Done** (DE org, API v68.0, 2026-11-12): Pending → Succeeded; missing members are `messages`; after the zip is returned both forms fault `INVALID_LOCATOR` |
| V74 | Retrieve zip contents: entry names are relative with `/` separators and `package.xml` at the root with `singlePackage=true`; no directory entries or symlink attributes; the size at which a retrieve fails (39 MB compressed and 400 MB uncompressed from memory of *Metadata API Limits*, not re-read) | 0.4 | **Done** (DE org, API v68.0, 2026-11-12): relative `/` names, `package.xml` at the root, no directory entries. Size limit untested |
| V75 | Pub/Sub trailing metadata: the key of the error code (`error-code`) and of the RPC id (`rpc-id`, or only the `rpc_id` field of responses), and that every §9.16.5 code arrives with the gRPC status listed there | 0.5 | **Done** (DE org, API v68.0, 2026-11-12): trailers `error-code` and `rpc-id`; 7 error kinds recorded (`errors.json`) |
| V76 | Idle close: the gRPC status Salesforce returns when a stream gets no `FetchRequest` for 60 s while nothing is pending, and the keepalive interval (empty `FetchResponse` within 270 s) | 0.5 | **Done (org-observed)** (DE org, API v68.0, 2026-11-12): with nothing pending the stream ends `OK` after ~68 s; keepalives ~every 120 s |
| V77 | Date (not DateTime) fields in Pub/Sub Avro payloads: wire type (`int` days, `long` ms or `string`) and how the schema marks them | 0.5 | **Done (fix, org-observed)** (DE org, API v68.0, 2026-11-12): Date is a `long` (epoch ms at midnight UTC) with `doc` `Data:DateOnly:00N…`; decoded to `date` |
| V78 | `GAP_OVERFLOW` events: the `recordIds` value (`000000000000000AAA`), whether `entityName` is always set, and whether overflow events arrive on `/data/ChangeEvents` as well as per-entity channels | 0.5 | **Not testable** on Developer Edition: needs >100,000 changes in one transaction (exceeds DE storage); needs a sandbox with enough storage |
| V79 | Pub/Sub acceptance on a sandbox (§19, 0.5): `tests/sandbox/test_pubsub.py` receives a platform event and a CDC update, then resumes after a forced disconnect without losing events or repeating more than one batch, for both clients | 0.5 | **Done (discrepancy)** (DE org, API v68.0, 2026-11-12): `tests/sandbox/test_pubsub.py` 10/10 on both clients. Change events carry a stable `event.id`; the Durability page says platform events only |
| V80 | A JSON `null` value in `PATCH sobjects/{o}/{id}` (and Collections update) clears the field; a key left out of the body keeps its value. The records guide relies on it to teach `model_dump(by_alias=True, exclude_unset=True)` | 0.4 | **Done (org-observed)** (DE org, API v68.0, 2026-11-12): JSON `null` clears the field and an omitted key keeps it, on PATCH and the Collections update |
| V81 | Bulk API 2.0 query jobs and `FIELDS(STANDARD)`: *Create a Query Job* (api_asynch, `query_create_job.htm`) says `FIELDS()` isn't supported; the SOQL *SELECT* page (`sforce_api_calls_soql_select_fields.htm`) lists Bulk API 2.0 as supporting `FIELDS(STANDARD)` only. The client rejects only `FIELDS(ALL)`/`FIELDS(CUSTOM)` | 0.4 | **Done (org-observed)** (DE org, API v68.0, 2026-11-12): `FIELDS(STANDARD)` works on objects without compound fields and fails like V42 otherwise; `FIELDS(ALL|CUSTOM)` 400 |
| V82 | Bulk API 2.0 query job info (`GET jobs/query/{id}`) has `numberRecordsProcessed`, updated while the job is `InProgress`, and no `numberRecordsFailed`. `sf.bulk.job` events report `records_failed=None` for query jobs | 0.4 | **Done** (DE org, API v68.0, 2026-11-12): `numberRecordsProcessed` 0 then the total while `InProgress`; no `numberRecordsFailed` |
| V85 | Cancel a deployment over REST (`PATCH metadata/deployRequest/{id}`, body `{"deployResult": {"status": "Canceling"}}`): the HTTP status and body of a successful request (the client parses it as a deploy result with status `Canceling` or `Canceled`), and the error a deployment in `FinalizingDeploy` gets from API 65.0. The client maps a 4xx whose message mentions "finaliz" (as the SOAP `cancelDeploy()` `INVALID_ID_FIELD` fault does) and a 2xx that still says `FinalizingDeploy` to `DeployNotCancelable` | 0.4 | **Done** (DE org, API v68.0, 2026-11-12): PATCH → 202 `Canceling` → `Canceled`; cancelling a finished deployment returns its final state. `FinalizingDeploy` not reached; a quick deploy reported the undocumented status `Finalizing` (now accepted as in progress) |
| V86 | Quick deploy over REST (`POST metadata/deployRequest/{validatedId}`, body `{"validatedDeployRequestId": id}`): the response has the shape of the `POST metadata/deployRequest` response (`id` of the new deployment, `deployResult`), and the error bodies for a validation that failed, ran no tests, is older than 10 days, or was followed by another deployment | 0.4 | **Done** (DE org, API v68.0, 2026-11-12): 201 `{id, validatedDeployRequestId}`; `NoTestRun` validations and stale validations → 400 `INVALID_ID_FIELD` |
| V87 | `GET analytics/reports` (*Report List*, `sforce_analytics_rest_api_recentreportslist.htm`) returns a JSON array of at most 200 recently viewed reports, each with `id`, `name`, `url`, `describeUrl` and `instancesUrl`; `sf.reports.recent()` returns the entries as sent | 0.4 | **Done** (DE org, API v68.0, 2026-11-12): array of recent reports with an extra `fieldsUrl` (org-observed) |
| V88 | Bulk API 2.0 job handles: `GET jobs/query/{id}` and `GET jobs/ingest/{id}` with a client API version other than the one that created the job (the docs name 409 only for results), what `GET jobs/query/{id}` returns for an ingest job id (and the reverse), and `Sforce-NumberOfRecords` on a results page with `maxRecords` | 0.5 | **Done** (DE org, API v68.0, 2026-11-12): a v60.0 client opens a v68.0 query job (no 409; `apiVersion` stays 68.0); the query id on `jobs/ingest/{id}` is 404 `NOT_FOUND`; `maxRecords=1` pages send `Sforce-NumberOfRecords: 1`; a saved locator resumes. The reverse kind (ingest id on `jobs/query`) was not tried |

Remaining open (after the real-org run of 2026-11-12, issue #33): V3 (record the exact error body; the probe can revoke the token chain), V20 (where admins see `client=`), V26 (needs a namespace), V29 (`API_DISABLED_FOR_ORG`), V36 (`LIMIT_EXCEEDED` / `PLATFORM_EVENT_PUBLISHING_UNAVAILABLE` statuses), V69 (a report the user can't access: needs a second user) and V78 (needs a sandbox with enough storage). Untested limits are noted in their rows (V40, V70, V74, V85).

Note: developer.salesforce.com blocks non-browser clients (403). In a real browser, each docs page
has a Markdown version at the same path with `.md` instead of `.html` (fetch it from the page's
origin). help.salesforce.com needs JavaScript to render.

---

## 22. Licence and governance

- **Licence:** Apache-2.0 (O3). `LICENSE` holds the unmodified text. `NOTICE`:
  ```
  even-simpler-salesforce
  Copyright 2026 Utsav Singh
  ```
- Contributors keep copyright on their contributions and license them under Apache-2.0 (section 5 of the licence). No CLA. Commits carry a DCO `Signed-off-by:` line, checked by CI.
- **Maintainer:** Utsav Singh, sole maintainer (O14).
- **Repository:** https://github.com/Uttsavv/even-simpler-salesforce
- **Docs:** https://uttsavv.github.io/even-simpler-salesforce/ (GitHub Pages, source: GitHub Actions)
- **PyPI:** https://pypi.org/project/even-simpler-salesforce/
- **Trademark:** "Salesforce" is a trademark of Salesforce, Inc.; README states the project is not affiliated with or endorsed by Salesforce.

---

## Appendix A: Salesforce facts confirmed during design

From the **REST API Developer Guide**, version 68.0 (Winter '27), "Headers" chapter
(`resources.docs.salesforce.com/latest/latest/en-us/sfdc/pdf/api_rest.pdf`; HTML:
`developer.salesforce.com/docs/platform/api-rest/guide/headers.html`), read 2026-11-09:

- *Assignment Rule Header*: `Sforce-Auto-Assign`: `TRUE`, `FALSE` (case-insensitive) or an
  AssignmentRule id; default `TRUE`; applies to Accounts, Cases, Leads, including ones written
  indirectly (e.g. by a trigger). No resource restriction stated.
- *Call Options Header*: `Sforce-Call-Options` with `client` and `defaultNamespace` ("Not supported
  in Bulk API"); usable with sObject Basic Information, sObject Rows, sObject Rows by External ID,
  Query, QueryAll, Search, Bulk API and Bulk API 2.0. Example:
  `client=caseSensitiveToken; defaultNamespace=battle`. With a default namespace the response omits
  the prefix; `defaultNamespace` is ignored for describe.
- *Conditional Request Headers*: `If-Match`/`If-None-Match` (ETag) only for sObject Rows on
  **Account**; `If-Modified-Since`/`If-Unmodified-Since` for sObject Rows, sObject Describe,
  Describe Global, Invocable Actions; invalid values on PATCH/POST → 400; `If-Range` unsupported;
  **"DELETE requests aren't supported"**.
- *Duplicate Rule Header*: API 52.0+; `allowSave`, `includeRecordDetails`, `runAsCurrentUser`, all
  default `false`. No resource restriction stated.
- *MRU Header*: `Sforce-Mru: updateMru=true|false`, API 60.0+, default `true`; for sObject Basic
  Information (POST), sObject Rows, sObject Rows by External ID, sObject Relationships; SOQL never
  updates the MRU. Salesforce recommends `false` for API-only integration users.
- *Package Version Header*: `x-sfdc-packageversion-[namespace]: xx.x`.
- *Query Options Header*: `batchSize` default 2,000, min 200, max 2,000, not guaranteed (V9).
- *Warning Header*: `Warning: <code> <message>`; code `299` for deprecated API versions.

From the official Salesforce Help page **"OAuth 2.0 Refresh Token Flow for Renewed Sessions"**
(`help.salesforce.com/s/articleView?id=xcloud.remoteaccess_oauth_refresh_token_flow.htm`), read 2026-11-09:

- "The connected app's session timeout value determines when an access token is no longer valid and when to apply for a new one using a refresh token."
- Example response fields: `id`, `issued_at`, `instance_url`, `signature`, `access_token`, `token_type`, `scope` (no `expires_in`). With rotation enabled the response also includes `refresh_token`.
- Rotation: "the connected app issues a new refresh token along with the access token each time the flow is invoked. The previous refresh token is automatically invalidated." "If someone tries to use a refresh token that's been rotated out, Salesforce invalidates the current refresh token and any associated access tokens. To get a new refresh token, the client must complete a new flow."
- "Avoid sending simultaneous requests that contain the same refresh token. ... some of the requests fail intermittently and the Status column in the Login History displays Failed: Token request is already being processed. ... cache and reuse tokens ... develop a way to retry the requests when this error occurs."
- Client credentials may be sent in the body or as HTTP Basic; if both, the body wins.

From **"OpenID Connect Token Introspection"** (`...id=xcloud.remoteaccess_oidc_token_introspection_endpoint.htm`):

- Endpoint `POST https://<host>/services/oauth2/introspect`, form params `token`, optional `token_type_hint` (`access_token`/`refresh_token`), optional `child_sessions=all`; client id/secret via Basic or body.
- Response fields are not listed; the page refers to RFC 7662 (where `exp` is optional) → V4.

From the **Pub/Sub API Developer Guide** (`developer.salesforce.com/docs/platform/pub-sub-api/`),
read in a browser 2026-11-09 (sources for §9.16 and ADR 0013):

- *Subscribe RPC Method*: `num_requested` max 100. With nothing pending, the client must send a
  `FetchRequest` within 60 s. With events pending, the server sends keepalives (an empty batch
  carrying `latest_replay_id`) within 270 s, and Salesforce recommends saving that replay id.
  Only the first `FetchRequest` sets the replay position. Events are retained 3 days.
- *ManagedSubscribe RPC Method*: labelled **Beta**. The client commits replay ids; it must commit
  within 30 min and request within 60 s.
- *Event Deserialization Considerations*: `changedFields`/`diffFields`/`nulledFields` are hex
  bitmaps over the Avro schema field positions, with `{pos}-{bitmap}` entries for compound
  fields. Bits for fields without permission are cleared. Dates arrive as epoch numbers. Byte
  fields such as `replay_id` are little-endian.
- *Event Message Durability*: 72-hour retention. Replay ids are not contiguous and not unique
  across org migrations. Use the event `id` (platform events only) to identify a message. (A recorded change event
  carried an id too; see §9.16.3.)
- *Handle Errors* / *Retry Long-Lived RPC Calls*: the custom code is in the `error-code` trailer
  and an `rpcId` is appended. `replayid.corrupted` means the replay id is invalid or outside
  retention. `org.migrated` needs retries at least 30 minutes apart.
- *Allocations*: about 3 MB recommended per publish batch (4 MB gRPC limit), 1 MB per event, at
  most 200 events per publish recommended, 1,000 concurrent streams per channel, 200 managed
  subscriptions per org.
- *Authentication*: metadata `accesstoken`, `instanceurl`, `tenantid`.

From the **Change Data Capture Developer Guide** (`…/change-data-capture/guide/`): gap events
(`GAP_CREATE`/`UPDATE`/`DELETE`/`UNDELETE`) carry header data and record ids but no fields.
`GAP_OVERFLOW` covers changes beyond 100,000 in one transaction, one event per entity, with
record id `000000000000000AAA`. Merged events list several `recordIds`. Replicate one transaction
(`transactionKey`, `sequenceNumber`) as one commit.

From the **SOAP API Developer Guide**, *Outbound Messaging*, and Salesforce Help (*Considerations
for Outbound Messages*; release notes 234, *Send an Outbound Message from Your Record-Triggered
Flow*):

- Messages carry `OrganizationId`, `ActionId`, an optional `SessionId` and one or more
  notifications.
- The listener ACKs the whole message. Salesforce retries with growing intervals (at most 2 h
  apart) for 24 hours, may deliver more than once and out of order, and the receiver should
  check the org id.

`forcedotcom/pub-sub-api` (GitHub): `pubsub_api.proto` (package `eventbus.v1`) is licensed
**CC0-1.0**. Last change 2024-05-30 at commit `03d49a9c21c10ca3b55fa7ab30db6ee69250378e`.
