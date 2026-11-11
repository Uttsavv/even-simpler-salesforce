# ADR 0013: Events are pulled through one async iterator

- **Status:** accepted, 2026-11-09
- **Source:** [`big-bang.md`](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md) §2, decision D31 (details in §5.1, §5.5, §8.2, §9.16, §11.2, §14, §15, §19, §21 V27–V28, Appendix A)
- **Refines:** the earlier §9.16 sketch (bare `async for` over `subscribe`, plus `sf.pubsub.publish`)

## Context

Salesforce pushes changes to external code in two ways today. The **Pub/Sub API** is a gRPC
stream of platform events and Change Data Capture (CDC) events. **Outbound Messaging** is a SOAP
POST to a public endpoint, and record-triggered flows can still send it. CometD is out of scope
(O15). Apex `EventBus` callbacks run inside Salesforce.

Salesforce's own Python sample for Pub/Sub:

- logs in with SOAP `login()` and a username and password, which D10 rules out;
- manages flow control with a threading semaphore;
- needs the `bitstring` package to decode CDC bitmaps;
- leaves the following to the app: storing replay ids, handling rejected replay ids, reading
  error trailers, reconnecting, and handling gap and overflow events.

Those are the parts users get wrong. A missed gap event silently corrupts a replica, and a
keepalive replay id saved before buffered events were processed silently skips them.

## Decision

- **One way to receive events:** `async with sf.pubsub.subscribe(topic, ...) as events: async for
  ev in events`. The sync twin is a blocking iterator. Pulling from the iterator is the flow
  control, which suits a pull-based API. The context manager closes the stream on `break` and on
  exceptions.
- **Rejected under D29:**
  - a callback or decorator registry (`@sf.on(...)`), which would be a second way to subscribe;
  - `before_request` / `on_refresh` hooks, because observers and `TokenStore` already cover them;
  - `sf.pubsub.publish`, because `sf.platform_events.publish` (§9.10) already publishes. It is
    deferred until users ask.
- **Decode by default:**
  - Avro payloads, with schemas cached by id;
  - CDC `changedFields` / `diffFields` / `nulledFields` bitmaps decoded to field names, including
    compound `Parent.Child` names;
  - change types as an enum, with `is_gap`, `is_overflow` and `record_ids`.

  Gap events are typed but not re-fetched automatically: that costs API calls and the fields are
  app-specific.
- **At-least-once delivery** through an optional `AsyncReplayStore` / `ReplayStore`, keyed
  `org_id:topic`:
  - Saves happen after each consumed batch and on normal exit, never when the block exits with
    an exception.
  - Keepalive `latest_replay_id` values are saved only when nothing is buffered.
  - A rejected replay id raises `ReplayUnavailable` unless `on_invalid_replay="latest"` or
    `"earliest"` is set.
  - Without a store, reconnects still resume from the last processed id held in memory.
- **Typed errors:** the gRPC status and the `error-code` trailer map onto §8 (`SessionExpired`
  after one shared refresh, `NotFound`, `Forbidden`, `PubSubLimitExceeded`, `ReplayUnavailable`,
  `PubSubError`). The `rpcId` goes into `request_id`. `org.migrated` reconnects at least 30
  minutes apart.
- **A second network path, owned by one module.** gRPC cannot run over httpx.
  - `_async/pubsub_transport.py` and the `GrpcStream` adapter in `_compat` are the only
    modules that import `grpc`. The transport reuses the auth runtime, `RetryPolicy` backoff and
    observers.
  - All decisions are pure functions in `core/pubsub.py`.
  - The sync twin swaps `grpc.aio` for `grpc` through a `GrpcStream` in `_compat`.
- **Dependencies:** `[pubsub]` = `grpcio`, `protobuf`, `fastavro`. The generated stubs ship in the
  wheel, so users never need `grpcio-tools`. The `protobuf` minimum equals the version the stubs
  were generated with.
- **The `.proto` file is vendored** from `forcedotcom/pub-sub-api` (CC0-1.0, compatible with
  Apache-2.0) at commit `03d49a9c21c10ca3b55fa7ab30db6ee69250378e`. `scripts/pubsub_proto.py
  --check` fails CI if the stubs or the pinned SHA drift.
- **Later:**
  - `ManagedSubscribe` (Beta) becomes one parameter once Salesforce makes it GA.
  - An Outbound Messaging receiver for the FastAPI/Flask integrations is unscheduled, built when
    users ask. It must use hardened XML, check the org id, de-duplicate on the notification id,
    send one ACK and redact `SessionId`.

## Consequences

- Events are not unique by replay id. Platform events de-duplicate on `event_id`, and change
  events on `(transaction_key, sequence_number)`.
- Pub/Sub streams emit `sf.pubsub.*` events, not `sf.request`. Payloads are never logged.
- Contract tests need an in-process gRPC fake server, because respx cannot mock gRPC. Users get
  `FakeSalesforce.pubsub` and `replay_store_contract`.
- On Python 3.10, `[pubsub]` resolves fastavro 1.12.x, because fastavro 1.13 needs Python 3.11.
  Python 3.10 reached end of life on 2026-10-01. Whether to keep it (O2) is a separate decision.
- Recorded on a Developer Edition org (2026-11-12): bit 0 of a bitmap is the least-significant
  bit (V27). Schemas mark Date and DateTime fields only in `doc` (V28, V77). Change events
  carry a `ConsumerEvent.event.id` too, although the docs describe it for platform events only.

## Implementation notes (0.5)

Choices made while building 0.5, within the decision above:

- **Refill rule.** The client asks for more events once at most half of `batch_size` is
  requested or buffered. A fast consumer then rarely waits for a round trip, and the limit of
  `batch_size` outstanding events still holds (`core.pubsub.refill`).
- **Idle closes.** A stream that ends with no specific Pub/Sub code while nothing was pending
  counts as an idle close: the client subscribes again at once and doesn't count an attempt.
  An `org.migrated` or authentication code is never treated as idle (V76).
- **Codes.** `subscription.fetch.replayid.validation.failed` maps like `replayid.corrupted`,
  and `auth.refresh.invalid` like the other auth codes. Codes match by suffix, so a longer
  prefix still maps.
- **Org id.** A token without an org id (`StaticToken`) gets one `userinfo` call per token key,
  cached in the client family's Pub/Sub state.
- **Endpoint.** The client takes `pubsub_endpoint=` (`host:port`). A loopback endpoint uses a
  plaintext channel. That exists only for in-process test servers; every other host uses TLS.
- **Dates.** A `long` becomes a UTC `datetime` when its Avro `doc` matches
  `<name>:DateTime[:…]`, and a `date` when it matches `<name>:DateOnly[:…]` (epoch ms at
  midnight UTC, recorded). fastavro's `timestamp-millis` and `date` logical types are honoured
  too (V28, V77).
- **Extra fields.** `ChangeHeader.commit_number` carries `commitNumber`.
  `UnexpectedResponse` gained `event_id` and `replay_id` for undecodable payloads.
  `sf.pubsub.subscribed` also reports `replay_id`, `batch_size` and `schema_calls`.
  `GetTopic` is not called.
- **Generated stubs.** The transport builds calls from method paths through `GrpcStream` and
  `grpc_unary` in `_compat`. `pubsub_api_pb2_grpc.py` ships for completeness, and only the
  contract test server imports it.
- **The fake.** `FakeSalesforce.pubsub` keeps the save points, replay keys, rejected replay
  ids and `sf.pubsub.*` events. It has no history before the test (`"latest"` and
  `"earliest"` both start at the first emitted event), and iteration ends when every emitted
  event was yielded, so tests finish. `fail_next` raises from the next read, where the real
  client surfaces stream errors.
- **Licence.** `pubsub_api.proto` is CC0-1.0 at the pinned commit, recorded with its source in
  `src/even_simpler_salesforce/_pubsub/NOTICE`.

New verification items: V75 (trailer keys), V76 (idle close status), V77 (Date fields), V78
(overflow events) and V79 (sandbox acceptance run).

## Sources

Read in a browser on 2026-11-09 (developer.salesforce.com blocks non-browser clients):

- Pub/Sub API: [Subscribe RPC](https://developer.salesforce.com/docs/platform/pub-sub-api/references/methods/subscribe-rpc.html),
  [ManagedSubscribe RPC (Beta)](https://developer.salesforce.com/docs/platform/pub-sub-api/references/methods/managedsubscribe-rpc.html),
  [Event Deserialization Considerations](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/event-deserialization-considerations.html),
  [Event Message Durability](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/event-message-durability.html),
  [Handle Errors](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/handling-errors.html),
  [Retry Long-Lived RPC Calls](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/retry-rpc-calls.html),
  [Allocations](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/allocations.html),
  [Authentication](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/supported-auth.html),
  [Pull Subscription and Flow Control](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/flow-control.html)
- Change Data Capture: [Gap Events](https://developer.salesforce.com/docs/platform/change-data-capture/guide/cdc-other-events-gap.html),
  [Overflow Events](https://developer.salesforce.com/docs/platform/change-data-capture/guide/cdc-other-events-overflow.html),
  [Merged Change Events](https://developer.salesforce.com/docs/platform/change-data-capture/guide/cdc-subscribe-merged-events.html),
  [Transaction-Based Replication Steps](https://developer.salesforce.com/docs/platform/change-data-capture/guide/cdc-replication-steps.html)
- Outbound Messaging: [Understanding Outbound Messaging](https://developer.salesforce.com/docs/platform/api/guide/sforce-api-om-outboundmessaging-understanding.html),
  [Outbound Messaging WSDL](https://developer.salesforce.com/docs/platform/api/guide/sforce-api-om-outboundmessaging-wsdl.html),
  [Security](https://developer.salesforce.com/docs/platform/api/guide/sforce-api-om-outboundmessaging-security.html),
  [Considerations for Outbound Messages](https://help.salesforce.com/s/articleView?language=en_US&id=workflow_om_considerations.htm&type=0),
  [Send an Outbound Message from Your Record-Triggered Flow (release 234)](https://help.salesforce.com/s/articleView?language=en_US&id=release-notes.rn_automate_flow_builder_outbound_message.htm&type=5&release=234.16.0)
- [`forcedotcom/pub-sub-api`](https://github.com/forcedotcom/pub-sub-api) (`pubsub_api.proto`, CC0-1.0; Python sample under `python/`)
- PyPI metadata for `grpcio`, `grpcio-tools`, `protobuf`, `fastavro`, `types-grpcio` and `types-protobuf`, checked 2026-11-09
