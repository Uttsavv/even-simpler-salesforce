`esf.DuplicateRules(allow_save=False, include_record_details=False, run_as_current_user=False)`
controls the org's duplicate rules (`Sforce-Duplicate-Rule-Header`). Pass it as
`duplicate_rules=` to `create()`, `update()` and `upsert()` on `sf.sobject(...)` and
`sf.collections`, and on `FakeSalesforce`. With `include_record_details=True`,
`DuplicateDetected.duplicates` carries fields of the matched records on single-record writes.
