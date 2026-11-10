# Client credentials

The app logs in as itself, and every call runs as the app's Run As user. Use it for backend
services, jobs and integrations that don't act for a particular person.

```python
import os

import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain=os.environ["SF_DOMAIN"],  # https://<mydomain>.my.salesforce.com
    client_id=os.environ["SF_CLIENT_ID"],
    client_secret=os.environ["SF_CLIENT_SECRET"],
)

async with esf.AsyncSalesforce(auth) as sf:
    me = await sf.auth.userinfo()
    print(me["preferred_username"])
```

You need an app with the Client Credentials Flow and a Run As user.
[Salesforce setup](salesforce-setup.md#client-credentials) shows how.

## Arguments

| Argument | Default | What it does |
|---|---|---|
| `domain` | required | the My Domain URL; `"login"`, `"test"` and a URL with a path raise `ValueError` |
| `client_id` | required | consumer key of the app |
| `client_secret` | required | consumer secret of the app; `repr(auth)` never shows it |
| `token_key` | hash of domain and client id | [token store](token-stores.md) key |
| `refresh_ahead` | `None` | [refresh proactively](index.md#proactive-refresh-opt-in) after this `timedelta` |
| `expiry` | `"unknown"` | `"introspect"` reads the expiry from the introspection endpoint |

## Share the token between processes

The default in-memory token store is fine for this flow. After a restart, the process requests
a new token. To share one token across processes and save token requests, pass a durable
[token store](token-stores.md):

```python
store = esf.AsyncInMemoryTokenStore()  # use a durable store here
async with esf.AsyncSalesforce(auth, store=store) as sf:
    await sf.limits()
```

## How it works

Salesforce issues no refresh token for this flow. A refresh requests a new token with the same
credentials.

A token request has no side effects in the org, so the client retries it. It retries after
timeouts, connection errors and 5xx responses from the token endpoint, up to
`RetryPolicy.max_attempts` times.

If Salesforce answers `invalid_grant`, the app has a configuration problem, such as no Run As
user. The client raises `OAuthError`, not `ReauthorizationRequired`.
