# Sandbox (real-org) tests

Opt-in tests that run the library against a real Salesforce org, plus
`scripts/verify_sandbox.py`, which records evidence for the open §21 verification items.
If `ESF_SANDBOX_*` is not set, every test here is **skipped** with a reason, so
`pytest tests` stays green in CI and on forks. Use a **sandbox or Developer Edition org**,
never production.

## TL;DR (one-time setup, then one command)

```sh
# 1. one-time: create the app (below), then
export ESF_SANDBOX_DOMAIN=https://<mydomain>--<sandbox>.sandbox.my.salesforce.com   # or "test"
export ESF_SANDBOX_CLIENT_ID=<consumer key>
export ESF_SANDBOX_CLIENT_SECRET=<consumer secret>          # omit for a PKCE-only public app
export ESF_SANDBOX_REDIRECT_URI=http://localhost:8765/callback
uv run python scripts/sandbox_login.py                      # browser login; saves tokens locally

# 2. every run
ESF_SANDBOX_ALLOW_WRITES=1 uv run pytest tests/sandbox -m sandbox -v -rs -s \
  && ESF_SANDBOX_ALLOW_WRITES=1 uv run python scripts/verify_sandbox.py
```

## Creating the app (External Client App, rotation on)

In the sandbox: **Setup → App Manager → New External Client App** (or *External Client App
Manager*). A legacy connected app also works if it has the same settings.

1. *Basic info*: any name, e.g. `esf-sandbox`; distribution state **Local**.
2. *API (Enable OAuth Settings)*: turn on.
   - **Callback URL**: `http://localhost:8765/callback` (must equal `ESF_SANDBOX_REDIRECT_URI`).
   - **OAuth scopes**: *Manage user data via APIs (`api`)*, *Perform requests at any time
     (`refresh_token`, `offline_access`)*, *Access unique user identifiers (`openid`)* and
     *Access the identity URL service (`id`, …)*. `openid`/`id` are for `userinfo`.
   - **Require PKCE**: on (the library always sends S256 PKCE).
   - **Require secret for Web Server Flow**: either way. If it's on, set
     `ESF_SANDBOX_CLIENT_SECRET`.
   - **Enable Client Credentials Flow**: optional. Only needed if you want to run in
     client-credentials mode (set a *Run As* user).
3. *Policies → OAuth Policies*:
   - **Refresh Token Policy**: *Refresh token is valid until revoked*, or a long expiry.
   - **Enable Refresh Token Rotation**: **on**. The §19 acceptance test depends on it, and
     Salesforce now enables it by default on new External Client Apps.
   - *Permitted users*: *All users may self-authorize*, or pre-approve your profile.
   - *IP relaxation*: *Relax IP restrictions* if your sandbox has IP ranges.
4. Copy the **Consumer Key** and **Consumer Secret** from *Settings → OAuth Settings →
   Consumer Key and Secret*.

Sources: [Configure the External Client App OAuth Settings](https://help.salesforce.com/s/articleView?language=en_US&id=sf.configure_external_client_app_oauth_settings.htm&type=5),
[Enforce One-Time-Use Refresh Tokens with Refresh Token Rotation](https://help.salesforce.com/s/articleView?id=005316711&language=en_US&type=1),
[Refresh Token Rotation is Enforced on New External Client Apps](https://help.salesforce.com/s/articleView?id=005390912&language=en_US&type=1),
[OAuth 2.0 Refresh Token Flow](https://help.salesforce.com/s/articleView?id=xcloud.remoteaccess_oauth_refresh_token_flow.htm&language=en_US&type=5).
The labels above come from these pages. Check them against your org's UI, which can differ
slightly between releases.

## How login works here

There are two modes. Web-server mode wins if both are configured.

| Mode | Variables | Notes |
|---|---|---|
| **web-server** (needed for the rotation test) | `DOMAIN`, `CLIENT_ID`, optional `CLIENT_SECRET`, optional `REDIRECT_URI`, plus a token from `scripts/sandbox_login.py` **or** `REFRESH_TOKEN` | Tokens live in a SQLite token store (`ESF_SANDBOX_TOKEN_DB`, default `.esf-sandbox/tokens.sqlite3`, mode 0600, gitignored). With rotation on, the store is the only place that knows the *current* refresh token, because the env value is rotated out on first use. A changed `ESF_SANDBOX_REFRESH_TOKEN` is re-seeded automatically. |
| **env** (package plugin rules) | `ACCESS_TOKEN` + `INSTANCE_URL`, or `DOMAIN` + `CLIENT_ID` + `CLIENT_SECRET` (client credentials, My Domain only), or `DOMAIN` + `CLIENT_ID` + `USERNAME` + `PRIVATE_KEY_FILE` (JWT) | Same rules as the package's `esf_sandbox` marker. The rotation test and V3 are skipped. `test_plugin_marker.py` uses the plugin's `esf_sandbox_auth` fixture directly. |

If the stored refresh token was revoked (an `invalid_grant`), the session stops with a clear
message. Run `scripts/sandbox_login.py` again.

## Environment variables (all prefixed `ESF_SANDBOX_`)

| Variable | Default | Meaning |
|---|---|---|
| `DOMAIN` | | `login`, `test`, or `https://<mydomain>….my.salesforce.com` |
| `CLIENT_ID` / `CLIENT_SECRET` | | Consumer key / secret |
| `REDIRECT_URI` | `http://localhost:8765/callback` | Must match the app's callback URL |
| `REFRESH_TOKEN` | | Optional alternative to `sandbox_login.py` |
| `INSTANCE_URL` | | Instance for a bare `ACCESS_TOKEN` (and seed hint) |
| `ACCESS_TOKEN`, `USERNAME`, `PRIVATE_KEY_FILE` | | env-mode logins (see above) |
| `TOKEN_DB` | `.esf-sandbox/tokens.sqlite3` | SQLite token store |
| `TOKEN_KEY` | `esf-sandbox:web_server` | Store key (one record per Salesforce identity) |
| `API_VERSION` | library default | e.g. `66.0` if your sandbox is on an older release |
| `READ_ONLY` | off | `1`: skip every test that writes |
| `ALLOW_WRITES` | off | `1`: run heavy writes (250-row collections tests, V19) |
| `EXTERNAL_ID_FIELD` | | `Lead.Ext__c` or `Contact.Ext__c`: a **writable external-id text field**. Enables the upsert tests and V19 |
| `ROTATION_CYCLES` | `1000` | Total rotations of the shared refresh-token chain in the rotation test |
| `ROTATION_VERIFY_EVERY` | `50` | Each worker calls `GET limits` after every N-th refresh |
| `ROTATION_PAUSE_MS` | `0` | Sleep between a worker's refreshes (use this if the org throttles token requests) |
| `SWEEP` | off | `1`: session cleanup also deletes leftovers of **earlier** runs (`LastName LIKE 'esf-sandbox-%'`) |
| `V5B_VALIDATION_RULE`, `V5B_FLOW_ERROR`, `V5B_APEX_ADD_ERROR`, `V5B_TRIGGER_EXCEPTION` | | verify_sandbox.py V5b (below) |

## What the tests do

Every smoke test runs twice, once on `AsyncSalesforce` and once on `Salesforce`. Every
record a test creates is a Lead (or your external-id object) whose `LastName` starts with the
run tag `esf-sandbox-<12 hex>`. Each test deletes what it created, and at the end of the
session everything carrying the tag is queried and deleted as a safety net.

| File | Writes | What it checks |
|---|---|---|
| `test_read.py` | none | `userinfo` and token ids; `limits()` plus `Sforce-Limit-Info` usage tracking; `describe_global`/`describe` and the cache; **304 revalidation** with `ttl=0` (V14); `query`/`count`/`query_iter`; `query_more` if some object has more than 200 rows; `search`; `INVALID_FIELD` → `InvalidField`, `MALFORMED_QUERY` → `MalformedQuery`, missing record → `NotFound`, unknown object → `InvalidType`/`NotFound` |
| `test_records.py` | 1 Lead per test | create/get/update/delete; `REQUIRED_FIELD_MISSING` → `RequiredFieldMissing`; upsert by external id (create, then update) and `get_by_external_id` (needs `EXTERNAL_ID_FIELD`) |
| `test_collections.py` | 250 Leads per client (`ALLOW_WRITES=1`) | create in 2 chunks; `count`; `query` with `page_size=200` and then `query_more`; `query_iter` order across pages; update; `retrieve` (input order kept); delete; a partial failure with `allOrNone=false`; collections upsert with 250 rows (needs `EXTERNAL_ID_FIELD`) |
| `test_plugin_marker.py` | none | the package's `esf_sandbox` marker and `esf_sandbox_auth` fixture (env mode only) |
| `test_rotation.py` | none (token requests only) | **§19 acceptance**: 4 spawned processes (2 async, 2 sync) share the SQLite CAS store and force refreshes until the chain has rotated `ROTATION_CYCLES` times. Asserts no `ReauthorizationRequired`/`invalid_grant`, no errors, every refresh rotated (so rotation really is on), and that the final stored refresh token still refreshes and works |

`tests/contract/test_rotation_multiprocess.py` is the mocked twin of the rotation test and
runs in normal CI. It uses a fake token endpoint that revokes the chain whenever a
rotated-out token is reused.

**Duration** (rough): read and record tests take about 1 minute. The collections tests add
1–2 minutes. The rotation test makes `ROTATION_CYCLES` + 1 token requests one after
another, about 3–10 minutes for 1,000 depending on latency, plus about 20 `GET limits`
calls per worker. API cost is a few hundred REST calls in total. Token requests are not
REST API calls.

**Risk**: if the rotation test ever fails with `invalid_grant`, Salesforce has revoked the
chain. Log in again with `scripts/sandbox_login.py`. Nothing else in the org is changed.

## Pub/Sub API (0.5)

`test_pubsub.py` runs only with `ESF_SANDBOX_PUBSUB=1` and the `[pubsub]` extra. It needs:

- a platform event `ESF_Sandbox_Event__e` with a text field `Message__c` (or set
  `ESF_SANDBOX_PUBSUB_EVENT=<Event__e>.<Field__c>`);
- Change Data Capture on Account (*Setup → Change Data Capture*).

It publishes tagged events, receives them from `replay="earliest"`, forces a disconnect and
checks that the client resumes without losing an event, then restarts from a replay store. The
CDC test updates a tagged Account and checks the decoded `changed_fields`. Run it with `-s` to
print the schemas and raw bitmaps (evidence for V27 and V28).

## `scripts/verify_sandbox.py` (§21 evidence)

```sh
uv run python scripts/verify_sandbox.py                    # V4, V5b (if configured), V20
uv run python scripts/verify_sandbox.py --allow-writes     # + V19
uv run python scripts/verify_sandbox.py --probe-v3         # + V3 if rotation is OFF
uv run python scripts/verify_sandbox.py --probe-v3 --allow-risky   # V3 with rotation ON (may revoke!)
```

It writes `tests/fixtures/recorded/verification/<YYYY-MM-DD>.json`, which contains field
names, status codes, error codes and Salesforce's error descriptions only. There are no
tokens, no record values, no ids beyond the 3-character prefix and no host names, and the
script refuses to write the file if a known token slipped in. Review the file, commit it, and
link it from §21 or the ADR.

| Item | What it records |
|---|---|
| V4 | `/services/oauth2/introspect` response field names and types for the access token (and refresh token): is `exp` present, and what is `exp - iat`? |
| V5b | `errorCode` and exception class for each configured automation failure (see below). A create that unexpectedly succeeds is deleted again |
| V19 | creates one record with external id `<tag>.json`, then reads it back with the library (`.json` as-is), with `%2E`, and upserts it. Records whether the `.json` suffix breaks the lookup. Deletes what it created |
| V20 | `Sforce-Call-Options: client=esf-verify` on limits, query, describe, search, collections retrieve, `/services/data/` and userinfo: accepted or not. Where admins see it (Event Monitoring) remains a manual check |
| V3 | runs 1 normal refresh to detect rotation, then 8 simultaneous raw refresh POSTs with the same refresh token. Records each error body (`error`, `error_description`), stores a successful response so the chain survives, then checks the chain still works |

### V5b: configuring automations that fail on purpose

Set each variable to `{"sobject": "<Object>", "record": {<fields for a create>}}`. Example
setup on Lead, where each automation fires only for one `Company` value:

| Variable | Automation to create (Setup) | Example value |
|---|---|---|
| `V5B_VALIDATION_RULE` | Validation rule on Lead: error condition `Company = "ESF_VR"` | `{"sobject":"Lead","record":{"LastName":"esf-sandbox-v5b","Company":"ESF_VR"}}` |
| `V5B_FLOW_ERROR` | Record-triggered flow (before save, Lead created) with a Decision `Company = ESF_FLOW` → **Custom Error** element | `…"Company":"ESF_FLOW"…` |
| `V5B_APEX_ADD_ERROR` | Apex trigger (below), `addError` branch | `…"Company":"ESF_ADDERROR"…` |
| `V5B_TRIGGER_EXCEPTION` | Same trigger, unhandled-exception branch | `…"Company":"ESF_THROW"…` |

```apex
trigger EsfSandboxV5b on Lead (before insert) {
    for (Lead l : Trigger.new) {
        if (l.Company == 'ESF_ADDERROR') {
            l.addError('esf-sandbox: addError probe');
        } else if (l.Company == 'ESF_THROW') {
            Integer zero = 0;
            Integer boom = 1 / zero; // unhandled MathException
        }
    }
}
```

Delete these automations when you're done.
