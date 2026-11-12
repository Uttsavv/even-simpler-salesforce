`sf.metadata.deploy_status()` no longer fails with `UnexpectedResponse` when Salesforce reports the
status `Finalizing` (seen on a REST quick deploy); it is treated as still in progress.
