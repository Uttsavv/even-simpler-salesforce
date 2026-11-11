`sobject.get()` is typed as returning `... | None` when you pass `if_modified_since=`, because it returns `None` on `304 Not Modified`.
