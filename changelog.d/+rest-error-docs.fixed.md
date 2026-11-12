The docs for `UnsupportedApiVersion` and `sobject(...).delete()` now match what Salesforce does: an
`api_version` newer than the org's gives `NotFound` (`NOT_FOUND`), and an `If-Unmodified-Since`
header on DELETE is rejected (the record is kept), not ignored.
