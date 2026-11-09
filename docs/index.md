# even-simpler-salesforce

A typed Salesforce client for Python. It is async-first, and a sync client with the same
methods is generated from the async code. Both are built on [httpx](https://www.python-httpx.org/).

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com",
    client_id="3MVG9...",
    client_secret="...",
)

async with esf.AsyncSalesforce(auth) as sf:
    page = await sf.query(esf.soql("SELECT Id, Name FROM Lead WHERE Status = {}", "Open"))
    for lead in page:
        print(lead["Id"], lead["Name"])
```

!!! warning "Project status: alpha (0.1)"
    Breaking changes are possible in 0.x minor releases. They are always listed under
    *Changed* / *Removed* in the [changelog](changelog.md) with migration notes.

    So far the library has been tested against **one production org** only:

    | Tested on | |
    |---|---|
    | Edition | _TBD_ |
    | API version | 68.0 (library default) |
    | Auth flows exercised | _TBD_ |

    If you can run it against another edition, a sandbox, a scratch org or another login
    setup, please share your results by opening an
    [issue or pull request](https://github.com/Uttsavv/even-simpler-salesforce).

## What you get

- **One request pipeline for every call.** Each call gets a token, passes the budget guard
  and concurrency limit, and is sent. The client then reads the usage header, maps any error
  to an exception class, refreshes once on an expired session, retries only when that is safe,
  and emits one log event. [`sf.request()`](guides/requests.md) uses the same pipeline.
- **Login flows as config objects:** [client credentials](authentication/client-credentials.md),
  [JWT bearer](authentication/jwt-bearer.md), [web server with PKCE](authentication/web-server-flow.md)
  and [static tokens](authentication/static-token.md), with
  [pluggable token stores](authentication/token-stores.md) that are safe under refresh-token
  rotation.
- **Records, query, search and Collections**:
  [records](guides/records.md), [query and search](guides/query.md),
  [sObject Collections](guides/collections.md).
- **[Typed errors](errors.md)** such as `ValidationRuleFailed`, `RowLocked`, `MultipleMatches`
  and `PartialFailure`, with `error_code`, `fields`, `status` and `request_id`.
- **[Usage tracking and budgets](usage-and-budgets.md)** read from response headers, at no
  extra API cost.
- **[Structured logs](logging.md)** with secrets redacted, plus `ESF_DEBUG=1` for readable
  local output.
- **[Testing helpers](testing.md):** in-memory fakes, a recorder and replayer, and a pytest
  plugin.

## Where to go next

| If you want to… | Read |
|---|---|
| install and make a first call | [Getting started](getting-started.md) |
| create the External Client App in Salesforce | [Salesforce setup](authentication/salesforce-setup.md) |
| pick a login flow | [Authentication](authentication/index.md) |
| move an existing simple-salesforce codebase | [Migrating from simple-salesforce](migrating-from-simple-salesforce.md) |
| look up a signature | [Reference](reference/index.md) |

## What is in 0.1

0.1 covers the clients, authentication, records, query and search, Collections, raw requests,
`limits()`, usage tracking, observability, the testing module and the [CLI](cli.md). The
remaining API areas (Composite, GraphQL, Bulk API 2.0, files, actions, Apex REST helpers, lead
convert and merge, platform events, Tooling, UI API, reports, Metadata and Pub/Sub) and the
FastAPI and Flask integrations are planned for later 0.x releases. Each of them has a page in the
navigation that says which release it is planned for.

---

Not affiliated with or endorsed by Salesforce, Inc. Licensed under the Apache License 2.0.
