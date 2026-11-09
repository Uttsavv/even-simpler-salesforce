# Composite

!!! note "Planned for 0.3"
    This API area is not part of 0.1. It is planned for **0.3** (see the
    [release plan](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md#19-release-plan)).

**Planned:** Composite, Composite Graph, sObject Tree and Batch requests, with a builder for references between subrequests (`@{ref.id}`) and limits checked on the client before sending.

Until then you can call the underlying REST resource with
[`sf.request()`](../guides/requests.md). The call still gets authentication, refresh, typed
errors, usage tracking and logging. For example, `await sf.request("GET", "composite")`.
