# Roadmap

0.1 covers authentication, records, queries, sObject Collections and raw requests. 0.2 adds
[Files](guides/files.md). 0.3 adds [Bulk API 2.0](guides/bulk.md), [Tooling](guides/tooling.md),
[GraphQL](guides/graphql.md) and [Composite](guides/composite.md). 0.4 adds the
[UI API](guides/ui.md), [Reports and dashboards](guides/reports.md) and the
[Metadata API](guides/metadata.md). The API areas below come in later releases.

| Area | Release | What's planned |
|---|---|---|
| Pub/Sub API | 0.5 | Subscribe and publish over gRPC (`[pubsub]` extra). |

The full plan is in the
[release plan](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md#19-release-plan).

## Until then

Call the REST resource yourself with [`sf.request()`](guides/requests.md). You still get
authentication, token refresh, typed errors, usage tracking and logging.
For example, `await sf.request("GET", "tooling/sobjects")` lists the Tooling API objects.
