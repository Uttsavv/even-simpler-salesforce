# Metadata API

`sf.metadata` deploys and retrieves metadata such as Apex classes, objects and fields. Use it
to move configuration between orgs, or to back up metadata from a script.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

retrieved = await sf.metadata.retrieve({"ApexClass": ["*"]}, extract_to="src")
print([p.full_name for p in retrieved.file_properties])  # ['InvoiceService', 'LeadScoring']

job = await sf.metadata.deploy("src", options=esf.DeployOptions(check_only=True))
result = await sf.metadata.deploy_status(job.id, wait=True)
result.raise_for_errors()
print(result.status, result.components_deployed)  # Succeeded 2
```

Your user needs the "Modify Metadata Through Metadata API Functions" or the "Modify All Data"
permission.

## Deploy metadata

`deploy()` takes a folder, a path to a zip file, or the zip file as `bytes`. The folder or the
zip must have `package.xml` at its root. `deploy()` starts the deployment and returns at once
with status `Pending`.

```python
from pathlib import Path

Path("src/classes/Hello.cls").write_text("public class Hello {}\n")
Path("src/classes/Hello.cls-meta.xml").write_text(
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<ApexClass xmlns="http://soap.sforce.com/2006/04/metadata">'
    "<apiVersion>68.0</apiVersion><status>Active</status></ApexClass>"
)
job = await sf.metadata.deploy("src")
print(job.id, job.status)  # 0Af... Pending
```

The client zips a folder on the fly. A large folder goes to a temporary file, not to memory.
Every symlink in the folder must point to a file inside it. A symlink that points outside the
folder raises `ValueError`, and the client sends nothing.

## Wait for a deployment

`deploy_status(id, wait=True)` polls until the deployment is done. The waits between polls
follow `poll`, an `esf.Backoff`: by default 2 seconds, doubling up to 30 seconds. The call
waits at most `timeout` seconds, an hour by default. Bulk jobs and report runs wait the same
way.

```python
result = await sf.metadata.deploy_status(job.id, wait=True, poll=esf.Backoff(start=5, max=60), timeout=1800)
print(result.done, result.success)  # True True
```

Without `wait`, `deploy_status()` makes one call and returns the current state. Use that to
show progress, for example `result.components_deployed` of `result.components_total`.

If the deployment is still running after `timeout` seconds, the call raises
`esf.MetadataTimeout`. The deployment keeps running in Salesforce. Call `deploy_status()` again
with `exc.job_id` to keep waiting.

## Handle a failed deployment

`deploy_status()` returns a failed deployment like any other. Call `raise_for_errors()` to raise
`esf.DeployFailed` when the deployment failed, was canceled, deployed only some components or
ran Apex tests that failed. The last case matters in sandboxes and Developer Edition orgs: with
`rollback_on_error` off, Salesforce reports such a deployment as `Succeeded`, and `result.failed`
is still `True`.

```python
Path("src/classes/Broken.cls").write_text("public class Broken {\n")
job = await sf.metadata.deploy("src")
result = await sf.metadata.deploy_status(job.id, wait=True)
try:
    result.raise_for_errors()
except esf.DeployFailed as exc:
    for failure in exc.result.component_failures:
        print(failure.file_name, failure.line, failure.problem)
        # classes/Broken.cls 1 Unexpected token 'Broken'.
```

`exc.errors` has one `ErrorDetail` per failed component and per failed Apex test.
`exc.result` is the full `DeployResult`, with `component_failures` and `test_failures`.

## Deploy options

`DeployOptions` holds the Metadata API `deployOptions`. All options are optional.

```python
options = esf.DeployOptions(
    check_only=True,
    test_level="RunSpecifiedTests",
    run_tests=["InvoiceServiceTest"],
)
Path("src/classes/Broken.cls").unlink()
job = await sf.metadata.deploy("src", options=options)
```

| Option | Default | What it does |
|---|---|---|
| `check_only` | `False` | Validate and run tests, but save nothing. |
| `rollback_on_error` | `None` | Roll back everything if one component fails. `None` leaves it to Salesforce. Production orgs need `True`. |
| `ignore_warnings` | `False` | Deploy even if there are warnings. |
| `test_level` | `None` | `NoTestRun`, `RunSpecifiedTests`, `RunLocalTests` or `RunAllTestsInOrg`. `None` uses the Salesforce default. |
| `run_tests` | `()` | Apex test classes to run. Needs `test_level="RunSpecifiedTests"`. |
| `purge_on_delete` | `False` | Delete the components in `destructiveChanges.xml` without the Recycle Bin. Not allowed in production orgs. |
| `allow_missing_files` | `False` | Deploy even if files in `package.xml` are missing. |
| `auto_update_package` | `False` | Add files that `package.xml` doesn't list. |
| `single_package` | `True` | `package.xml` is at the root of the zip. Set `False` only for a zip with one folder per package. |

## Deploy a validated deployment

A validation (`check_only=True`) runs the Apex tests but saves nothing. If it succeeded, you can
deploy the same components later without running the tests again. Salesforce calls this a
quick deploy. Pass the id of the validation to `quick_deploy()`.

```python
check = await sf.metadata.deploy(
    "src", options=esf.DeployOptions(check_only=True, test_level="RunLocalTests")
)
validated = await sf.metadata.deploy_status(check.id, wait=True)
validated.raise_for_errors()

job = await sf.metadata.quick_deploy(validated.id)
result = await sf.metadata.deploy_status(job.id, wait=True)
print(result.status, result.check_only)  # Succeeded False
```

`quick_deploy()` starts a new deployment with its own id. Salesforce accepts a validation only
if it succeeded and ran Apex tests, and if no other deployment ran in the org since. It refuses
any other id with an `esf.RequestError` whose `error_code` is `INVALID_ID_FIELD`.

## Cancel a deployment

`cancel_deploy()` asks Salesforce to stop a deployment that is still running. The cancel runs
in Salesforce: the call returns with status `Canceling`. Wait with `deploy_status()` until the
status is `Canceled`. A deployment that has already finished comes back unchanged, with its
final status.

```python
job = await sf.metadata.deploy("src")
try:
    await sf.metadata.cancel_deploy(job.id)
except esf.DeployNotCancelable:
    pass  # already finalizing: it finishes on its own
result = await sf.metadata.deploy_status(job.id, wait=True)
print(result.status)  # Canceled
```

From API 65.0, Salesforce can't cancel a deployment that has reached the `FinalizingDeploy`
stage. The call then raises `esf.DeployNotCancelable`, and the deployment finishes on its own.

## Retrieve metadata

`retrieve()` takes a mapping of metadata type to member names, or the text of a
`package.xml`. Use `"*"` for every component of a type. The call waits until Salesforce has
built the zip file.

```python
package = """<?xml version="1.0" encoding="UTF-8"?>
<Package xmlns="http://soap.sforce.com/2006/04/metadata">
    <types><members>InvoiceService</members><name>ApexClass</name></types>
    <version>68.0</version>
</Package>"""
result = await sf.metadata.retrieve(package)
print(len(result.zip_file) > 0, result.file_properties[0].file_name)  # True classes/InvoiceService.cls
```

`result.zip_file` is the zip as `bytes`, with `package.xml` at its root. With `extract_to=`, the
client also writes the files into that folder. `result.messages` lists components that
Salesforce couldn't get, such as a name that doesn't exist. The retrieve still succeeds.

The client checks every path in the zip before it writes a file. An absolute path, a `..`
part or a symlink raises `esf.UnexpectedResponse`, and the client writes nothing.

If Salesforce can't run the retrieve, the call raises `esf.RetrieveFailed`. If the retrieve is
still running after `timeout` seconds, it raises `esf.MetadataTimeout`.

## List components

`list_metadata(type)` returns one `FileProperties` per component of a type. Pass `folder=` for
types that live in folders, such as `Report`, `Dashboard`, `Document` and `EmailTemplate`.

```python
for p in await sf.metadata.list_metadata("ApexClass"):
    print(p.full_name, p.last_modified_date)  # InvoiceService 2026-11-02T10:00:00.000Z
```

## Describe the metadata types

`describe_metadata()` returns the metadata types that the org supports, with their folders and
file suffixes.

```python
describe = await sf.metadata.describe_metadata()
apex = next(t for t in describe.types if t.xml_name == "ApexClass")
print(apex.directory_name, apex.suffix, apex.meta_file)  # classes cls True
```

## Test code that uses the Metadata API

`FakeSalesforce` has a `metadata` area. Deployments succeed at once unless you script another
outcome with `fake.on_deploy()`. Retrieve, `list_metadata()` and `describe_metadata()` read the
components that you add with `fake.add_metadata()`.

```python
from even_simpler_salesforce.testing import FakeSalesforce


def test_deploy_validation_failure(esf_fake: FakeSalesforce) -> None:
    esf_fake.on_deploy(esf.DeployResult("0Af", "Failed", done=True, error_message="Tests failed"))

    job = esf_fake.metadata.deploy(Path("src"), options=esf.DeployOptions(check_only=True))
    result = esf_fake.metadata.deploy_status(job.id, wait=True)

    assert result.failed
    assert esf_fake.deployed_zip(job.id) is not None


def test_retrieve(esf_fake: FakeSalesforce) -> None:
    esf_fake.add_metadata("ApexClass", "Hello", {"classes/Hello.cls": "public class Hello {}"})

    result = esf_fake.metadata.retrieve({"ApexClass": ["*"]})

    assert [p.full_name for p in result.file_properties] == ["Hello"]
```

`quick_deploy()` accepts a fake validation that succeeded. `cancel_deploy()` returns
`Canceling`, and the next `deploy_status()` returns `Canceled`. To test the refusal of a
finalizing deployment, use
`fake.fail_next("metadata.cancel_deploy", esf.DeployNotCancelable(...))`.

The operation names for `fail_next()` are `"metadata.deploy"`, `"metadata.quick_deploy"`,
`"metadata.cancel_deploy"`, `"metadata.deploy_status"`, `"metadata.retrieve"`,
`"metadata.list"` and `"metadata.describe"`. See
[Testing](../testing.md).

## How it works

- `deploy()` sends `POST /services/data/vXX.X/metadata/deployRequest` with a multipart body: a
  `json` part with `deployOptions` and a `file` part with the zip. The client streams the zip.
- `deploy_status()` sends `GET metadata/deployRequest/{id}?includeDetails=true`.
- `quick_deploy()` sends `POST metadata/deployRequest/{id}` with the body
  `{"validatedDeployRequestId": id}`. It is a `POST` on purpose: Salesforce documents that a
  `PATCH` with this body starts a new deployment.
- `cancel_deploy()` sends `PATCH metadata/deployRequest/{id}` with the body
  `{"deployResult": {"status": "Canceling"}}`.
- Retrieve, `list_metadata()` and `describe_metadata()` have no REST resource. They use the
  SOAP Metadata API at `/services/Soap/m/XX.X` with your OAuth access token. You never see the
  SOAP part.
- `retrieve()` starts a job with `retrieve`, then polls `checkRetrieveStatus` without the zip.
  When the job is done, one more call gets the zip.
- The client never resends `deploy()`, `quick_deploy()`, `cancel_deploy()` or `retrieve()`
  after the request may have reached Salesforce, so a timeout can't start a second job. Status polls, `list_metadata()` and
  `describe_metadata()` are reads. The client resends them like a `GET`.
- Salesforce deletes the zip of a retrieve once it has returned it. For this reason the client
  never resends the call that gets the zip.
- If the session expires, the client refreshes the token once and sends the call again with the
  new token.

## Limitations

- A deploy zip can be at most 39 MB and 10,000 files. This is a Salesforce limit.
- A retrieve zip arrives in one response, base64-encoded, and the client holds it in memory.
  Retrieve large orgs in parts.
- `retrieve()` gets unpackaged components only. It doesn't retrieve a managed package by name.
