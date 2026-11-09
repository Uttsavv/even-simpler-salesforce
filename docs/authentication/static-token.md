# Static token

Use an access token you got somewhere else, such as the Salesforce CLI, another service or a
test fixture. The client can't refresh it.

```python
import os

import even_simpler_salesforce as esf

auth = esf.StaticToken(os.environ["SF_ACCESS_TOKEN"], os.environ["SF_INSTANCE_URL"])

async with esf.AsyncSalesforce(auth) as sf:
    print((await sf.limits())["DailyApiRequests"].remaining)
```

`StaticToken(access_token, instance_url)` takes both arguments by position. The instance URL is
the org's API host, such as `https://acme.my.salesforce.com`, not the login host. `repr(auth)`
doesn't show the token.

## Handle an expired token

If Salesforce rejects the token with `401`, the call raises `SessionExpired`. If your program
can get a new token, catch the error and create a new client with it.

```python
async with esf.AsyncSalesforce(auth) as sf:
    try:
        await sf.limits()
    except esf.SessionExpired:
        ...  # get a new token and create a new client
```

## Get a token from the Salesforce CLI

`sf org display` prints an access token and instance URL for an org you have logged in to with
the Salesforce CLI. This is handy for local scripts. For services, use a flow that can refresh.

```sh
sf org display --target-org <alias> --json
```

## Revoke and introspect

`sf.auth.revoke()` revokes a static token through the instance's revoke endpoint.

`sf.auth.introspect()` needs an OAuth client, which a static token doesn't have. Pass `auth=`
with the flow that issued the token. The same applies to `sf.auth.revoke(token_key=...)` for
keys other than this client's own.
