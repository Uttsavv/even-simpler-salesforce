# Bulk API 2.0

!!! note "Planned for 0.3"
    This API area is not part of 0.1. It is planned for **0.3** (see the
    [release plan](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md#19-release-plan)).

**Planned:** Bulk API 2.0 ingest and query jobs, with chunked uploads, parallel result download and `BulkJobFailed` for failed jobs.

Until then you can call the underlying REST resource with
[`sf.request()`](../guides/requests.md). The call still gets authentication, refresh, typed
errors, usage tracking and logging. For example, `await sf.request("GET", "jobs/ingest")`.
