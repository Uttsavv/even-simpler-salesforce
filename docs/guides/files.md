# Files

!!! note "Planned for 0.2"
    This API area is not part of 0.1. It is planned for **0.2** (see the
    [release plan](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md#19-release-plan)).

**Planned:** Uploading and downloading files (ContentVersion and related resources) with streaming transfers.

Until then you can call the underlying REST resource with
[`sf.request()`](../guides/requests.md). The call still gets authentication, refresh, typed
errors, usage tracking and logging. For example, `await sf.request("GET", "sobjects/ContentVersion")`.
