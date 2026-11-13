# Invocable actions

`sf.actions` runs Flows, Apex `@InvocableMethod` classes and standard actions. Use it to run
logic that lives in the org.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

lead_id = await sf.sobject("Lead").create({"LastName": "Lovelace", "Company": "Analytical"})
out = await sf.actions.flow("Assign_Lead", {"leadId": lead_id, "region": "North"})
print(out["assignedTo"], out["Flow__InterviewStatus"])  # '005...' 'Finished'
```

You pass the inputs as a dict. The call returns the output values as a dict.

## Run a Flow

Pass the Flow API name and its input variables. The Flow must be an autolaunched Flow.
For a packaged Flow, include the namespace prefix: `"acme__Assign_Lead"`.

The example at the top of this page runs a Flow.

## Run an Apex action

Pass the name of the Apex class, not the name of the method.

```python
out = await sf.actions.apex("LeadScoring", {"leadId": lead_id})
print(out["score"])  # 42
```

## Run a standard action

Standard actions come with Salesforce, for example `chatterPost` or `emailSimple`.

```python
out = await sf.actions.standard("chatterPost", {"text": "New lead assigned", "subjectNameOrId": lead_id})
print(out["feedItemId"])  # '0D5...'
```

## Run many inputs at once

Pass a list of dicts to run the action once per input in one request. The call returns a
`BatchResult` with one `ActionResult` per input, in the order you gave.

```python
results = await sf.actions.flow(
    "Assign_Lead",
    [{"leadId": lead_id, "region": "North"}, {"region": "South"}],  # the second has no leadId
)
for res in results:
    print(res.ok, res.outputs, [e.error_code for e in res.errors])
```

A failed input does not raise. Check `res.ok`, or call `results.raise_for_errors()` to raise
`esf.PartialFailure`. Salesforce answers with HTTP 400 when every input failed and with 200
otherwise; the client treats both the same. Each Flow input runs as its own interview, so one
input can fail alone. An Apex action gets all inputs in one call, so an exception that the
method doesn't catch fails every input of the request:

```python
try:
    results.raise_for_errors()
except esf.PartialFailure as exc:
    print(exc.error_code, exc.fields)  # REQUIRED_FIELD_MISSING ('leadId',)
```

`ActionResult` has these attributes:

| Attribute | Type | Description |
|---|---|---|
| `ok` | `bool` | `isSuccess` from Salesforce |
| `outputs` | `Mapping[str, Any]` | the output values. For a failed Flow interview, only `Flow__InterviewGuid` and `Flow__InterviewStatus` (`"Error"`) |
| `errors` | `tuple[ErrorDetail, ...]` | the errors if the input failed |
| `action_name` | `str \| None` | the action name that Salesforce reports |

## Handle errors

If one input fails, the call raises the typed error for its first error, for example
`esf.RequiredFieldMissing` or `esf.ValidationRuleFailed`. A Flow "Custom Error" element works with
[custom error tags](../errors.md#custom-errors-with-tags), like a validation rule.

```python
class RegionUnknown(esf.ValidationRuleFailed): ...


tagged = esf.AsyncSalesforce(auth, error_tags={"REGION_UNKNOWN": RegionUnknown})
try:
    await tagged.actions.flow("Assign_Lead", {"leadId": lead_id, "region": "Mars"})
except RegionUnknown as exc:
    print(exc.message)  # Region Mars does not exist
```

An unknown error code raises `esf.RecordError`, with `.error_code` set.

## Use typed stubs

`esf generate actions` reads the describe of each action and writes one class per action. Each
class lists the inputs with their types, so your editor can complete them and pyright can check
them. See [the CLI page](../cli.md#generate-action-stubs) for the command.

```sh
esf generate actions --include flow:Assign_Lead --include apex:* --out myapp/sf_actions.py
```

The file holds frozen dataclasses like these. With `--pydantic` it holds Pydantic models
instead.

```python
from dataclasses import dataclass, field


@dataclass(frozen=True, kw_only=True)
class AssignLeadResult:
    """Output values of flow:Assign_Lead."""

    assigned_to: str | None = field(default=None, metadata={"api_name": "assignedTo"})
    flow_interview_status: str | None = field(default=None, metadata={"api_name": "Flow__InterviewStatus"})


@dataclass(frozen=True, kw_only=True)
class AssignLead(esf.Action[AssignLeadResult]):
    __action__ = ("flow", "Assign_Lead")
    lead_id: str = field(metadata={"api_name": "leadId"})
    region: str | None = field(default=None, metadata={"api_name": "region"})
```

Run a stub with `sf.run()`. It returns the result class:

```python
result = await sf.run(AssignLead(lead_id=lead_id, region="North"))
print(result.assigned_to, result.flow_interview_status)  # '005...' 'Finished'
```

The client sends each field under its `api_name`. It does not send fields that are `None`.
Failures raise the same typed errors as `sf.actions.flow()`.

You can write a stub by hand too. Subclass `esf.Action[ResultType]`, set `__action__` to
`(kind, name)` and make the class a dataclass or a Pydantic model. The kind is `"standard"`,
`"flow"` or `"apex"`.

## List and describe actions

`sf.actions.list(kind)` returns the actions of one kind. `sf.actions.describe(kind, name)`
returns the inputs and outputs of one action.

```python
flows = await sf.actions.list("flow")
print([a["name"] for a in flows])  # ['Assign_Lead']

info = await sf.actions.describe("flow", "Assign_Lead")
print([(i["name"], i["type"], i["required"]) for i in info["inputs"]])
```

The kind is `"standard"`, `"flow"`, `"apex"` or another custom action type such as
`"emailAlert"` or `"quickAction"`. Each call costs one API call.

## Test code that calls actions

`FakeSalesforce` runs actions with handlers that you register. See
[Testing](../testing.md#fake-actions-and-apex-rest).

## Limitations

- The client does not resend a call that runs an action after a timeout or a 5xx. The action
  could already have run, and a resend would run it twice.
- The client sends a list of inputs in one request and does not split it. Salesforce accepts
  at most 2,000 inputs per request for Apex and Flow actions. With more, Salesforce runs the
  first 2,000 and fails each extra input with `UNKNOWN_EXCEPTION` ("Cannot specify more than
  2000 requests"). So the client raises `ValueError` before it sends a list of more than 2,000
  inputs to an Apex or Flow action. Split the list into calls of at most 2,000. The limit for
  standard actions is not verified, so the client doesn't check it.
- Triggers can't be called. They run when records change, so create, update or delete the
  records instead.
- To run anonymous Apex, wait for the Tooling API area in 0.3.
