# Roadmap

0.1 covers authentication, records, queries, sObject Collections and raw requests. 0.2 adds
[Files](guides/files.md). 0.3 adds [Bulk API 2.0](guides/bulk.md), [Tooling](guides/tooling.md),
[GraphQL](guides/graphql.md) and [Composite](guides/composite.md). 0.4 adds the
[UI API](guides/ui.md), [Reports and dashboards](guides/reports.md) and the
[Metadata API](guides/metadata.md). 0.5 adds the [Pub/Sub API](guides/pubsub.md).

Every API area in the release plan is now in the library.

| Next | What's planned |
|---|---|
| 1.0 | Interface freeze, docs for each version, the complete migration guide, and all deprecations resolved. |

The full plan is in the
[release plan](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md#19-release-plan).

## Anything else

Call the REST resource yourself with [`sf.request()`](guides/requests.md). You still get
authentication, token refresh, typed errors, usage tracking and logging.
For example, `await sf.request("GET", "recent")` lists the records the user viewed recently.
