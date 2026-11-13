`allow_save=` on `create()`, `update()` and `upsert()` (`sf.sobject(...)`, `sf.collections` and
`FakeSalesforce`) is deprecated in favour of `duplicate_rules=esf.DuplicateRules(allow_save=...)`.
It still sends the same header, but it emits `EsfDeprecationWarning`, and passing both raises
`TypeError`. It will be removed in 0.7.
