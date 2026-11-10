# JWT bearer

Your service signs a short-lived JWT with its private key, and Salesforce issues an access
token for the user named in it. Use it for server-to-server login with a certificate, acting as
a named user.

```python
import os
from pathlib import Path

import even_simpler_salesforce as esf

auth = esf.JwtBearer(
    domain="login",  # or "test", the My Domain URL or an Experience Cloud site URL
    client_id=os.environ["SF_CLIENT_ID"],
    username=os.environ["SF_USERNAME"],  # the user to act as; must be pre-authorized
    private_key=Path("server.key").read_bytes(),  # PEM, str or bytes
)

async with esf.AsyncSalesforce(auth) as sf:
    print(await sf.count("SELECT COUNT() FROM Account"))
```

You need the `[jwt]` extra. Without PyJWT installed, creating `JwtBearer` raises `ImportError`
straight away, not at the first call.

```sh
pip install --pre "even-simpler-salesforce[jwt]"
```

You also need an app with a certificate and pre-authorized users.
[Salesforce setup](salesforce-setup.md#jwt-bearer) shows how.

## Arguments

| Argument | Default | What it does |
|---|---|---|
| `domain` | required | token endpoint host: `"login"`, `"test"`, a My Domain URL or an [Experience Cloud site URL](index.md#experience-cloud-sites) |
| `client_id` | required | consumer key; sent as the JWT `iss` claim |
| `username` | required | Salesforce username; sent as the JWT `sub` claim |
| `private_key` | required | RSA private key in PEM format (RS256); never logged or shown by `repr(auth)` |
| `audience` | inferred from `domain` | the `aud` claim: `"login"`, `"test"` or a URL (see [Audience](#audience)) |
| `token_key` | hash of domain, client id and username | [token store](token-stores.md) key |
| `refresh_ahead` | `None` | [refresh proactively](index.md#proactive-refresh-opt-in) after this `timedelta` |
| `expiry` | `"unknown"` | `"introspect"` reads the expiry from the introspection endpoint |

## Audience

Salesforce checks the `aud` claim against the login host. The client infers it from `domain`:

| `domain` | Inferred `aud` |
|---|---|
| `"login"` or a production My Domain | `https://login.salesforce.com` |
| `"test"`, `https://test.salesforce.com` or `https://<name>--<sandbox>.sandbox.my.salesforce.com` | `https://test.salesforce.com` |
| an Experience Cloud site URL, such as `https://acme.my.site.com/partners` or any `*.my.site.com` host | the site URL itself |

For an Experience Cloud site, the client sends the token request to the site URL. Salesforce
expects the site URL as `aud` for site members:

```python
site = esf.JwtBearer(
    domain="https://acme.my.site.com/partners",
    client_id=os.environ["SF_CLIENT_ID"],
    username="member@acme.example",
    private_key=Path("server.key").read_bytes(),
)
print(site.token_url)  # https://acme.my.site.com/partners/services/oauth2/token
print(site.aud)  # https://acme.my.site.com/partners
```

If the site has a custom domain without a path, the client can't tell it is a site. Set
`audience=` to the site URL. Set `audience=` also for any other case where the guess is wrong:

```python
auth = esf.JwtBearer(
    domain="https://acme--uat.sandbox.my.salesforce.com",
    client_id=os.environ["SF_CLIENT_ID"],
    username="integration@acme.example.uat",
    private_key=Path("server.key").read_text(),
    audience="test",
)
print(auth.aud)  # https://test.salesforce.com
```

## How it works

The client signs a new assertion for every token request. Each assertion is valid for 180
seconds.

There is no refresh token. To refresh, the client signs a new assertion and requests a new
token. It retries those requests after timeouts and 5xx responses.

If Salesforce answers `invalid_grant`, the client raises `OAuthError`. Usual causes:

- the user isn't pre-authorized;
- the audience is wrong;
- the certificate doesn't match the private key.
