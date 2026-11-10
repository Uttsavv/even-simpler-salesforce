"""Platform event publish results and errors (§9.10). No I/O.

Publishing an event is a create on the event's sObject (``POST sobjects/{Event__e}``, or
``composite/sobjects`` for many). Salesforce reports a queued event as ``success: true`` with
one ``OPERATION_ENQUEUED`` entry in ``errors`` whose ``message`` is the event UUID; that entry
is not an error.

Sources (Platform Events Developer Guide):

- Publish with Salesforce APIs: https://developer.salesforce.com/docs/atlas.en-us.platform_events.meta/platform_events/platform_events_publish_api.htm
- EventUuid: https://developer.salesforce.com/docs/atlas.en-us.platform_events.meta/platform_events/platform_events_event_uuid.htm
- Error status codes: https://developer.salesforce.com/docs/atlas.en-us.platform_events.meta/platform_events/platform_events_error_status_codes.htm
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from .. import errors as E
from ..models import PublishResult, RecordResult
from .classify import parse_details
from .http import check_sobject

__all__ = ["ENQUEUED", "check_event_name", "event_error", "parse_publish", "publish_result"]

#: Status code of the entry that carries the event UUID of a queued event.
ENQUEUED = "OPERATION_ENQUEUED"

# EventBus codes with a class of their own; any other code that would be a plain RecordError
# becomes EventPublishFailed.
_EVENT_CODES: dict[str, type[E.SalesforceError]] = {
    "PLATFORM_EVENT_PUBLISHING_UNAVAILABLE": E.EventPublishUnavailable,
    "PLATFORM_EVENT_PUBLISH_FAILED": E.EventPublishFailed,
    "LIMIT_EXCEEDED": E.EventPublishFailed,
}


def check_event_name(name: str) -> str:
    """Validate a platform event API name (it ends in ``__e``)."""
    check_sobject(name)
    if not name.endswith("__e"):
        raise ValueError(f"platform event names end in '__e': {name!r}")
    return name


def publish_result(result: RecordResult) -> PublishResult:
    """Turn a create result into a :class:`PublishResult`, moving ``OPERATION_ENQUEUED`` to ``uuid``."""
    uuid: str | None = None
    errors: list[E.ErrorDetail] = []
    for d in result.errors:
        if d.error_code == ENQUEUED and uuid is None:
            uuid = d.message or None
        else:
            errors.append(d)
    return PublishResult(ok=result.ok, id=result.id, uuid=uuid, errors=tuple(errors))


def parse_publish(body: Any) -> PublishResult:
    """Parse the body of ``POST sobjects/{Event__e}``.

    Raises:
        UnexpectedResponse: the body is not a create result.
    """
    if not isinstance(body, Mapping) or "success" not in body:
        raise E.UnexpectedResponse(
            "unexpected platform event publish response", raw=repr(cast(object, body))[:4096]
        )
    item = cast("Mapping[str, Any]", body)
    rid = item.get("id")
    return publish_result(
        RecordResult(
            ok=bool(item.get("success")),
            id=str(rid) if rid is not None else None,
            errors=parse_details(item.get("errors") or []),
        )
    )


def event_error(exc: E.SalesforceError) -> E.SalesforceError:
    """Map an error from a publish call to its ``EventBus`` class (§9.10).

    Codes listed for platform events get their class (:class:`EventPublishUnavailable`,
    :class:`EventPublishFailed`). Any other code that classified as a plain
    :class:`RecordError` becomes :class:`EventPublishFailed`. Everything else, such as
    :class:`RequiredFieldMissing` or :class:`SessionExpired`, is returned unchanged.
    """
    target = _EVENT_CODES.get(exc.error_code or "")
    if target is None:
        if type(exc) is not E.RecordError:
            return exc
        target = E.EventPublishFailed
    if type(exc) is target:
        return exc
    state = dict(exc.__dict__)
    state["retryable"] = target.default_retryable
    new = E._rebuild(target, state)  # pyright: ignore[reportPrivateUsage]
    new.__cause__ = exc.__cause__
    return new
