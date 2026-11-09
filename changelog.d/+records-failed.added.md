Collections `sf.request` events carry `records_failed`, and the `capture()` roll-up has `sf_records_failed`, so partial failures are visible in logs even when `raise_for_errors()` is never called.
