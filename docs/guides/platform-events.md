# Platform events

`sf.platform_events` publishes platform events, such as `Order_Event__e`. Use it to tell
Flows, Apex triggers and other subscribers that something happened.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

result = await sf.platform_events.publish("Order_Event__e", {"Order_Number__c": "A-1001"})
print(result.ok, result.uuid)  # True 5e4b3c2a-...
```

A successful publish means Salesforce queued the event. Subscribers get it later. `uuid` is the
event's `EventUuid`, which subscribers see too, so you can match the two.

## Publish many events

`publish_many()` sends the events through sObject Collections, 200 per request. It returns one
`PublishResult` per event, in your order.

```python
rows = [{"Order_Number__c": f"A-{n}"} for n in range(1002, 1006)]
batch = await sf.platform_events.publish_many("Order_Event__e", rows)
print(len(batch.succeeded), len(batch.failed))  # 4 0
batch.raise_for_errors()
```

Each event publishes on its own. Check `batch.failed`, or call `batch.raise_for_errors()` to
raise `PartialFailure`.

## Errors

A failed publish raises a `RecordError` subclass.

| Status code | Class |
|---|---|
| `PLATFORM_EVENT_PUBLISHING_UNAVAILABLE` | `EventPublishUnavailable` (`retryable`) |
| `LIMIT_EXCEEDED`, `PLATFORM_EVENT_PUBLISH_FAILED`, other codes | `EventPublishFailed` |
| a known record error, such as `REQUIRED_FIELD_MISSING` | its usual class |

```python
try:
    await sf.platform_events.publish("Order_Event__e", {"Order_Number__c": "A-1006"})
except esf.EventPublishUnavailable:
    pass  # try again later
except esf.EventPublishFailed as exc:
    print(exc.error_code)  # LIMIT_EXCEEDED: the hourly allocation is used up
```

The client doesn't resend a publish after it may have reached Salesforce. A resend could
publish the event twice. If you get `EventPublishUnavailable`, retry the publish yourself.

## Limitations

- Publishing only. To subscribe, use the Pub/Sub API, planned for 0.5.
- `ok=True` means queued, not delivered. Salesforce can still fail a queued event later. It
  reports that failure on the `/event/AsyncOperationEvent` channel, not to the publisher.
