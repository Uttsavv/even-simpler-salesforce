`ErrorDetail` and `RecordResult` are hashable. `ErrorDetail.extended` still counts for `==` but not for `hash()`.
