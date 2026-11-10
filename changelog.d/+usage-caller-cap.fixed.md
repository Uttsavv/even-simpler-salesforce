`calls_by_caller` counts at most 1,000 distinct caller tags, so tags with many values can't grow memory without bound. Calls with a new tag after that count under `"other"`.
