The describe cache now revalidates with `If-None-Match` when Salesforce sent an `ETag`, so stale `sf.ui.object_info()` and `sf.ui.picklist_values()` entries get a `304` instead of a full download.
