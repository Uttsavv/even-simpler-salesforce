# Reports and dashboards

!!! note "Planned for 0.4"
    This API area is not part of 0.1. It is planned for **0.4** (see the
    [release plan](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md#19-release-plan)).

**Planned:** Running reports and reading dashboards through the Analytics REST API.

Until then you can call the underlying REST resource with
[`sf.request()`](../guides/requests.md). The call still gets authentication, refresh, typed
errors, usage tracking and logging. For example, `await sf.request("GET", "analytics/reports")`.
