# Command-line tool (`esf`)

Installing the package also installs an `esf` command for quick checks against an org. It
uses only the standard library and the sync client.

```sh
esf whoami [--json]
esf limits [--json]
esf query "SELECT Id, Name FROM Lead LIMIT 10" [--json | --csv] [--all] [--include-deleted] [--page-size N]
esf version
esf migrate src/ [--write | --check] [--report FILE] [--client-names NAME,...] [--rules RULE,...] [--exclude GLOB,...]
```

| Command | What it does | API calls |
|---|---|---|
| `esf whoami` | Prints the user's name, username, e-mail, user and org ids, instance URL, API version and login flow | `GET /services/oauth2/userinfo` (plus the token request) |
| `esf limits` | Prints a table of every org limit: used, max, remaining and percentage used | one `GET limits` |
| `esf query SOQL` | Runs a query and prints a table (default), a JSON array (`--json`) or CSV (`--csv`). Prints the first page only, unless you pass `--all` | one per page |
| `esf version` | Prints the library, default API version, httpx and Python versions | none |
| `esf migrate PATH...` | Rewrites simple-salesforce code and marks what it can't (needs the `[migrate]` extra). See [`esf migrate`](migrate.md) | none (no login) |

`esf query` flattens each row with [`esf.flatten`](guides/query.md#exporting-rows-esfflatten):
parent relationships become dotted columns (`Owner.Name`), and child subquery rows are printed
as JSON in a single cell. It writes the row count to stderr, so stdout stays
clean for pipes:

```sh
esf query "SELECT Id, Email FROM Lead WHERE Status = 'Open'" --all --csv > open-leads.csv
```

`esf generate` is reserved for code generation (`esf generate actions`), which is planned for
0.2. In 0.1 it exits with code 2.

## Login

The CLI reads the login from environment variables only, never from flags, so secrets don't end
up in your shell history. It uses the first complete set it finds:

| Flow | Variables |
|---|---|
| Existing session ([`StaticToken`](authentication/static-token.md)) | `ESF_ACCESS_TOKEN`, `ESF_INSTANCE_URL` |
| [JWT bearer](authentication/jwt-bearer.md) | `ESF_DOMAIN`, `ESF_CLIENT_ID`, `ESF_USERNAME`, `ESF_PRIVATE_KEY_FILE` |
| [Client credentials](authentication/client-credentials.md) | `ESF_DOMAIN`, `ESF_CLIENT_ID`, `ESF_CLIENT_SECRET` |

`ESF_API_VERSION` (e.g. `66.0` or `v66.0`) optionally overrides the default API version.
A partly configured flow is an error that names the missing variables. Tokens are never
printed. `ESF_DEBUG=1` works here too (see [Logging](logging.md#esf_debug)).

```sh
export ESF_DOMAIN=https://acme.my.salesforce.com
export ESF_CLIENT_ID=3MVG9...
export ESF_CLIENT_SECRET=...
esf whoami
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | Salesforce or API error (the exception class and message are printed to stderr) |
| `2` | usage or configuration error (bad arguments, missing variables, invalid API version) |
| `130` | interrupted (Ctrl-C) |

`esf migrate` uses its own codes: `0` nothing left to do, `1` review/TODO items or pending
rewrites remain, `2` error (see [esf migrate](migrate.md#options)).
