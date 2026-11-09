# Pub/Sub API

!!! note "Planned for 0.5"
    This API area is not part of 0.1. It is planned for **0.5** (see the
    [release plan](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md#19-release-plan)).

**Planned:** Subscribing to and publishing events through the gRPC Pub/Sub API (an optional `[pubsub]` extra).

Until then, [`sf.request()`](../guides/requests.md) can reach any REST resource under
`/services/`. The call still gets authentication, refresh, typed errors, usage tracking and
logging.
