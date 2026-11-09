# Client credentials

The app logs in as itself and every call runs as the app's **Run As** user. Use it for backend
services, jobs and integrations that don't act for a particular person.

```python
import os

import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain=os.environ["SF_DOMAIN"],        # must be https://<mydomain>.my.salesforce.com
    client_id=os.environ["SF_CLIENT_ID"],
    client_secret=os.environ["SF_CLIENT_SECRET"],
)

async with esf.AsyncSalesforce(auth) as sf:
    me = await sf.auth.userinfo()
    print(me["preferred_username"])
```

| Argument | Required | Notes |
|---|---|---|
| `domain` | yes | the **My Domain** URL; `"login"` and `"test"` raise `ValueError` |
| `client_id`, `client_secret` | yes | consumer key and secret of the [app](salesforce-setup.md#client-credentials-esfclientcredentials) |
| `token_key` | no | store key; defaults to a hash of domain and client id |
| `refresh_ahead` | no | refresh proactively after this `timedelta` |
| `expiry` | no | `"unknown"` (default) or `"introspect"` |

`repr(auth)` never shows the secret.

## Refresh

Salesforce issues no refresh token for this flow, so a "refresh" requests a new token with the
same credentials. A new-token request has no side effects in the org, so the library retries it
after timeouts, connection errors and token-endpoint 5xx responses (up to
`RetryPolicy.max_attempts`). An `invalid_grant` response means a configuration problem, such as
no Run As user. It raises `OAuthError`, not `ReauthorizationRequired`.

The default in-memory token store is fine for this flow. After a restart the process simply
requests a new token. To share one token across processes and save token requests, pass a
durable [token store](token-stores.md).
