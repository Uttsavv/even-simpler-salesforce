# Salesforce setup (External Client Apps)

Your code logs in through an OAuth app defined in the org. Salesforce's current app type is the
**External Client App** (ECA). Classic **Connected Apps** work with the same flows, so if you
already have one, keep using it. This page lists what each login flow needs from the app.

!!! warning "Salesforce UI steps are not verified by the maintainer"
    Salesforce changes Setup screens and labels between releases. The menu paths and setting
    names below describe the intent of each step. They have **not** been checked click by click
    against a current org. If something has moved, use the linked Salesforce Help pages. Pull
    requests with corrections are welcome.

Official references:

- [OAuth 2.0 Client Credentials Flow](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_client_credentials_flow.htm&type=5)
- [OAuth 2.0 JWT Bearer Flow](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_jwt_flow.htm&type=5)
- [OAuth 2.0 Web Server Flow](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_web_server_flow.htm&type=5)
- [OAuth 2.0 Refresh Token Flow](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_refresh_token_flow.htm&type=5)
- [Revoke tokens](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_revoke_token.htm&type=5),
  [Token introspection](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oidc_token_introspection_endpoint.htm&type=5),
  [UserInfo endpoint](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_using_userinfo_endpoint.htm&type=5)

## 1. Create the app *(UI steps unverified)*

1. In **Setup**, open **External Client App Manager** and create a **New External Client App**.
2. Give it a name, a contact e-mail and the distribution state **Local** (the app is used only
   in this org).
3. Under **API (Enable OAuth Settings)**, enable OAuth.
4. Add the **OAuth scopes** you need. `api` is enough for this library's REST calls. Add
   `refresh_token` (shown as "Perform requests at any time") for the web server flow.
5. Set a **callback URL**. Only the web server flow uses it. The other flows need some valid
   URL, such as `https://localhost/callback`.
6. Enable the flows you will use (see the sections below), save, and open the app's **Settings**
   to copy the **Consumer Key** (`client_id`) and **Consumer Secret** (`client_secret`).

Changes to OAuth settings can take a few minutes to apply. An `invalid_client_id` error right
after you create the app usually goes away after that.

## 2. Settings per flow

### Client credentials (`esf.ClientCredentials`)

- Enable the **Client Credentials Flow**.
- In the app's **policies**, set the **Run As** user *(UI label unverified)*. Every call runs
  as this user, so give it a dedicated integration user with only the permissions the
  integration needs.
- Use the org's **My Domain** URL as `domain`. Salesforce doesn't support `login.salesforce.com`
  or `test.salesforce.com` for this flow, and the library rejects them.
- Salesforce issues no refresh token for this flow. The client requests a new token when the
  current one expires.

### JWT bearer (`esf.JwtBearer`)

- Create a key pair and a certificate (the private key stays with your service):

  ```sh
  openssl req -x509 -newkey rsa:2048 -nodes -days 730 \
    -keyout server.key -out server.crt -subj "/CN=acme-integration"
  ```

- Enable the **JWT Bearer Flow** and **upload `server.crt`** as the app's certificate.
- Pre-authorize the users the service may act as. In the app's policies, set permitted users to
  *Admin approved users are pre-authorized*, then assign the integration user's profile or a
  permission set to the app *(UI labels unverified)*. Without this step the token request fails
  with `invalid_grant` / `user hasn't approved this consumer`.
- `domain` can be `"login"`, `"test"` or the My Domain URL. The JWT `aud` claim is
  `https://login.salesforce.com`, or `https://test.salesforce.com` for `"test"` and
  `*.sandbox.my.salesforce.com` domains. Override it with `audience=`.

### Web server flow with PKCE (`esf.WebServerFlow`)

- Set the **callback URL** to your app's redirect endpoint, for example
  `https://app.example.com/oauth/callback`. It must exactly match the `redirect_uri` you pass
  to `WebServerFlow`.
- Keep **Require Proof Key for Code Exchange (PKCE)** enabled. The library sends an S256 code
  challenge by default.
- Decide whether the client secret is required for the web server flow. Public clients
  (desktop, CLI) can leave the secret out and rely on PKCE. Server apps should keep it.
- **Refresh token policy.** Set how long refresh tokens stay valid, for example *valid until
  revoked* or *expire after N days of inactivity*.
- **Refresh token rotation** *(recommended; UI label unverified)*. When it is on, each refresh
  returns a new refresh token and invalidates the old one. Reusing a rotated-out token makes
  Salesforce revoke the current token too. The library handles this: it saves the new refresh
  token before using the new access token, and it never resends a refresh request that may have
  reached Salesforce. With several processes, use a durable
  [token store](token-stores.md) with compare-and-swap.

### Static token (`esf.StaticToken`)

No app settings are needed. The token comes from somewhere else (the Salesforce CLI, another
service). See [Static token](static-token.md).

## 3. Session timeout

The token response tells the client when a token was issued but not when it expires. The
access-token lifetime is the session timeout of the app (or of the profile or org) *(location of
the setting in the ECA UI unverified)*. The default 401-driven refresh doesn't need this value.
If you set [`refresh_ahead`](index.md#proactive-refresh-opt-in), make it a little shorter than
the timeout.

## 4. Check the setup

The [`esf` CLI](../cli.md) is the quickest check:

```sh
export ESF_DOMAIN=https://acme.my.salesforce.com ESF_CLIENT_ID=3MVG9... ESF_CLIENT_SECRET=...
esf whoami
```

Common OAuth errors and what they usually mean:

| `OAuthError.error` | Usual cause |
|---|---|
| `invalid_client_id` | wrong consumer key, or the app isn't active yet |
| `invalid_client` | wrong consumer secret |
| `invalid_grant` | client credentials: no Run As user, or not the My Domain URL; JWT: user not pre-authorized, wrong `aud`, or certificate mismatch; web server: code already used or expired |
| `unsupported_grant_type` | the flow isn't enabled on the app |
| `redirect_uri_mismatch` | `redirect_uri` differs from the callback URL |
