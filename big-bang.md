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
│   ├── cli/                     # `esf` command (generate actions, whoami, limits, debug)
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

Top-level exports (the 0.1 SemVer surface): `AsyncSalesforce`, `Salesforce`, all `Auth`
classes, `Token`, `TokenStore`, `InMemoryTokenStore`, `soql`, `Cursor`, all exceptions,
result models, `Budget`, `Observer`, `capture`, `__version__`.

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
- **Auth runtime** sits between the client and the `Auth` config: it holds the current in-memory token per `token_key`, runs single-flight refresh and talks to the `TokenStore`.

### 5.2 Client lifecycle

- `AsyncSalesforce(auth, *, api_version="68.0", http_client=None, store=None, cache=None, observers=(), budget=None, retry=RetryPolicy(), timeout=Timeouts(), max_concurrency=None, user_agent=None, json=None)`.
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
| `asyncio.Event` | `threading.Event` |
| `asyncio.sleep` | `time.sleep` |
| `gather_limited(n, coros)` (semaphore + `asyncio.gather`; Python 3.10 has no `TaskGroup`) | `ThreadPoolExecutor(max_workers=n).map` or sequential |
| `anext`-based helpers | `next` |

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
| `[pubsub]` | `grpcio`, `fastavro` | Pub/Sub API |
| `[otel]` | `opentelemetry-api` | spans and metrics observer |
| `[structlog]` | `structlog` | structlog observer |
| `[fastapi]` | `fastapi` | `contrib.fastapi` |
| `[flask]` | `flask` | `contrib.flask` |
| `[testing]` | `respx`, `pytest` | `testing` module, pytest plugin |
| `[all]` | all of the above | |

The Metadata API (0.4) uses only the stdlib (`zipfile`, `xml.etree`) and needs no extra.

---

## 6. Authentication and tokens

### 6.1 Flow classes

All are frozen config objects implementing the `Auth` protocol. They hold no tokens themselves.

| Class | Use | Inputs | Refresh behaviour |
|---|---|---|---|
| `WebServerFlow` | A human user logged in via the browser; app holds their refresh token | `domain`, `client_id`, `client_secret` (optional for PKCE public clients), `redirect_uri`, `scopes`, `token_key`, `refresh_ahead=None`, `expiry="unknown"\|"introspect"` | Refresh token grant |
| `ClientCredentials` | The app acting as its integration user | `domain` (**must be My Domain**), `client_id`, `client_secret` | Re-request a token (no refresh token issued) |
| `JwtBearer` | Server-to-server with a certificate | `domain`, `client_id`, `username`, `private_key`, `audience` (`login`/`test`/custom) | Re-sign and re-request |
| `StaticToken` | Tests, scripts, tokens obtained elsewhere | `access_token`, `instance_url` | None: a 401 raises `SessionExpired` |
| `TokenExchange` *(later)* | RFC 8693 exchange | | |
| `DeviceFlow` *(later)* | CLIs and devices | | |

`domain` accepts `"login"`, `"test"`, or a full `https://<mydomain>.my.salesforce.com` URL.

**WebServerFlow helpers** (pure + one I/O call each):

```python
start = flow.authorization_url(state="...")       # returns AuthStart(url, state, code_verifier)
token = await sf.auth.exchange_code(flow, code, code_verifier=start.code_verifier)  # saves to store
await sf.auth.revoke(token_key)                  # POST /services/oauth2/revoke, clears store
info  = await sf.auth.userinfo()                 # /services/oauth2/userinfo
intro = await sf.auth.introspect(token_key)      # /services/oauth2/introspect
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

```python
class TokenStore(Protocol):
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
- Simultaneous refreshes with the same refresh token fail intermittently with **"Token request is already being processed"**. Salesforce advises caching and reusing tokens, and retrying when this happens.

Algorithm for a call on a client with `token_key = K`:

```
1. tok = memory[K] or await store.load(K) (or initial acquisition for ClientCredentials/JwtBearer)
2. If refresh_ahead is set and tok.issued_at + refresh_ahead <= now  → go to REFRESH(tok)
   If expiry == "introspect" and tok.expires_at known and <= now + skew → go to REFRESH(tok)
3. Send request with tok.access_token.
4. If response is 401 with errorCode INVALID_SESSION_ID (or OAuth `invalid_token`):
       new = REFRESH(tok); resend once with new. A second 401 raises SessionExpired.

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
       new = merge(resp, stale)   # keep old refresh_token if the response has none
       saved = await store.save(K, new, expected_version=current.version if current else None)
         - TokenConflict → re-load and use the stored token (another process won)
       memory[K] = saved; emit `sf.auth.refreshed`; return saved
```

Rules:
- The new refresh token MUST be saved **before** the new access token is used for any request.
- The refresh POST itself is never retried blindly. This is a deliberate exception to the general retry policy.
- `ClientCredentials` / `JwtBearer` have no refresh token; "refresh" is a fresh token request, and blind retries are safe.
- Default skew for `expires_at` checks: 60 s.

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
7. **Parse `Sforce-Limit-Info`** (`api-usage=used/max`, and `api-bursts=used/max` when present) into usage counters.
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
│   └── BudgetExceeded                   raised by the library's own guard (§10.4)
├── ServerError                          500, 502, 503, SERVER_UNAVAILABLE (502/503 retryable; 420 = Edge routing,
│                                        not retryable)
├── PartialFailure                       allOrNone=false batches; .results: BatchResult
├── BulkJobFailed                        (.job_id, .state, .error_message)
├── ApexError                            APEX_ERROR / Apex REST errors (.status, .body)
├── GraphQLError                         (.errors from the GraphQL `errors` array)
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

### 8.5 What the library will not do

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
| `delete(id)` | `DELETE sobjects/{o}/{id}` | |
| `describe(cached=True)` | `GET sobjects/{o}/describe` | uses describe cache (§12) |
| `metadata()` | `GET sobjects/{o}` | |
| `updated(start, end)` / `deleted(start, end)` | `GET sobjects/{o}/updated|deleted` | |
| `sf.describe_global(cached=True)` | `GET sobjects` | |

**External id values in paths** are percent-encoded as one path segment (`url_segment`: space →
`%20`, `/` → `%2F`, `+` → `%2B`; httpx keeps them as sent). `quote_plus` is never used (it turns
spaces into `+`, which Salesforce reads literally; simple-salesforce #801). Values containing `.`
(e.g. ending in `.json` or `.xml`) may be read by Salesforce as a format suffix (#22): ⚠️ **VERIFY**
in a sandbox (V19); if confirmed, `get_by_external_id`/`upsert` fall back to a SOQL lookup or the
Collections upsert for such values.

Headers supported via keyword args: `assignment_rule_id`, `duplicate_rule_header`
(`allow_save=True`), `if_modified_since`, `if_unmodified_since` → `Conflict` on 412.

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
- `execute_anonymous` returns `AnonymousResult(compiled, success, compile_problem, exception_message, exception_stack_trace, line, column)`. It **cannot** return values. It is never called implicitly.
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
⚠️ **VERIFY** that the SOAP API endpoint (not `login()`) keeps working with OAuth tokens after the
Summer '27 `login()` retirement.

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
`sf.files.download_attachment(id)`.

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
```

- **Parameter names match REST** (#510, #760): `sf.bulk.query(soql, include_deleted=False)` uses the
  `queryAll` operation when `True`; iterators are named `*_iter` like `query_iter` where they are
  plain iterators.
- **Encoding** (#706, #710): uploads are sent as `Content-Type: text/csv; charset=UTF-8`; result
  CSVs are always decoded as UTF-8, whatever charset httpx guesses.
- **Nulls** (#309, #769): in Bulk 2.0 CSV an empty cell leaves the field unchanged and `#N/A` sets it
  to null. Rows given as dicts map `None` → `#N/A` and a missing key → empty cell; a literal
  string `"#N/A"` value is rejected with `ValueError` (ambiguous). ⚠️ **VERIFY** against the official
  Bulk 2.0 docs and a sandbox (V18).
- **Streaming uploads** (#738, #629): `rows=csv_path` and iterables are streamed in ≤ 100 MB parts;
  the client never reads a whole file into memory before uploading. Benchmarks assert flat memory.
- **Job states** (#517): states parse into an enum (`Open`, `UploadComplete`, `InProgress`,
  `JobComplete`, `Failed`, `Aborted`). An unknown state raises `UnexpectedResponse` instead of being
  polled forever, and `wait()` always has a timeout (default 3600 s).
- **Compound fields** (#182): Bulk query rejects address and geolocation compound fields. The
  client maps that job failure to `BulkJobFailed` with a hint naming the component fields, and
  offers `sf.bulk.expand_compound_fields(sobject, fields)` (describe-based) to rewrite a field list.
- Ingest uploads: one request may carry at most 150 MB **after** Salesforce's base64 conversion, so the client splits raw CSV at **100 MB** per job, as the docs advise. Gzip request compression is still applied on the wire.
- Results are fetched with `?locator=&maxRecords=` using the **same API version that created the job**; a different version returns 409, so the job object pins its version.
- Query results use `Sforce-Locator` pagination; with `parallel_downloads>1`, locators are fetched ahead with bounded concurrency while preserving order.
- `BulkJobFailed` on `state=Failed`; `job.info` exposes counts and timing.

### 9.12 Tooling — `sf.tooling` (0.3)

`query`, `query_iter`, `sobject(name)` (same interface as §9.1), `execute_anonymous`,
`run_tests_async(class_ids)`, `completions(type)`. Uses `/services/data/vXX/tooling/`.

### 9.13 UI API — `sf.ui` (0.4)

`record(id, fields=None, layout_types=None, modes=None)`, `object_info(name)`,
`picklist_values(name, record_type_id)`, `create_defaults(name, record_type_id=None)`,
`clone_defaults(id)`, `list_views(name)`, `list_records(name, list_view)`. Cached like describe.

### 9.14 Reports and dashboards — `sf.reports` (0.4)

`describe(report_id)`, `run(report_id, filters=None, include_details=True)` (sync run),
`run_async(...)` + `instance(...)` polling, `dashboards.get(id)`, `dashboards.refresh(id)`.
Result helpers turn fact maps into rows.

### 9.15 Metadata API — `sf.metadata` (0.4)

`deploy(zip_bytes | directory, options=DeployOptions(...))` via REST
`metadata/deployRequest`, `deploy_status(id)`, `retrieve(package_xml_or_types)` via SOAP Metadata
API with polling. `list_metadata(type)`, `describe_metadata()`. Stdlib only.

### 9.16 Pub/Sub API — `sf.pubsub` (0.5, `[pubsub]`)

```python
async for event in sf.pubsub.subscribe("/event/Order_Event__e", replay="latest" | "earliest" | replay_id,
                                       batch_size=100):
    event.payload  # decoded Avro → dict
    event.replay_id
await sf.pubsub.publish("/event/Order_Event__e", [payload, ...])
```

- gRPC to `api.pubsub.salesforce.com:443` (7443 also accepted; configurable). The EU-only endpoint `api.deu.pubsub.salesforce.com` is selectable for data-in-transit requirements.
- Auth metadata from the same `Auth`: `accesstoken`, `instanceurl`, `tenantid` (= org id).
- Flow control: the client sends `FetchRequest.num_requested` as it frees capacity (server default 100).
- Schema cache by schema id; flow control via `FetchRequest(num_requested)`; automatic resubscribe from the last replay id on stream errors; token refresh on `UNAUTHENTICATED`.
- Supports platform events, Change Data Capture (`/data/AccountChangeEvent`) and managed subscriptions.
- Sync twin: `subscribe` is a blocking iterator; documented as best used in a dedicated thread.

### 9.17 Limits — `sf.limits()` (0.1)

`GET limits` → `dict[str, Limit(max, remaining)]`. Not called implicitly (it costs an API call). See §10 for header-based tracking.

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
`[A-Za-z0-9_.:-]{1,64}`. A caller's own `Sforce-Call-Options` (e.g. `defaultNamespace=ns`) is
merged, not replaced: comma-separated, ours first, header name matched case-insensitively; a
`client=` the caller sets for one call wins for that call. ⚠️ **VERIFY** where the `client` value
shows up for admins (V20).


`sf.tagged(caller="job:backfill", priority="low")` returns a child view whose calls carry those
attributes (for counters, logs and the guard). Default `priority="normal"`. A contextvar alternative
exists: `with esf.call_context(caller="route:/leads", priority="high"): ...`.

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
| `sf.bulk.job` | bulk job state changes | job_id, state, records_processed, records_failed |
| `sf.pubsub.*` | subscribe, reconnect, error | topic, replay_id |

### 11.3 `sf.request` fields

`operation` (`sobject.get`, `query`, `query_more`, `collections.update`, `bulk.ingest.upload`, ...),
`sobject`, `method`, `path_template` (ids → `:id`), `status`, `outcome` (`ok`/`error`),
`error_code`, `error_class`, `duration_ms`, `attempts`, `retry_reasons`, `refreshed`,
`records` (returned or sent), `bytes_in`, `bytes_out`, `api_used`, `api_max`, `caller`,
`priority`, `soql_hash` (SHA-256 of normalised SOQL, first 12 hex), `soql` (only if
`log_soql="redacted"`: string/number literals replaced by `?`), `request_id`, `api_version`,
`client_id_hash`.

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
`esf.duration`, gauge `esf.api_usage`).

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
| Conditional describe | When a cached describe is stale, revalidate with `If-Modified-Since`; Salesforce documents `304 Not Modified` with no body for describe | on |
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

---

## 15. The library's own test strategy

| Layer | Location | Tools | What |
|---|---|---|---|
| Unit | `tests/unit` | pytest | `core/` builders and parsers, SOQL escaping (property tests with hypothesis), cursor encode/validate/sign, error classification for every recorded error body, limit-header parsing, unasync output |
| Contract | `tests/contract` | pytest + respx, parametrised over async/sync | every public method against recorded responses: success, each error family, pagination, chunking, retries (fake clock), refresh single-flight and rotation, "already being processed", ambiguous refresh |
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
- **Structure** (mirrors simple-salesforce's docs, extended): Home · Getting started · Authentication (one page per flow, Salesforce setup steps for External Client Apps, token stores) · Guides per API area · Errors · Usage & budgets · Logging · Performance · Integrations · Testing · Migrating from simple-salesforce · Reference · Changelog · ADRs.
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
- Areas: Records (§9.1), Query/Search + `soql` + Cursor (§9.2), Collections (§9.3), raw `request` (§9.5), `limits()` (§9.17).
- Usage (§10) and observability (§11) complete, stdlib observer; structlog/otel adapters.
- Testing module (§14) for the areas above.
- Docs: getting started, auth pages, records/query/collections guides, errors, logging, usage, migration guide (partial).
- Repo tooling, CI and release pipeline (§17–18).

Acceptance criteria:
- Every §21 item tagged *0.1* verified.
- Concurrency test: 100 callers, expired token → 1 token POST; with rotation enabled in a sandbox, no `invalid_grant` across 1,000 refresh cycles from 4 processes sharing a CAS store.
- A sample FastAPI app and a sample Flask app in `examples/` use only public APIs for query, records and collections, with tests passing.

### 0.2 — files, actions, integrations
Files, platform events publish, actions + Apex REST + `esf generate actions`, lead convert/merge
(SOAP), FastAPI and Flask integrations.

### 0.3 — Bulk, Tooling, GraphQL, Composite
Bulk 2.0 ingest/query, Tooling incl. execute anonymous, GraphQL, Composite/Graph/Tree/Batch builders.

### 0.4 — UI API, Reports, Metadata
### 0.5 — Pub/Sub API
### 1.0 — stabilise
Interface freeze, per-version docs, migration guide complete, all
deprecations resolved.

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
| `format_soql(...)` | `esf.soql(...)` |
| `sf.restful("composite/sobjects", method="PATCH", json=...)` | `sf.collections.update(...)` |
| `sf.restful(path, ...)` | `sf.request(method, path, ...)` |
| `sf.apexecute("path", method="POST", data=...)` | `sf.apex.post("/path", json=...)` |
| `sf.bulk.Lead.insert(rows)` (Bulk v1) | `sf.bulk.ingest("Lead", "insert", rows=rows)` (Bulk 2.0) |
| `sf.bulk2.Lead.query(q)` | `sf.bulk.query(q)` |
| `sf.toolingexecute(...)` | `sf.tooling.request(...)` / `sf.tooling.query(...)` |
| `sf.limits()` | `sf.limits()`; usage also in `sf.usage.snapshot()` for free |
| `SalesforceMalformedRequest` (inspect `.content`) | specific classes (`InvalidField`, `ValidationRuleFailed`, ...) with `.error_code`, `.fields` |
| `SalesforceExpiredSession` | handled automatically; `ReauthorizationRequired` if the refresh token is dead |
| `OrderedDict` results | plain `dict` |

---

## 21. Verification backlog

Each item MUST be verified (official doc URL or recorded sandbox evidence linked in the ADR/test)
before the release named.

| # | Claim | Needed by | How |
|---|---|---|---|
| V1 | Token response has no `expires_in` | 0.1 | **Done** — *OAuth 2.0 Refresh Token Flow* (help) |
| V2 | Rotation invalidates the previous refresh token; reuse invalidates the chain | 0.1 | **Done** — same page |
| V3 | "Token request is already being processed" on concurrent refresh | 0.1 | **Done** — same page. Still record the exact error body in a sandbox |
| V4 | Introspection response contains `exp` | 0.1 | **Open** — *OpenID Connect Token Introspection* (help) doesn't list response fields; check in a sandbox |
| V5 | Error code strings in §8.2 exist; HTTP status meanings | 0.1 | **Done** — *Core Data Types Used in API Calls* (`StatusCode`) and *Status Codes and Error Responses* (REST). Added 300/410/414/428/431; `REQUEST_LIMIT_EXCEEDED` is 403 and also covers concurrent limits |
| V5b | Validation rules / Flow custom errors / `addError()` → `FIELD_CUSTOM_VALIDATION_EXCEPTION`; unhandled trigger exception → `CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY` | 0.1 | **Open** — record responses in a sandbox |
| V6 | Expired/invalid token → 401 with `INVALID_SESSION_ID` | 0.1 | **Done** — REST status page (401 = session ID or OAuth token expired/invalid, body has `errorCode`) and `StatusCode` (`INVALID_SESSION_ID` = malformed or expired) |
| V7 | Client credentials needs the My Domain token endpoint; no refresh token | 0.1 | **Done** — *OAuth 2.0 Client Credentials Flow* (help): login/test.salesforce.com "aren't supported"; "doesn't support refresh tokens" |
| V8 | Composite family limits | 0.1/0.3 | **Done** — Collections create 200; retrieve by POST 2,000 (GET ≈ 800 ids before HTTP 414); Composite 25 with ≤ 5 Collections/query subrequests; Graph 500; Tree 200 records / 5 types / 5 levels; Batch 25 |
| V9 | `Sforce-Query-Options` batch size | 0.1 | **Done** — default 2,000, min 200, max 2,000, not guaranteed |
| V10 | `Sforce-Limit-Info` format | 0.1 | **Done** — `api-usage=10018/100000; api-bursts=1/750` |
| V11 | SOAP API (Partner) still works with OAuth access tokens after `login()` retirement | 0.2 | **Partly done** — *Platform SOAP API login() Retirement* (help, updated 9 Oct 2026) scopes the retirement to `login()` only and recommends ECA + OAuth; SOAP API versions 41.0–66.0 are listed as supported. Check `convertLead` with an OAuth token in a sandbox |
| V12 | ContentVersion upload limit | 0.2 | **Done** — *Insert or Update Blob Data*: multipart 2 GB ContentVersion; non-multipart 37.5 MB base64 |
| V13 | Bulk 2.0 upload size and result locators | 0.3 | **Done** — 150 MB base64 per upload (≈100 MB raw); `locator`/`maxRecords`; same API version or 409 |
| V14 | Describe returns 304 with `If-Modified-Since` | 0.1 | **Done** — *sObject Describe* reference |
| V15 | Zensical renders mkdocstrings API reference | first docs release | **Done** — Zensical *MkDocs plugins* compatibility page (native mkdocstrings; no gen-files; use autoapi/api-autonav) |
| V16 | Pub/Sub host/port, auth metadata keys, flow control | 0.5 | **Done** — ports 443/7443, EU endpoint, `accesstoken`/`instanceurl`/`tenantid`, `num_requested` (default 100) |
| V17 | Tooling `executeAnonymous` result fields | 0.3 | **Open** — the REST resource is documented (GET, API 29.0+, blocked from managed packages) but no result-field page was found; record a response in a sandbox |
| V18 | Bulk 2.0 CSV: empty cell = unchanged, `#N/A` = null; UTF-8 handling | 0.3 | **Open** — official Bulk 2.0 docs + sandbox (simple-salesforce #309, #769, #706) |
| V19 | External id values containing `.` (e.g. `x.json`) in `sobjects/{o}/{field}/{value}` are read as a format suffix | 0.1 | **Open** — sandbox (simple-salesforce #22) |
| V21 | HTTP status and body for `UNSUPPORTED_API_VERSION` (configured version not offered by the org); message seen elsewhere: "Invalid Api version specified on URL" | 0.1 | **Open** — sandbox; current test fixture is synthetic |
| V20 | `Sforce-Call-Options: client=<name>` on REST calls: accepted on every resource, and where admins see it | 0.1 | **Open** — REST API headers reference + sandbox |

Remaining open: V4, V5b, V11 (sandbox half), V17, V18, V19, V20, V21 — all need a sandbox (V18/V20 also a docs check).

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
