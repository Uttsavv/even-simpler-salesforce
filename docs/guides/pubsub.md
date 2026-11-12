# Pub/Sub API

`sf.pubsub.subscribe()` receives platform events and Change Data Capture (CDC) events from the
Salesforce Pub/Sub API. Use it to react to changes in Salesforce, or to keep a copy of records
up to date.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)


async def print_orders(sf: esf.AsyncSalesforce) -> None:
    async with sf.pubsub.subscribe("/event/Order_Event__e") as events:
        async for ev in events:
            print(ev.event_id, ev.payload["Order_Number__c"])
```

The `async for` loop runs until you leave it, and the client reconnects for you. When the
`async with` block exits, the stream closes, also after `break` or an exception.

The Pub/Sub API runs over gRPC, so it needs the `[pubsub]` extra:

```console
pip install 'even-simpler-salesforce[pubsub]'
```

Without the extra, `subscribe()` raises `ImportError` with that command. The rest of the
library doesn't need it.

## Choose a topic

| Topic | Events |
|---|---|
| `/event/Order_Event__e` | a platform event |
| `/data/AccountChangeEvent` | CDC events of one object; custom objects use `/data/Order__ChangeEvent` |
| `/data/ChangeEvents` | CDC events of every object selected for Change Data Capture |
| `/data/My_Channel__chn` | a custom channel |

To receive CDC events, select the objects in Setup, under **Change Data Capture**. The user of
the client needs read access to the objects and fields.

## Read events

Each event is a `PubSubEvent`:

| Attribute | Value |
|---|---|
| `topic` | the topic you subscribed to |
| `replay_id` | position of the event in the stream (`bytes`) |
| `event_id` | the event's `id` (the `EventUuid` of a platform event); `None` if Salesforce sent none |
| `schema_id` | id of the Avro schema of the payload |
| `payload` | the decoded event (`dict`) |
| `change` | a `ChangeHeader` for a change event; `None` for a platform event |

The client gets each Avro schema once and caches it for the life of the client. A field that
the schema marks as a DateTime becomes an aware UTC `datetime`, and a field it marks as a Date
becomes a `date`. Other numbers stay `int`.

If a payload doesn't match its schema, the client raises `UnexpectedResponse`, with
`event_id` and `replay_id` set. Salesforce doesn't check payloads on publish, so this can
happen.

## Change events

`ev.change` holds the decoded `ChangeEventHeader`. The field lists hold field names, not the
bitmaps that Salesforce sends.

```python
async def print_changes(sf: esf.AsyncSalesforce) -> None:
    async with sf.pubsub.subscribe("/data/AccountChangeEvent") as events:
        async for ev in events:
            ch = ev.change
            print(ch.change_type, ch.record_ids, ch.changed_fields)
            # ChangeType.UPDATE ('001...',) ('Phone', 'BillingAddress.City')
```

| Attribute | Value |
|---|---|
| `entity_name` | the object, such as `"Account"` |
| `change_type` | `ChangeType.CREATE`, `UPDATE`, `DELETE`, `UNDELETE`, or a `GAP_*` value |
| `is_gap` | `True` for every `GAP_*` value, including `GAP_OVERFLOW` |
| `is_overflow` | `True` for `GAP_OVERFLOW` |
| `record_ids` | the changed records; several ids if Salesforce merged identical changes |
| `changed_fields` | fields set by the change |
| `nulled_fields` | fields set to `null` |
| `diff_fields` | long text fields sent as a diff, not the full value |
| `transaction_key`, `sequence_number` | the transaction and the position in it |
| `commit_timestamp`, `commit_user`, `commit_number`, `change_origin` | when, who and how |

A component of a compound field is named `Parent.Child`, such as `BillingAddress.City`. An
unknown `changeType` raises `UnexpectedResponse`.

A gap event (`GAP_CREATE`, `GAP_UPDATE`, ...) has no field values. Salesforce sends it when it
can't build a normal change event. An overflow event (`GAP_OVERFLOW`) has no record ids. It
means one transaction changed too many records. In both cases, read the data again from
Salesforce. See [Replicate records](#replicate-records).

## Keep your place with a replay store

A replay store keeps the position of a subscription. After a restart, the client continues
after the last event you processed.

```python
store = esf.AsyncInMemoryReplayStore()  # lost on restart: use your own store in production


async def print_orders_from_last_position(sf: esf.AsyncSalesforce, store: esf.AsyncReplayStore) -> None:
    async with sf.pubsub.subscribe("/event/Order_Event__e", replay_store=store) as events:
        async for ev in events:
            print(ev.payload["Order_Number__c"])
```

Delivery is at least once:

- An event counts as processed when you ask for the next one, or when the block exits
  normally.
- The client saves the position after each batch you consumed, and when the block exits
  normally.
- If the block exits with an exception, the client doesn't save the current event.

After a crash, you get at most one batch again. Make your handler safe to run twice for the
same event. See [De-duplicate events](#de-duplicate-events).

A replay store has two methods. The key is `"{org_id}:{topic}"`, so a sandbox refresh or a
second org never continues from the position of another org.

```python
import sqlite3
import threading


class SqliteReplayStore:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS replay (key TEXT PRIMARY KEY, id BLOB)")

    def load(self, key: str) -> bytes | None:
        with self.lock:
            row = self.db.execute("SELECT id FROM replay WHERE key = ?", (key,)).fetchone()
        return bytes(row[0]) if row else None

    def save(self, key: str, replay_id: bytes) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO replay VALUES (?, ?)", (key, replay_id))
```

This store is for the sync client (`esf.ReplayStore`). For `AsyncSalesforce`, make `load` and
`save` coroutines (`esf.AsyncReplayStore`). Check your store with the contract suite in
`esf.testing`:

```python
import pytest
from even_simpler_salesforce.testing import ReplayStoreContract


class TestSqliteReplayStore(ReplayStoreContract):
    @pytest.fixture
    def make_store(self, tmp_path):
        return lambda: SqliteReplayStore(str(tmp_path / "replay.db"))
```

## Where to start

A position in the replay store wins. Without one, `replay=` decides:

| `replay=` | Starts with |
|---|---|
| `"latest"` (default) | new events only |
| `"earliest"` | every retained event, up to 72 hours old |
| a replay id (`bytes`) | the event after that id |

`"earliest"` can take a long time on a busy topic.

## When the stored position is too old

Salesforce keeps events for 72 hours. If the stored position is older, or comes from another
topic, Salesforce rejects it. The client then raises `ReplayUnavailable`, with the rejected id
in `exc.replay_id`.

```python
async def resume_or_resync(sf: esf.AsyncSalesforce, store: esf.AsyncReplayStore) -> None:
    try:
        async with sf.pubsub.subscribe("/data/AccountChangeEvent", replay_store=store) as events:
            async for ev in events:
                print(ev.change.change_type)
    except esf.ReplayUnavailable:
        print("missed events: re-sync Account, then subscribe with on_invalid_replay='latest'")
```

To continue without an error, pass `on_invalid_replay="latest"` or `"earliest"`. The client then
subscribes from there and emits `sf.pubsub.reconnect` with `reason="invalid_replay"`. The
default raises, because skipping or replaying three days of events is a decision for your app.

## Flow control

You pull events, so the client never gets ahead of your code. It holds at most `batch_size`
events that you haven't consumed yet. Requests for more events go out only after you take
events from the buffer.

```python
async def one_at_a_time(sf: esf.AsyncSalesforce) -> None:
    async with sf.pubsub.subscribe("/event/Order_Event__e", batch_size=10) as events:
        async for ev in events:
            print(ev.payload["Order_Number__c"])
```

`batch_size` is 1 to 100 (the server's limit), default 100. A smaller batch means fewer events
again after a crash, and more round trips.

If your code is slow, Salesforce closes the stream after 60 seconds without a request. The
client then subscribes again after the last processed event. You don't lose or skip events.

## Replicate records

This recipe keeps a copy of Account records. It follows the steps that Salesforce recommends
for transaction-based replication:

1. Buffer the events of a transaction, by `transaction_key`. Apply them when the next
   transaction starts.
2. On a gap event, read the records in `record_ids` again.
3. On an overflow event, read every record of the object again.

```python
FIELDS = ["Id", "Name", "Phone"]


async def replicate_accounts(sf: esf.AsyncSalesforce, store: esf.AsyncReplayStore, replica: dict) -> None:
    pending: list[esf.PubSubEvent] = []

    async def flush() -> None:
        for ev in pending:
            await apply(sf, ev, replica)
        pending.clear()

    async with sf.pubsub.subscribe("/data/AccountChangeEvent", replay_store=store) as events:
        async for ev in events:
            if pending and ev.change.transaction_key != pending[-1].change.transaction_key:
                await flush()
            pending.append(ev)
        await flush()


async def apply(sf: esf.AsyncSalesforce, ev: esf.PubSubEvent, replica: dict) -> None:
    ch = ev.change
    if ch.is_overflow:
        replica.clear()
        soql = f"SELECT {', '.join(FIELDS)} FROM {esf.soql_identifier(ch.entity_name)}"
        async for row in sf.query_iter(soql):
            replica[row["Id"]] = {f: row[f] for f in FIELDS}
    elif ch.is_gap:
        rows = await sf.collections.retrieve(ch.entity_name, ch.record_ids, FIELDS)
        for record_id, row in zip(ch.record_ids, rows):
            if row is None:
                replica.pop(record_id, None)
            else:
                replica[record_id] = {f: row[f] for f in FIELDS}
    elif ch.change_type is esf.ChangeType.DELETE:
        for record_id in ch.record_ids:
            replica.pop(record_id, None)
    else:
        for record_id in ch.record_ids:
            row = replica.setdefault(record_id, {"Id": record_id})
            for name in ch.changed_fields:
                if name in FIELDS:
                    row[name] = ev.payload[name]
            for name in ch.nulled_fields:
                if name in FIELDS:
                    row[name] = None
```

Reading records again costs API calls, and the fields to read depend on your app, so the
client never does it for you. Gap events can arrive in bursts. If many arrive at once, read
the whole object once instead of each record.

## De-duplicate events

Replay ids aren't unique across org migrations. Never use them as a key to find duplicates.

- Platform events: use `ev.event_id`.
- Change events: use `(ev.change.transaction_key, ev.change.sequence_number)`. Change events
  also carry an `ev.event_id`, but Salesforce documents the id for platform events only, so
  don't rely on it.

## Errors and reconnects

The client reconnects after the last processed event, with or without a replay store. Events
that were in the buffer and not yet consumed come again.

| Salesforce reports | The client |
|---|---|
| an authentication error (`UNAUTHENTICATED`) | refreshes the token once, then subscribes again; a second failure raises `SessionExpired` |
| a rejected replay id | raises `ReplayUnavailable`, or uses `on_invalid_replay` |
| an unknown topic | raises `NotFound` |
| no permission for the topic | raises `Forbidden` (a `tenant.license` error is tried once more first) |
| an allocation or rate limit (`RESOURCE_EXHAUSTED`) | raises `PubSubLimitExceeded` |
| the service is unavailable, or the stream reset | reconnects with backoff from your `RetryPolicy` |
| the org moved to another instance (`org.migrated`) | waits 30 minutes, then reconnects |
| anything else | raises `PubSubError` |

When the attempts of your `RetryPolicy` run out, the client raises the last error. The counter
starts again after an event arrives. Every Pub/Sub error has the Pub/Sub `error-code` in
`exc.error_code`, and the `rpcId` in `exc.request_id`. Give both to Salesforce Support.

```python
async def run_forever(sf: esf.AsyncSalesforce, store: esf.AsyncReplayStore) -> None:
    try:
        await print_orders_from_last_position(sf, store)
    except esf.PubSubLimitExceeded as exc:
        print("limit:", exc.error_code)
    except esf.PubSubError as exc:
        print("Pub/Sub error:", exc.error_code, exc.request_id)
```

Streams emit `sf.pubsub.subscribed`, `sf.pubsub.batch`, `sf.pubsub.reconnect` and
`sf.pubsub.closed`, not `sf.request`. See [Logging](../logging.md#pubsub-events). Payloads are
never logged.

## Sync client

The sync client has the same interface. Iterating blocks, so run each subscription in its own
thread.

```python
import threading


def print_orders_sync(sf: esf.Salesforce) -> None:
    with sf.pubsub.subscribe("/event/Order_Event__e", replay_store=esf.InMemoryReplayStore()) as events:
        for ev in events:
            print(ev.payload["Order_Number__c"])


def start_in_thread(sf: esf.Salesforce) -> threading.Thread:
    worker = threading.Thread(target=print_orders_sync, args=(sf,), daemon=True)
    worker.start()
    return worker
```

## Test your code

`FakeSalesforce.pubsub` and `AsyncFakeSalesforce.pubsub` feed `subscribe()` without gRPC. Emit
events with `emit()` and `emit_change()`, and make the next read fail with `fail_next()`. With
the fake, the loop ends when every emitted event was yielded.

```python
async def test_replica_handles_a_gap(esf_async_fake):
    sf = esf_async_fake
    acme = sf.add("Account", {"Name": "Acme", "Phone": "555-0100"})
    sf.pubsub.emit_change("Account", "UPDATE", [acme], changed_fields=["Phone"], values={"Phone": "555-0199"})
    sf.pubsub.emit_change("Account", "GAP_UPDATE", [acme])

    replica: dict = {}
    await replicate_accounts(sf, esf.AsyncInMemoryReplayStore(), replica)
    assert replica[acme] == {"Id": acme, "Name": "Acme", "Phone": "555-0100"}


async def test_old_position_is_reported(esf_async_fake):
    esf_async_fake.pubsub.fail_next(esf.ReplayUnavailable("too old"))
    await resume_or_resync(esf_async_fake, esf.AsyncInMemoryReplayStore())


def test_orders_print(esf_fake, capsys):
    esf_fake.pubsub.emit("/event/Order_Event__e", {"Order_Number__c": "A-1001"})
    print_orders_sync(esf_fake)
    assert "A-1001" in capsys.readouterr().out
```

The fake has no history from before the test: `replay="latest"` and `"earliest"` both start at
the first emitted event. Change events from `emit_change()` carry field names in their
`ChangeEventHeader`, not bitmaps. See [Testing](../testing.md#pubsub).

## Move from the official Salesforce sample

Salesforce publishes a Python sample client in
[`forcedotcom/pub-sub-api`](https://github.com/forcedotcom/pub-sub-api). It shows the gRPC
calls. The client does the same work for you:

| In the sample | With `sf.pubsub` |
|---|---|
| SOAP `login()` with a username and password | any `Auth` of the client, such as `ClientCredentials` or `JwtBearer` |
| generated stubs and a `grpc` channel in your code | `[pubsub]` extra; one channel per client |
| a `threading.Semaphore` and a request generator | the `async for` loop is the flow control |
| `GetSchema` and Avro decoding by hand | `ev.payload` (schemas cached) |
| `bitstring` to decode `changedFields` | `ev.change.changed_fields` as field names |
| keeping the replay id yourself | `replay_store=` |
| reading `error-code` trailers and reconnecting | typed errors and reconnects (see above) |
| gap and overflow events left to you | `ev.change.is_gap`, `is_overflow`, `record_ids` |

A loop from the sample becomes this:

```python
async def from_the_sample(sf: esf.AsyncSalesforce, store: esf.AsyncReplayStore) -> None:
    async with sf.pubsub.subscribe(
        "/data/AccountChangeEvent", replay="earliest", replay_store=store, batch_size=100
    ) as events:
        async for ev in events:
            print(ev.change.change_type, ev.change.changed_fields, ev.payload.get("Name"))
```

Keep the sample's topic name. Replace the login with an `Auth` of your connected app. Replace the
code that stores the replay id with a replay store.

## Limitations

- Subscribing only. To publish, use [platform events](platform-events.md).
- No managed subscriptions (`ManagedSubscribe`): that RPC is Beta.
- One gRPC channel carries at most 1,000 concurrent streams. gRPC queues more in the client.
- An org has at most 200 managed subscriptions. The delivery allocations of CDC and platform
  events apply to Pub/Sub subscribers.
- Event filters and enrichment are channel settings in Salesforce, not client options.

## How it works

- The client connects to `api.pubsub.salesforce.com:443`. To keep data in transit in the EU,
  pass `pubsub_endpoint="api.deu.pubsub.salesforce.com:443"` to the client.
- Each stream sends the access token, the instance URL and the org id as gRPC metadata. If the
  token has no org id, such as a `StaticToken`, the client calls
  `/services/oauth2/userinfo` once and caches the result.
- All subscriptions of a client and its `with_auth()` children share one gRPC channel. Closing
  the client closes it.
- While events are requested but none arrive, Salesforce sends an empty response as a
  keepalive at least every 270 seconds. The client never yields it. If nothing is buffered, the client saves its `latest_replay_id`, so a restart
  doesn't go back far.
- Only the first request of a stream sets the start position. Later requests ask for more
  events only.
- Salesforce sends Date and DateTime values as a `long` of epoch milliseconds. Only the Avro
  `doc` attribute says which is which. A field is a DateTime when its `doc` is
  `"CreatedDate:DateTime"`, `"Data:DateTime"` or `"Data:DateTime:00N…"`, or when it has a
  `timestamp-millis` logical type. A field is a Date when its `doc` is `"Data:DateOnly:00N…"`
  (the value is midnight UTC of that day), or when it has a `date` logical type.
