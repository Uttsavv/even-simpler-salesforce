---
name: esf-probe
description: >-
  Read-only introspection of a Salesforce org's metadata and data with even-simpler-salesforce:
  the esf CLI (query, describe, request, limits) and sf.tooling in short throwaway scripts. Use it
  to find which Apex triggers, classes, flows, validation rules or workflow rules read or change a
  field, to read an Apex body, to list the automation on an object, or to check a record's
  current values. Triggers: "what changes field X on object Y", "which trigger or flow runs on
  Lead", "show me the Apex for Z", "why does this field get overwritten", "list the validation
  rules on Opportunity", "what fields does this object have", "how many API calls are left".
---

# Probe an org (read-only)

Answer questions about a live org by querying it, not by guessing. You read metadata with the
Tooling API and records with the data API. **Everything in this skill only reads.**

## Safety rules

Follow these rules on every org, also a sandbox:

- No DML. Don't call `create`, `update`, `upsert`, `delete`, Collections, Composite, Bulk ingest,
  lead convert or merge.
- Don't call `sf.tooling.execute_anonymous()`. Anonymous Apex can change data.
- Don't deploy or retrieve with `sf.metadata`, and don't run Apex tests.
- Use `esf request` with `GET` only.
- Never print, log or paste tokens. Don't print `sf.auth`, the `ESF_*` values or
  `esf whoami --json` output into shared places. The CLI never prints tokens; keep it that way.
- Print selected lines, not whole Apex bodies. Bodies can have thousands of lines.

If the user asks for a change, stop and say what you would change. Don't do it from a probe.

## Log in

The `esf` CLI and the probe template read the login from `ESF_*` environment variables. The first
complete set wins:

| Login flow | Variables |
|---|---|
| existing session | `ESF_ACCESS_TOKEN`, `ESF_INSTANCE_URL` |
| JWT bearer | `ESF_DOMAIN`, `ESF_CLIENT_ID`, `ESF_USERNAME`, `ESF_PRIVATE_KEY_FILE` |
| client credentials | `ESF_DOMAIN`, `ESF_CLIENT_ID`, `ESF_CLIENT_SECRET` |

`ESF_API_VERSION` pins the API version. Check the login first. Ask the user to set the variables
if it fails. Don't ask them to paste a token into the chat.

```sh
esf whoami
```

## Data API or Tooling API

Metadata objects live on the Tooling API. Business records live on the data API. If you query an
object on the wrong API, Salesforce answers `INVALID_TYPE`; then try the other one.

| Object | API | CLI |
|---|---|---|
| `ApexTrigger`, `ApexClass` (with `Body`) | Tooling | `esf request GET tooling/query --param "q=..."` |
| `ValidationRule`, `WorkflowRule`, `Flow` | Tooling | same |
| `EntityDefinition`, `FieldDefinition` | Tooling (also the data API) | same |
| `FlowDefinitionView` | data API | `esf query "..."` |
| `Lead`, `Opportunity`, `User`, custom objects | data API | `esf query "..."` |

## Use the CLI for one question

```sh
esf describe Lead                                   # fields, types, required, picklist values
esf query "SELECT Id, OwnerId, Owner.Name FROM Lead WHERE Id = '00Q...'" --json
esf query "SELECT Id, Status FROM Lead WHERE CreatedDate = TODAY" --all --csv
esf request GET tooling/query --param "q=SELECT Name, Status FROM ApexTrigger WHERE TableEnumOrId = 'Lead'"
esf request GET sobjects/Lead/describe/layouts
esf limits                                          # one API call; shows what is left today
```

`esf query` gets the first page; add `--all` for every page. `esf request` returns one page of a
Tooling query. If its JSON has `"done": false`, use a script with `query_iter` (below) instead
of following `nextRecordsUrl` by hand.

## Use a script for more than one query

For a question that needs several queries or a search in Apex bodies, write a throwaway script.
Start from `probe_template.py`, which sits next to this file. If the skill isn't installed as a
directory, this command prints the template's path in the installed library:

```sh
python -c "from importlib.resources import files; print(files('even_simpler_salesforce') / 'skills/esf-probe/probe_template.py')"
```

1. Copy `probe_template.py` to a temporary path outside the project, such as
   `/tmp/esf_probe_<topic>.py`.
2. Edit `OBJECT`, `FIELD` and `main()`.
3. Run it with the `ESF_*` variables set: `python /tmp/esf_probe_<topic>.py`.
4. Delete the script when you have the answer. Never commit it.

The template uses the sync client. `query_iter` follows every page for you, on both APIs:

<!-- esf: skip -->
```python
import even_simpler_salesforce as esf

with esf.Salesforce(esf.StaticToken("<from ESF_ACCESS_TOKEN>", "https://acme.my.salesforce.com")) as sf:
    for t in sf.tooling.query_iter(
        "SELECT Name, Status, Body FROM ApexTrigger WHERE TableEnumOrId = {}", "Opportunity"
    ):
        print(t["Name"], t["Status"], len(t["Body"].splitlines()))
    rows = list(sf.query_iter("SELECT Id, OwnerId FROM Opportunity WHERE Name = {}", "Acme renewal"))
```

Pass values after the query, as in `WHERE TableEnumOrId = {}`. The client quotes them. Never
build SOQL with an f-string.

## Recipe queries

Triggers on an object, with the body (Tooling):

```sql
SELECT Name, Status, Body FROM ApexTrigger WHERE TableEnumOrId = 'Opportunity'
```

One Apex class body (Tooling):

```sql
SELECT Name, Body FROM ApexClass WHERE Name = 'LeadTriggerHandler'
```

Classes by name pattern (Tooling). SOQL can't filter on `Body`, so get the bodies and search them
in the script. The template's `grep_apex()` does this:

```sql
SELECT Name, Body FROM ApexClass WHERE Name LIKE '%Lead%'
```

Active record-triggered flows on an object (data API). Some fields depend on the API version. If
the query fails, drop `TriggerType` and the `TriggerObjectOrEvent` filter:

```sql
SELECT ApiName, Label, ProcessType, TriggerType, TriggerObjectOrEvent.QualifiedApiName, IsActive
FROM FlowDefinitionView
WHERE TriggerObjectOrEvent.QualifiedApiName = 'Lead' AND IsActive = true
```

Validation rules on an object (Tooling):

```sql
SELECT ValidationName, Active, ErrorMessage, ErrorDisplayField
FROM ValidationRule WHERE EntityDefinition.QualifiedApiName = 'Opportunity'
```

Fields on an object (Tooling):

```sql
SELECT QualifiedApiName, DataType, Label
FROM FieldDefinition WHERE EntityDefinition.QualifiedApiName = 'Lead'
```

Workflow rules, the legacy automation (Tooling):

```sql
SELECT Name, TableEnumOrId FROM WorkflowRule WHERE TableEnumOrId = 'Opportunity'
```

## What changes field X on object Y

1. Get the triggers on `Y` with their bodies. Note which handler classes run in
   `before insert`, `before update`, `after insert` and `after update`.
2. Get the bodies of those handler classes, or of `ApexClass WHERE Name LIKE '%<Handler>%'`.
   Search them for the field: `X =`, `.put('X'`, `addError`.
3. List the active flows on `Y` (`FlowDefinitionView`) and the workflow rules (`WorkflowRule`).
   To read one flow's elements, query `Flow` in the Tooling API with `Metadata` for one flow
   version at a time.
4. Check the real record on the data API: the current value of `X` and the fields the logic
   depends on, such as the record type, the owner's profile or the lead source. This tells you
   which branch ran.

Report what you found with the trigger, class and line numbers. Say what you could not confirm.

## Costs

Each page of a query is one API call. `esf describe` and `esf limits` cost one call each. The
client reads the org's usage from every response. In a script, print `sf.usage.snapshot()` at
the end. Keep probes small on production orgs, because every integration shares the daily limit.

## More

- Tooling API guide: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/guides/tooling.md
- CLI: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/cli.md
- Index of all docs pages: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/llms.txt
