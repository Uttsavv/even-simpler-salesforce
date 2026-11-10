# Command-line tool

The package installs an `esf` command for quick checks against an org. It uses the sync client
and only the standard library.

```sh
export ESF_DOMAIN=https://acme.my.salesforce.com
export ESF_CLIENT_ID=3MVG9...
export ESF_CLIENT_SECRET=...
esf whoami
```

`esf whoami` prints the user's name, username, e-mail, user id and org id. It also prints the
instance URL, the API version and the login flow.

## Login

The CLI reads the login from environment variables only, never from flags. This keeps secrets out
of your shell history. The CLI uses the first complete set it finds:

| Login flow | Variables |
|---|---|
| Existing session ([`StaticToken`](authentication/static-token.md)) | `ESF_ACCESS_TOKEN`, `ESF_INSTANCE_URL` |
| [JWT bearer](authentication/jwt-bearer.md) | `ESF_DOMAIN`, `ESF_CLIENT_ID`, `ESF_USERNAME`, `ESF_PRIVATE_KEY_FILE` |
| [Client credentials](authentication/client-credentials.md) | `ESF_DOMAIN`, `ESF_CLIENT_ID`, `ESF_CLIENT_SECRET` |

If a set is only partly configured, the CLI stops with an error that names the missing variables.
The CLI never prints tokens.

To use another API version, set `ESF_API_VERSION`, for example `66.0` or `v66.0`. To see debug
output, set `ESF_DEBUG=1` (see [Logging](logging.md#esf_debug)).

## Run a query

`esf query` prints the first page of results as a table. Add `--all` to get every page.

```sh
esf query "SELECT Id, Name FROM Lead LIMIT 10"
esf query "SELECT Id, Email FROM Lead WHERE Status = 'Open'" --all --csv > open-leads.csv
```

The command flattens each row with [`esf.flatten`](guides/query.md#exporting-rows-esfflatten):

- Parent relationships become dotted columns, such as `Owner.Name`.
- Child subquery rows go into one cell as JSON.

The row count goes to stderr, so stdout stays clean for pipes.

With `--all --json`, the command prints each page as it arrives. CSV and table output wait for the
last page, because the columns depend on every row. If a later page fails, the command exits with
`1` and stdout holds an incomplete JSON array.

| Flag | What it does |
|---|---|
| `--json` | print a JSON array instead of a table |
| `--csv` | print CSV instead of a table |
| `--all` | get every page, not only the first one |
| `--include-deleted` | use the `queryAll` endpoint, which also returns deleted and archived records |
| `--page-size N` | batch size hint for Salesforce, from 200 to 2000 |

## Check limits

`esf limits` prints every org limit in a table: used, max, remaining and the percentage used. Add
`--json` to get JSON.

```sh
esf limits
```

## Describe an sObject

`esf describe` prints the fields of an sObject: type, length, whether a value is required on
create, and the active picklist values. Add `--json` to get JSON.

```sh
esf describe Lead
```

```console
field      type      length  required  values
---------  --------  ------  --------  -------------------------
Id         id        18
LastName   string    80      yes
Email      email     80
Status     picklist  40                (restricted) Open, Working
```

`required` means that Salesforce needs a value when you create a record. The field can't be
empty, and Salesforce doesn't fill in a default.

## Call any REST resource

`esf request` sends one request with [`sf.request`](guides/requests.md), so you get the same
login, refresh, retries and typed errors. The path is relative to `/services/data/vXX.X/`, or
absolute under `/services/`.

```sh
esf request GET sobjects/Lead/describe/layouts
esf request GET query --param "q=SELECT Id FROM Lead LIMIT 5"
esf request PATCH sobjects/Lead/00Q5g00000AbCdE --json '{"Status": "Working"}'
esf request POST /services/apexrest/orders --json @order.json
```

| Flag | What it does |
|---|---|
| `--json JSON` | send JSON as the request body; `@FILE` reads the body from a file |
| `--param KEY=VALUE` | add a query parameter; repeat it for more parameters |

The command prints a JSON response as indented JSON and a text response as text. It writes
binary responses, such as file contents, to stdout unchanged. A write is not sent again if
it times out. See [Retries](errors.md#which-failures-are-retried).

## Generate code

`esf generate` writes Python code from the describe data of your org. Each generated file
starts with a comment that names the org, the API version, the time and the command. To update
the file after a change in the org, run the command again.

### `esf generate models`

`esf generate models` writes one `TypedDict` per sObject. It makes one describe call for each
sObject.

```sh
esf generate models Lead Account --out myapp/sf_models.py
esf generate models Lead --pydantic --out myapp/sf_models.py
```

- Every model has `total=False`, because a query returns only the fields it selects.
- A field that can be empty has the type `X | None`.
- A restricted picklist becomes a `Literal` of its active values. Other picklists are `str`.
- Dates and times are `str`, because the client returns them as text.

Pass a model as `model=` to type the rows. A `TypedDict` only adds types: the rows stay `dict`s.

```python
from typing import Literal, TypedDict

import even_simpler_salesforce as esf


class Lead(TypedDict, total=False):  # from esf generate models Lead
    Id: str
    LastName: str
    Status: Literal["Open", "Working"]


auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
async with esf.AsyncSalesforce(auth) as sf:
    page = await sf.query("SELECT Id, LastName FROM Lead LIMIT 2", model=Lead)
    for lead in page.records:
        print(lead["LastName"])  # your editor knows this key
```

With `--pydantic`, the command writes pydantic models instead. Every field is optional, and the
model checks picklist values when it reads a row. You need `pydantic` installed to import the
file.

| Flag | What it does |
|---|---|
| `--out FILE` | write to `FILE` instead of stdout |
| `--pydantic` | write pydantic models instead of `TypedDict`s |

## Commands

| Command | What it does | API calls |
|---|---|---|
| `esf whoami [--json]` | prints the current user, org and login flow | `GET /services/oauth2/userinfo`, plus the token request |
| `esf limits [--json]` | prints a table of every org limit | one `GET limits` |
| `esf query SOQL` | runs a query and prints the rows | one per page |
| `esf describe SOBJECT [--json]` | prints the fields of an sObject | one `GET sobjects/{o}/describe` |
| `esf request METHOD PATH` | calls any REST resource and prints the response | one, plus retries |
| `esf generate models SOBJECT...` | writes typed models from describe | one describe per sObject |
| `esf version` | prints the library, default API version, httpx and Python versions | none |
| `esf migrate PATH...` | rewrites simple-salesforce code; needs the `[migrate]` extra; see [`esf migrate`](migrate.md) | none, and no login |
| `esf generate actions` | writes typed stubs for invocable actions; see [below](#generate-action-stubs) | one `GET` per action, plus one per kind for a pattern |

## Generate action stubs

`esf generate actions` reads the describe of each action and writes a Python file with one class
per action and one per result. You run the classes with
[`sf.run()`](guides/actions.md#use-typed-stubs).

```sh
esf generate actions --include flow:Assign_Lead --include apex:* --out myapp/sf_actions.py
```

| Option | Description |
|---|---|
| `--include KIND:NAME` | an action to include; repeat it for more; `KIND` is `standard`, `flow` or `apex`; `NAME` may be a pattern such as `*` or `Lead_*` |
| `--out FILE` | the file to write; the command replaces it |
| `--pydantic` | write Pydantic models instead of dataclasses; needs the `[pydantic]` extra |
| `--org NAME` | read the login from `ESF_<NAME>_*` variables instead of `ESF_*`, for example `ESF_SANDBOX_ACCESS_TOKEN` |

The file header names the org, the API version and the time. Field names are snake case, and
each field keeps its API name: in `metadata["api_name"]` for a dataclass, as the alias for a
Pydantic model. Run the command again when an action changes. Don't edit the file by hand.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | Salesforce or API error; the CLI prints the exception class and message to stderr, then the [hint](errors.md#hints-and-docs-links) and the docs link |
| `2` | usage or configuration error: bad arguments, missing variables, an invalid API version, or a file the CLI can't read or write |
| `130` | interrupted with Ctrl-C |

`esf migrate` has its own [exit codes](migrate.md#exit-codes).
