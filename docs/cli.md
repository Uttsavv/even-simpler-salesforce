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

## Commands

| Command | What it does | API calls |
|---|---|---|
| `esf whoami [--json]` | prints the current user, org and login flow | `GET /services/oauth2/userinfo`, plus the token request |
| `esf limits [--json]` | prints a table of every org limit | one `GET limits` |
| `esf query SOQL` | runs a query and prints the rows | one per page |
| `esf version` | prints the library, default API version, httpx and Python versions | none |
| `esf migrate PATH...` | rewrites simple-salesforce code; needs the `[migrate]` extra; see [`esf migrate`](migrate.md) | none, and no login |

`esf generate` is reserved for code generation (`esf generate actions`), which is planned for 0.2.
In 0.1 it exits with code 2.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | Salesforce or API error; the CLI prints the exception class and message to stderr |
| `2` | usage or configuration error: bad arguments, missing variables or an invalid API version |
| `130` | interrupted with Ctrl-C |

`esf migrate` has its own [exit codes](migrate.md#exit-codes).
