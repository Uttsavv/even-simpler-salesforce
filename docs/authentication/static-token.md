# Static token

Use an access token you got somewhere else, for example from the Salesforce CLI, another
service or a test fixture:

```python
import os

import even_simpler_salesforce as esf

auth = esf.StaticToken(os.environ["SF_ACCESS_TOKEN"], os.environ["SF_INSTANCE_URL"])

with esf.Salesforce(auth) as sf:
    print(sf.limits()["DailyApiRequests"].remaining)
```

The library can't refresh a static token. When Salesforce rejects it (`401`), the call raises
`SessionExpired`. Catch it and build a new client with a new token if your program can get
one.

- `StaticToken(access_token, instance_url)` takes both arguments positionally. The instance URL
  is the org's API host (`https://acme.my.salesforce.com`), not the login host.
- `repr(auth)` doesn't show the token.
- `sf.auth.revoke()` revokes a static token through the instance's revoke endpoint.
- `sf.auth.introspect()` and `sf.auth.revoke(token_key=...)` for *other* keys need the flow
  that issued that token, because a static token has no OAuth client. Pass it as `auth=`.

!!! tip "Getting a token from the Salesforce CLI"
    `sf org display --target-org <alias> --json` prints an access token and instance URL for an
    org you have logged in to with the Salesforce CLI. This is handy for local scripts. For
    services, use a flow that can refresh.
