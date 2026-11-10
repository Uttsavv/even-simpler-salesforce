# Lead convert and merge

`sf.leads.convert()` turns a lead into an account, a contact and an opportunity. `sf.merge()`
merges duplicate leads, contacts or accounts into one record.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

lead = await sf.query_one("SELECT Id FROM Lead WHERE Email = {}", "ada@example.com")
result = await sf.leads.convert(lead["Id"], converted_status="Closed - Converted")
print(result.account_id, result.contact_id, result.opportunity_id)  # 001... 003... 006...
```

The REST API has no resource for either task, so the client uses the SOAP API for these calls.
It sends your OAuth access token, and you never write any SOAP yourself.

## Convert a lead

Pass the lead id and a converted lead status of your org. Find the valid values under
**Setup > Lead Status**, where **Converted** is checked.

By default Salesforce creates a new account, a new contact and a new opportunity. The result
holds their ids in a `LeadConvertResult`.

## Convert into existing records

Pass `account_id` to attach the lead to an account you already have. Add `contact_id` to merge
the lead into an existing contact of that account. Salesforce fills only the empty fields of
the existing records.

```python
lead = await sf.query_one("SELECT Id FROM Lead WHERE Email = {}", "grace@example.com")
account = await sf.query_one("SELECT Id FROM Account WHERE Name = {}", "Acme")
result = await sf.leads.convert(
    lead["Id"],
    converted_status="Closed - Converted",
    account_id=account["Id"],
    do_not_create_opportunity=True,
)
print(result.opportunity_id)  # None
```

| Option | Default | Description |
|---|---|---|
| `converted_status` | required | a converted lead status of your org |
| `account_id` | `None` | use this account instead of creating one |
| `contact_id` | `None` | merge into this contact; needs `account_id` |
| `opportunity_name` | company name | name of the new opportunity |
| `do_not_create_opportunity` | `False` | don't create an opportunity |
| `owner_id` | lead owner | owner of the new records |
| `send_email` | `False` | email the owner about the new records |
| `overwrite_lead_source` | `False` | replace `LeadSource` on an existing contact |

## Convert many leads

Pass a list of `LeadConvert` objects. The client sends 100 leads per call and returns one
result per lead, in your order. Each lead converts on its own, so one failure doesn't stop the
others.

```python
page = await sf.query("SELECT Id FROM Lead WHERE Status = 'Working' LIMIT 3")
batch = await sf.leads.convert_many(
    [esf.LeadConvert(row["Id"], "Closed - Converted") for row in page.records]
)
print(len(batch.succeeded), len(batch.failed))  # 3 0
batch.raise_for_errors()
```

`LeadConvert` takes the same options as `convert()`. It checks the ids when you create it.

## Merge records

Pass the record to keep and one or two duplicates. Salesforce deletes the duplicates and moves
their related records, such as contacts of a merged account, to the record you keep.

```python
acme = await sf.query_one("SELECT Id FROM Account WHERE Name = {}", "Acme")
dups = await sf.query("SELECT Id FROM Account WHERE Name IN {}", ["Globex", "Initech"])
merged = await sf.merge(acme["Id"], [r["Id"] for r in dups.records], sobject="Account")
print(merged.merged_ids)  # ('001...', '001...')
```

`sobject` is `"Lead"`, `"Contact"` or `"Account"`. To merge more than three records, call
`merge()` again with the same record to keep.

## Errors

A failed conversion or merge raises the [error class](../errors.md#hierarchy) for its status
code, for example `RequiredFieldMissing`. `convert_many()` doesn't raise for one lead. The lead's
result has `ok=False` and its `errors`.

```python
try:
    await sf.merge(acme["Id"], ["001000000000000AAA"], sobject="Account")
except esf.NotFound as exc:
    print(exc.error_code)  # ENTITY_IS_DELETED
```

The client never resends a convert or merge call that may have reached Salesforce. If the token
expired, Salesforce rejects the call before it runs. The client then refreshes the token once
and sends the call again.

## How it works

- The client posts a Partner SOAP envelope to `/services/Soap/u/{api_version}`. The access
  token goes in `SessionHeader`. It never appears in logs or errors.
- The client parses the reply with the standard library. It refuses any reply that declares a
  DTD or entities.
- A SOAP fault maps to the same error classes as REST errors. See
  [SOAP faults](../errors.md#soap-faults).
- Each call counts as one API call in [usage](../usage-and-budgets.md) and logs one
  `sf.request` event with `operation` set to `leads.convert` or `merge`.
