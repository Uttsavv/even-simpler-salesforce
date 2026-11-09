# Salesforce setup

Your code logs in through an OAuth app defined in your org. This page shows how to create one
and what each login flow needs from it.

Salesforce's current app type is the External Client App. Classic Connected Apps work
with the same flows, so if you already have one, keep using it. Setup screens change between
releases, so check the [limitations](#limitations) if a step doesn't match what you see.

## Create the app

1. In Setup, open External Client App Manager and create a New External Client App.
2. Give it a name, a contact e-mail and the distribution state Local. Local means only this org
   uses the app.
3. Under API, open Enable OAuth Settings and enable OAuth.
4. Add the OAuth scopes you need. `api` is enough for this library's REST calls. For the web
   server flow, also add `refresh_token`. Setup shows it as "Perform requests at any time".
5. Set a callback URL. Only the web server flow uses it, but the other flows need some valid
   URL, such as `https://localhost/callback`.
6. Enable the login flows you use and save. The sections below list them.
7. Open the app's Settings. Copy the Consumer Key as `client_id` and the Consumer Secret as
   `client_secret`.

OAuth settings can take a few minutes to apply. If you get `invalid_client_id` right after you
create the app, wait a few minutes and try again.

## Client credentials

For [`esf.ClientCredentials`](client-credentials.md):

- Enable the Client Credentials Flow.
- In the app's policies, set the Run As user. Every call runs as this user, so use a dedicated
  integration user with only the permissions the integration needs.
- Use the org's My Domain URL as `domain`. Salesforce doesn't support `login.salesforce.com` or
  `test.salesforce.com` for this flow, and the library rejects them.

Salesforce issues no refresh token for this flow. The client requests a new token when the
current one expires.

## JWT bearer

For [`esf.JwtBearer`](jwt-bearer.md), first create a key pair and a certificate. The private
key stays with your service.

```sh
openssl req -x509 -newkey rsa:2048 -nodes -days 730 \
  -keyout server.key -out server.crt -subj "/CN=acme-integration"
```

Then set up the app:

- Enable the JWT Bearer Flow and upload `server.crt` as the app's certificate.
- Pre-authorize the users the service may act as. In the app's policies, set permitted users to
  "Admin approved users are pre-authorized", then assign the integration user's profile or a
  permission set to the app. Without this, the token request fails with `invalid_grant` /
  `user hasn't approved this consumer`.

`domain` can be `"login"`, `"test"` or the My Domain URL. The JWT `aud` claim is
`https://login.salesforce.com`, or `https://test.salesforce.com` for `"test"` and
`*.sandbox.my.salesforce.com` domains. Override it with `audience=`.

## Web server flow

For [`esf.WebServerFlow`](web-server-flow.md):

- Set the callback URL to your app's redirect endpoint, such as
  `https://app.example.com/oauth/callback`. It must exactly match the `redirect_uri` you pass to
  `WebServerFlow`.
- Keep Require Proof Key for Code Exchange enabled. The library sends an S256 code
  challenge by default.
- Decide whether the web server flow requires the client secret. Public clients, such as desktop
  apps and CLIs, can leave it out and rely on PKCE. Server apps should keep it.
- Set the refresh token policy. It controls how long refresh tokens stay valid, such as "valid
  until revoked" or "expire after N days of inactivity".
- Turn on refresh token rotation. This is recommended.

With rotation on, each refresh returns a new refresh token and invalidates the old one. Reusing
a rotated-out token makes Salesforce revoke the current token too. The library saves the new
refresh token before it uses the new access token, and it never resends a refresh request that
may have reached Salesforce. With several processes, use a durable
[token store](token-stores.md) with compare-and-swap.

## Static token

[`esf.StaticToken`](static-token.md) needs no app settings. The token comes from somewhere
else, such as the Salesforce CLI or another service.

## Session timeout

The access token lives as long as the session timeout of the app, the profile or the org. The
token response says when a token was issued, but not when it expires.

The client's default refresh on `401` doesn't need this value. If you set
[`refresh_ahead`](index.md#proactive-refresh-opt-in), make it a little shorter than the
timeout.

## Check the setup

The [`esf` CLI](../cli.md) is the quickest check:

```sh
export ESF_DOMAIN=https://acme.my.salesforce.com ESF_CLIENT_ID=3MVG9... ESF_CLIENT_SECRET=...
esf whoami
```

If it fails, `OAuthError.error` usually points at the cause:

| `OAuthError.error` | Usual cause |
|---|---|
| `invalid_client_id` | wrong consumer key, or the app isn't active yet |
| `invalid_client` | wrong consumer secret |
| `invalid_grant` | client credentials: no Run As user, or not the My Domain URL; JWT: user not pre-authorized, wrong `aud`, or certificate mismatch; web server: code already used or expired |
| `unsupported_grant_type` | the flow isn't enabled on the app |
| `redirect_uri_mismatch` | `redirect_uri` differs from the callback URL |

## Salesforce references

- [OAuth 2.0 Client Credentials Flow](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_client_credentials_flow.htm&type=5)
- [OAuth 2.0 JWT Bearer Flow](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_jwt_flow.htm&type=5)
- [OAuth 2.0 Web Server Flow](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_web_server_flow.htm&type=5)
- [OAuth 2.0 Refresh Token Flow](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_refresh_token_flow.htm&type=5)
- [Revoke tokens](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_revoke_token.htm&type=5),
  [Token introspection](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oidc_token_introspection_endpoint.htm&type=5),
  [UserInfo endpoint](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_using_userinfo_endpoint.htm&type=5)

## Limitations

The maintainer has not checked the Setup steps on this page click by click against a current
org. Salesforce changes Setup screens and labels between releases, so the menu paths describe
the intent of each step. If something has moved, use the Salesforce Help pages above. Pull
requests with corrections are welcome.

These labels and locations are the least certain:

- the Run As user setting for client credentials;
- the permitted-users and profile assignment settings for JWT bearer;
- the refresh token rotation setting;
- where the session timeout lives in the External Client App UI.
