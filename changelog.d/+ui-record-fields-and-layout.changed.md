`sf.ui.record()` now raises `ValueError` when you pass both `fields` and `layout_types`. Salesforce refuses the combination with HTTP 400 `INVALID_INPUT_COMBINATION`, so the call could never succeed.
