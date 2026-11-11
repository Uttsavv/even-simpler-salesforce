# UI API

`sf.ui` reads records, object metadata and list views through the Salesforce User Interface
API. Use it when you build a user interface: the UI API applies the user's layouts, field
access and sharing, and returns each value with its display format.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

ada = await sf.query_one("SELECT Id FROM Lead WHERE LastName = {}", "Lovelace")
lead = await sf.ui.record(ada["Id"], fields=["Lead.Name", "Lead.Status"])
print(lead["fields"]["Status"])  # {'displayValue': 'Open', 'value': 'Open'}
```

Every method returns the response body as a `dict`, in the shape the UI API sends. The
examples use the async client. With `esf.Salesforce`, drop `await`.

## Get a record

Pass `fields` to get those fields. Qualify each field name with the object, like
`"Lead.Name"`. A relationship field, like `"Lead.Owner.Name"`, returns the related record
inside `value`.

```python
lead = await sf.ui.record(ada["Id"], fields=["Lead.Name", "Lead.Email"])
print(lead["apiName"], lead["fields"]["Email"]["value"])  # Lead ada@example.com
```

If the user can't read one of the fields, the call fails. Pass `layout_types` instead to get
the fields on the user's page layout. `modes` picks the layout for viewing, editing or
creating a record.

```python
lead = await sf.ui.record(ada["Id"], layout_types=["Compact"], modes=["View"])
print(sorted(lead["fields"]))  # ['Company', 'Email', 'Name', 'Phone', 'Status']
```

| Argument | Values | Description |
|---|---|---|
| `fields` | `["Lead.Name", ...]` | fields to return; the call fails if one is not readable |
| `layout_types` | `"Full"`, `"Compact"` | return the fields on this layout |
| `modes` | `"View"`, `"Edit"`, `"Create"` | the layout mode; Salesforce uses `View` without it |

Pass `fields`, `layout_types` or both. With neither, the call raises `ValueError` before it
sends anything.

## Get object metadata

`object_info()` returns the labels, fields, record types and child relationships of an
object, as the user sees them.

```python
info = await sf.ui.object_info("Lead")
print(info["label"], info["fields"]["Status"]["dataType"])  # Lead Picklist
print(info["defaultRecordTypeId"])  # 012000000000000AAA
```

The client keeps the result in the describe cache, like `sf.sobject(name).describe()`. Pass
`cached=False` to ask Salesforce again. See [Performance](../performance.md) for the cache
settings.

## Get picklist values

`picklist_values()` returns the values of every picklist field for one record type. Use
`defaultRecordTypeId` from `object_info()` if you don't know the record type.

```python
values = await sf.ui.picklist_values("Lead", info["defaultRecordTypeId"])
status = values["picklistFieldValues"]["Status"]
print([v["value"] for v in status["values"]])  # ['Open', 'Working', 'Closed - Not Converted']
```

A dependent picklist has `controllerValues`, and each of its values has `validFor`. Together
they tell you which values each controlling value allows. The describe cache keeps one entry
per object and record type.

## Get default values for a new record

`create_defaults()` returns the default field values and the create layout for a new record.
Show them in a form, then create the record with `sf.sobject(name).create()`.

```python
defaults = await sf.ui.create_defaults("Lead")
print(defaults["record"]["fields"]["Status"]["value"])  # Open
```

Pass `record_type_id` for a record type other than the user's default. `clone_defaults()`
returns the values to copy an existing record. The copy has no id.

```python
clone = await sf.ui.clone_defaults(ada["Id"])
print(clone["record"]["id"], clone["record"]["fields"]["Company"]["value"])  # None Analytical Engines
```

## Read a list view

`list_views()` returns the list views of an object that the user can see. `list_records()`
returns one page of the records in a list view.

```python
views = await sf.ui.list_views("Lead")
print([v["apiName"] for v in views["lists"]])  # ['AllOpenLeads', 'MyUnreadLeads']

page = await sf.ui.list_records("Lead", "AllOpenLeads", page_size=100)
print(page["count"], page["nextPageToken"])  # 100 100
```

To read the next page, pass the `nextPageToken` of the last page as `page_token`. On the last
page, `nextPageToken` is `None`.

```python
names = []
token = None
while True:
    page = await sf.ui.list_records("Lead", "AllOpenLeads", page_size=200, page_token=token)
    names += [r["fields"]["Name"]["value"] for r in page["records"]]
    token = page["nextPageToken"]
    if token is None:
        break
print(len(names))  # 152
```

`page_size` takes 1 to 2,000. Salesforce returns 50 records without it. The list view name
`"__Recent"` returns the records the user viewed most recently.

## Handle errors

UI API errors map to the usual classes. A record the user can't see raises `esf.NotFound`.
An object that doesn't exist raises `esf.InvalidType` from `object_info()`.

```python
try:
    await sf.ui.object_info("Invoice__c")
except esf.InvalidType as exc:
    print(exc.status, exc.error_code)  # 404 NOT_FOUND
```

## Test code that uses the UI API

`FakeSalesforce` builds UI API responses from its records and describes. Add list views with
`fake.set_list_view()`. See [Testing](../testing.md#ui-api).

## How it works

- Each method sends one `GET` to `/services/data/vXX.X/ui-api/...`. All of them are reads, so
  the client resends them after a timeout or a `5xx`.
- `object_info()` and `picklist_values()` use the describe cache. It has the same time to
  live, revalidation and size limit as `describe()`. Entries are kept per org, user and API
  version.
- If many tasks ask for the same object at once, one of them calls Salesforce. The others
  wait and read the cache.
- `record()`, the defaults and the list views are not cached. They change with the data.
- UI API error bodies have the same shape as REST error bodies, so the same classes apply.
  `NOT_FOUND` from `object_info()` means the object doesn't exist, so it is `InvalidType`.

## Limitations

- The client returns the UI API shapes as they are. It doesn't convert them to flat records.
  Use `sf.sobject(name).get()` if you need plain field values.
- The client doesn't send `optionalFields`, `childRelationships` or `formFactor`. Use
  [`sf.request()`](requests.md) for them.
