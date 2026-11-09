# JWT bearer

Server-to-server login with a certificate. The app signs a short-lived JWT with its private
key, and Salesforce issues an access token for the user named in the JWT. You need the
`[jwt]` extra:

```sh
pip install "even-simpler-salesforce[jwt]"
```

```python
import os
from pathlib import Path

import even_simpler_salesforce as esf

auth = esf.JwtBearer(
    domain="login",  # or "test", or the My Domain URL
    client_id=os.environ["SF_CLIENT_ID"],
    username=os.environ["SF_USERNAME"],  # the user to act as (pre-authorized)
    private_key=Path("server.key").read_bytes(),  # PEM, str or bytes
)

async with esf.AsyncSalesforce(auth) as sf:
    print(await sf.count("SELECT COUNT() FROM Account"))
```

| Argument | Required | Notes |
|---|---|---|
| `domain` | yes | token endpoint host: `"login"`, `"test"` or a My Domain URL |
| `client_id` | yes | consumer key; it is the JWT `iss` claim |
| `username` | yes | Salesforce username; it is the JWT `sub` claim |
| `private_key` | yes | RSA private key in PEM format (RS256) |
| `audience` | no | the `aud` claim: `"login"`, `"test"` or a URL. When unset, it is inferred from `domain` (see below) |
| `token_key`, `refresh_ahead`, `expiry` | no | as for the other flows |

The assertion is valid for 180 seconds and is signed again for every token request. The
private key is never logged and `repr(auth)` doesn't show it. If PyJWT isn't installed,
creating `JwtBearer` raises `ImportError` straight away, not at the first call.

## Audience

Salesforce checks the `aud` claim against the login host:

| `domain` | inferred `aud` |
|---|---|
| `"login"` or a production My Domain | `https://login.salesforce.com` |
| `"test"` or `https://<name>--<sandbox>.sandbox.my.salesforce.com` | `https://test.salesforce.com` |

Set `audience=` when this guess is wrong, for example for an Experience Cloud site URL:

```python
auth = esf.JwtBearer(
    domain="https://acme--uat.sandbox.my.salesforce.com",
    client_id=os.environ["SF_CLIENT_ID"],
    username="integration@acme.example.uat",
    private_key=Path("server.key").read_text(),
    audience="test",
)
print(auth.aud)
```

## Refresh

As with client credentials, there is no refresh token. A "refresh" signs a new assertion and
requests a new token, and those requests are retried after timeouts and 5xx responses.
`invalid_grant` (user not pre-authorized, wrong audience, certificate mismatch) raises
`OAuthError`.
