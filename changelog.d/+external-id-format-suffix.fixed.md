`sobject(...).get_by_external_id()` and `upsert()` now work for external id values that end in
`.json`, `.xml` or `.html`. Salesforce reads those endings of the path as a response format and
answered `404 NOT_FOUND`; for such values the client now finds the record with SOQL (then reads it
by id) or upserts it through sObject Collections, with the same results and errors.
