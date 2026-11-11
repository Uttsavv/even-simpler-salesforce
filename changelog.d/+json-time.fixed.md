`datetime.time` values encode as `isoformat()` strings with both JSON backends. Previously the stdlib backend raised `TypeError` and orjson rejected a `time` with a timezone.
