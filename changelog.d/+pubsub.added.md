`sf.pubsub.subscribe()` receives platform events and Change Data Capture events over the
Pub/Sub API (`[pubsub]` extra). It decodes Avro payloads and the `ChangeEventHeader`,
including changed, nulled and diff field bitmaps with compound `Parent.Child` names, as
`PubSubEvent` and `ChangeHeader` (`ChangeType`, `is_gap`, `is_overflow`). Flow control
follows the consumer (`batch_size`, at most 100). `AsyncReplayStore` / `ReplayStore`, with
`AsyncInMemoryReplayStore` / `InMemoryReplayStore`, give at-least-once delivery across
restarts. The client reconnects from the last processed event, refreshes the token once on an
authentication error, and raises the new `PubSubError`, `PubSubLimitExceeded` and
`ReplayUnavailable` errors. Streams emit `sf.pubsub.*` events. `esf.testing` adds
`fake.pubsub` (`emit`, `emit_change`, `fail_next`) and `ReplayStoreContract`. The client
takes `pubsub_endpoint=` for the EU endpoint.
