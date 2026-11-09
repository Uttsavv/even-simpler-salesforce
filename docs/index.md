---
template: home.html
hide:
  - navigation
  - toc
---

# even-simpler-salesforce

<div class="grid cards" markdown>

-   **Login flows as config objects**

    ---

    [Client credentials](authentication/client-credentials.md),
    [JWT bearer](authentication/jwt-bearer.md),
    [web server with PKCE](authentication/web-server-flow.md) and
    [static tokens](authentication/static-token.md), with
    [token stores](authentication/token-stores.md) that are safe under refresh-token rotation.

-   **Records, query and Collections**

    ---

    [Records](guides/records.md), [query and search](guides/query.md) with values quoted for
    you, and [sObject Collections](guides/collections.md).

-   **Typed errors**

    ---

    [Exceptions](errors.md) such as `ValidationRuleFailed`, `RowLocked`, `MultipleMatches` and
    `PartialFailure`, with `error_code`, `fields`, `status` and `request_id`.

-   **Usage and budgets**

    ---

    [API usage](usage-and-budgets.md) read from response headers at no extra API cost, and a
    budget guard that stops calls before you hit the org limit.

-   **Structured logs**

    ---

    [One event per call](logging.md) with secrets redacted, plus `ESF_DEBUG=1` for readable
    local output.

-   **Testing helpers**

    ---

    [In-memory fakes, a recorder and replayer](testing.md), and a pytest plugin.

</div>

!!! note "Alpha (0.1)"
    Breaking changes are possible in 0.x releases. See the
    [project status](getting-started.md#project-status) for what is covered and where it has been tested.
