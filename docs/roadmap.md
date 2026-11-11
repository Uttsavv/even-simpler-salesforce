# Roadmap

0.1 covers authentication, records, queries, sObject Collections and raw requests. 0.2 adds
[Files](guides/files.md). 0.3 adds [GraphQL](guides/graphql.md). The API areas below come in
later releases.

| Area | Release | What's planned |
|---|---|---|
| Composite | 0.3 | Composite, Composite Graph, sObject Tree and Batch, with references between subrequests. |
| Bulk API 2.0 | 0.3 | Ingest and query jobs, chunked uploads and `BulkJobFailed`. |
| Tooling API | 0.3 | Tooling queries and requests, including anonymous Apex. |
| UI API | 0.4 | Records with layout information, list views and picklist values. |
| Metadata API | 0.4 | Metadata API operations. |
| Pub/Sub API | 0.5 | Subscribe and publish over gRPC (`[pubsub]` extra). |

The full plan is in the
[release plan](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md#19-release-plan).

## Until then

Call the REST resource yourself with [`sf.request()`](guides/requests.md). You still get
authentication, token refresh, typed errors, usage tracking and logging.
For example, `await sf.request("GET", "jobs/ingest")` lists Bulk API 2.0 ingest jobs.
