A call that waits before a retry no longer holds its `max_concurrency` place, so it doesn't block other calls for the whole backoff.
