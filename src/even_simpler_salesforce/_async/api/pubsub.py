"""``sf.pubsub``: subscribe to platform events and Change Data Capture events (§9.16, D31)."""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING

from ...core import pubsub as ps

if TYPE_CHECKING:
    from ..client import AsyncSalesforce
    from ..pubsub_transport import AsyncSubscription
    from ..replay_store import AsyncReplayStore

__all__ = ["AsyncPubSub"]

_EXTRA_MODULES = ("grpc", "google.protobuf", "fastavro")


def _require_extra() -> None:
    for name in _EXTRA_MODULES:
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            raise ps.missing_extra(ImportError(f"No module named {name!r}", name=name))


class AsyncPubSub:
    """Pub/Sub API subscriptions over gRPC. Needs the ``[pubsub]`` extra.

    Example:
        async with sf.pubsub.subscribe("/data/AccountChangeEvent") as events:
            async for ev in events:
                print(ev.change.change_type, ev.change.record_ids)

    To publish platform events, use ``sf.platform_events``.
    """

    def __init__(self, client: AsyncSalesforce) -> None:
        self._sf = client

    def subscribe(
        self,
        topic: str,
        *,
        replay: ps.Replay = "latest",
        replay_store: AsyncReplayStore | None = None,
        on_invalid_replay: ps.OnInvalidReplay = "raise",
        batch_size: int = ps.MAX_BATCH_SIZE,
    ) -> AsyncSubscription:
        """Subscribe to ``topic``. Use the result as a context manager and iterate it.

        Delivery is at least once. An event counts as processed when you ask for the next one,
        or when the block exits normally. With a ``replay_store`` the client saves the position
        after each fully consumed batch and on a normal exit, never when the block raises. A
        restart therefore redelivers at most one batch. Reconnects resume after the last
        processed event, with or without a store.

        Args:
            topic: ``"/event/Order_Event__e"`` for a platform event, or a change event channel
                such as ``"/data/AccountChangeEvent"`` or ``"/data/ChangeEvents"``.
            replay: Where to start when the store has no position: ``"latest"`` (new events
                only), ``"earliest"`` (everything retained, up to 72 hours) or a replay id to
                start after.
            replay_store: Loads the start position and saves progress. Its key is
                ``"{org_id}:{topic}"``.
            on_invalid_replay: If Salesforce rejects the start position (for example it is older
                than 72 hours): ``"raise"`` raises :class:`ReplayUnavailable`; ``"latest"`` or
                ``"earliest"`` resubscribe from there.
            batch_size: Most events requested but not yet consumed (1 to 100).

        Returns:
            An unopened subscription; ``async with`` opens the stream.

        Raises:
            ValueError: a bad argument.
            ImportError: the ``[pubsub]`` extra is not installed.
        """
        ps.check_subscribe(topic, replay, on_invalid_replay, batch_size)
        _require_extra()
        from ..pubsub_transport import AsyncSubscription

        return AsyncSubscription(
            self._sf,
            topic,
            replay=replay,
            replay_store=replay_store,
            on_invalid_replay=on_invalid_replay,
            batch_size=batch_size,
        )
