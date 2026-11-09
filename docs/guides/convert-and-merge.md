# Lead convert and merge

!!! note "Planned for 0.2"
    This API area is not part of 0.1. It is planned for **0.2** (see the
    [release plan](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md#19-release-plan)).

**Planned:** Lead conversion and record merge as normal methods. Salesforce has no REST resource for either, so the library will call the SOAP API internally (SOAP login is still not supported).

Until then, [`sf.request()`](../guides/requests.md) can reach any REST resource under
`/services/`. The call still gets authentication, refresh, typed errors, usage tracking and
logging.
