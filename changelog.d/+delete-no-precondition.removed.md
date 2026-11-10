`sobject.delete()` no longer takes `if_unmodified_since=`. Salesforce ignores conditional headers on `DELETE`, so the option deleted a changed record instead of raising `Conflict`.
