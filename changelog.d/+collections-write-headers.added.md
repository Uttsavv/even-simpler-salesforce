`sf.collections.create()`, `update()` and `upsert()` take `assignment_rule_id=` and `allow_save=`
(`Sforce-Auto-Assign` and `Sforce-Duplicate-Rule-Header`, sent on every chunk). A sandbox check
confirmed that Salesforce applies both headers to sObject Collections.
