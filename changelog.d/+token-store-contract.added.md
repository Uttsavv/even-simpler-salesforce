`TokenStoreContract` and `AsyncTokenStoreContract` check that `lock()` excludes a second holder. They also check that a compare-and-swap save against a missing key conflicts.
