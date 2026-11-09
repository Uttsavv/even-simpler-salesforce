# Salesforce dependency index

Every Salesforce endpoint, OAuth flow, header, response shape, error code and limit that this
library depends on, in one place. When Salesforce publishes a release (three a year), go
through this page against the release notes and the API version changes. Anything listed
here that changes or is retired is a potential break.

`tests/unit/test_salesforce_index.py` keeps the page honest: it fails when the code uses
an endpoint, OAuth endpoint, `Sforce-*` request header or mapped error code that isn't listed
below. Keep the backticked keys in the first column exactly as written; the test reads them.

**Releases to compare against:** [Salesforce release notes](https://help.salesforce.com/s/articleView?id=release-notes.salesforce_release_notes.htm&type=5)
and the [REST API release notes](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/intro_rest_whats_new.htm).

**Last full review:** not yet reviewed against a release. "Docs" links were added from the
documented URL layout and still need a check by hand; tick the row's *Checked* column when
you've opened the link and compared the behaviour.

## API versions

| Item | Value | Where | Checked |
|---|---|---|---|
| Default API version | `68.0` | `DEFAULT_API_VERSION` in `_async/client.py` | |
| Oldest supported version | `59.0` | `MIN_API_VERSION` in `_async/client.py` | |
| Version format accepted | `NN.N` (2-3 digit major) | `_check_version` | |

Salesforce retires old REST API versions on a published schedule
([API end-of-life policy](https://help.salesforce.com/s/articleView?id=000354473&type=1)).
Bump `MIN_API_VERSION` when 59.0 is retired, and `DEFAULT_API_VERSION` each library release.

## REST endpoints

Paths are relative to `/services/data/vXX.X/` unless they start with `/services/`.
`{}` marks a value filled in at runtime.

| Key | Method | Library operation | Docs | Checked |
|---|---|---|---|---|
| `query` | GET | `sf.query`, `sf.query_iter`, `sf.count` (`?q=`) | [Query](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_query.htm) | |
| `queryAll` | GET | `query(..., include_deleted=True)` | [QueryAll](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_queryall.htm) | |
| `query/{}` | GET | `sf.query_more`, `sf.query_iter` (`nextRecordsUrl`, absolute path) | [Query](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_query.htm) | |
| `search` | GET | `sf.search` (`?q=` SOSL) | [Search](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_search.htm) | |
| `parameterizedSearch` | POST | `sf.parameterized_search` | [Parameterized Search](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_search_parameterized.htm) | |
| `limits` | GET | `sf.limits` | [Limits](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_limits.htm) | |
| `sobjects` | GET | `sf.describe_global()` (`If-Modified-Since` → 304) | [Describe Global](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_describeGlobal.htm) | |
| `sobjects/{}` | GET | `sobject.metadata()` | [sObject Basic Information](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_sobject_basic_info.htm) | |
| `sobjects/{}/` | POST | `sobject.create()` | [sObject Basic Information](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_sobject_basic_info.htm) | |
| `sobjects/{}/describe` | GET | `sobject.describe()` (`If-Modified-Since` → 304) | [sObject Describe](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_sobject_describe.htm) | |
| `sobjects/{}/{}` | GET, PATCH, DELETE | `sobject.get/update/delete()` (by id) | [sObject Rows](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_sobject_retrieve.htm) | |
| `sobjects/{}/{}/{}` | GET, PATCH | `sobject.get_by_external_id/upsert()` (300 on multiple matches) | [sObject Rows by External ID](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_sobject_upsert.htm) | |
| `sobjects/{}/updated/` | GET | `sobject.updated(start, end)` | [Get Updated](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_getupdated.htm) | |
| `sobjects/{}/deleted/` | GET | `sobject.deleted(start, end)` | [Get Deleted](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_getdeleted.htm) | |
| `composite/sobjects` | POST, PATCH, DELETE | `collections.create/update/delete()` | [sObject Collections](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_sobjects_collections.htm) | |
| `composite/sobjects/{}` | POST | `collections.retrieve()` | [sObject Collections Retrieve](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_sobjects_collections_retrieve.htm) | |
| `composite/sobjects/{}/{}` | PATCH | `collections.upsert()` | [sObject Collections Upsert](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_sobjects_collections_upsert.htm) | |
| `/services/oauth2/userinfo` | GET | `sf.auth.userinfo()` | [UserInfo endpoint](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_using_userinfo_endpoint.htm&type=5) | |

`sf.request(method, path)` can call any other path under `/services/`; those calls are the
caller's dependency, not the library's.

## OAuth endpoints and flows

Base URL: `https://login.salesforce.com`, `https://test.salesforce.com` (`domain="login"` /
`"test"`) or the org's My Domain (`https://<name>.my.salesforce.com`, required for client
credentials).

| Key | Method | Used for | Docs | Checked |
|---|---|---|---|---|
| `/services/oauth2/authorize` | GET (browser) | Web server flow start (`code_challenge`, `S256`, `state`, `scope`, `prompt`) | [Web server flow](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_web_server_flow.htm&type=5) | |
| `/services/oauth2/token` | POST form | Every grant below | see each flow | |
| `/services/oauth2/revoke` | POST form | `sf.auth.revoke()` | [Revoke tokens](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_revoke_token.htm&type=5) | |
| `/services/oauth2/introspect` | POST form | `sf.auth.introspect()`, `expiry="introspect"` (`active`, `exp`) | [Token introspection](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oidc_token_introspection_endpoint.htm&type=5) | |

| Flow (`grant_type`) | Library class | Docs | Checked |
|---|---|---|---|
| `authorization_code` (+ PKCE) | `WebServerFlow.exchange_code` | [Web server flow](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_web_server_flow.htm&type=5) | |
| `refresh_token` | `WebServerFlow` refresh | [Refresh token flow](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_refresh_token_flow.htm&type=5) | |
| `client_credentials` | `ClientCredentials` | [Client credentials flow](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_client_credentials_flow.htm&type=5) | |
| `urn:ietf:params:oauth:grant-type:jwt-bearer` | `JwtBearer` (RS256, `aud` = login/test/My Domain) | [JWT bearer flow](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_jwt_flow.htm&type=5) | |

Token-endpoint behaviour we rely on:

- Success body fields: `access_token`, `instance_url` (required), `id` (identity URL; org and
  user ids are parsed from `/id/<org>/<user>`), `issued_at` (ms), `signature`, `scope`,
  optional `refresh_token` (a new one means rotation; it's saved before use).
- No `expires_in`: expiry is learnt from a 401, `refresh_ahead`, or introspection `exp`.
- Errors are `{"error", "error_description"}`. `invalid_grant` → `ReauthorizationRequired`;
  the text "token request is already being processed" → bounded back-off (3 POSTs).
- API calls answer an expired token with 401 and `INVALID_SESSION_ID` (or OAuth
  `invalid_token`): one refresh, one resend, then `SessionExpired`.

## Headers

| Key | Direction | Used for | Docs | Checked |
|---|---|---|---|---|
| `Sforce-Call-Options` | request | `client_name=` → `client=<name>` | [Call Options header](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/headers_calloptions.htm) | |
| `Sforce-Query-Options` | request | `page_size=` → `batchSize=200..2000` | [Query Options header](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/headers_queryoptions.htm) | |
| `Sforce-Auto-Assign` | request | `assignment_rule_id=` (`TRUE`/`FALSE`/rule id) | [Assignment Rule header](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/headers_autoassign.htm) | |
| `Sforce-Duplicate-Rule-Header` | request | `allow_save=` → `allowSave=true/false` | [Duplicate Rule header](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/headers_duplicaterules.htm) | |
| `If-Modified-Since` | request | describe revalidation, `get(if_modified_since=)` → 304 | [Conditional requests](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/intro_rest_conditional_requests.htm) | |
| `If-Unmodified-Since` | request | `update/delete(if_unmodified_since=)` → 412 | [Conditional requests](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/intro_rest_conditional_requests.htm) | |
| `Content-Encoding: gzip` | request | bodies > 64 KiB (Collections) | [Compression](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/intro_rest_compression.htm) | |
| `Sforce-Limit-Info` | response | `api-usage=N/M; api-bursts=N/M` → `sf.usage`, budgets | [Limit Info header](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/headers_api_usage.htm) | |
| `Sforce-Request-Id` | response | `error.request_id`, `sf.request` events | | |
| `Retry-After` | response | 429 / `REQUEST_LIMIT_EXCEEDED` back-off (seconds or HTTP date) | | |
| `Last-Modified` | response | describe cache revalidation | | |

## Response shapes

| Resource | Fields read | Checked |
|---|---|---|
| Query / QueryAll / query more | `records`, `totalSize`, `done`, `nextRecordsUrl` | |
| Search | `searchRecords` | |
| sObject Collections writes | list, one per input in order: `id`, `success`, `errors[]`, `created` | |
| sObject Collections retrieve | list, one per id in order, `null` when not found | |
| Upsert by external id | `id`, `created`; 201 = created, 204/200 = updated; 300 = list of matching URLs | |
| Limits | `{name: {"Max", "Remaining", ...}}` | |
| REST error body | list of `{"message", "errorCode" or "statusCode", "fields"}` (plus extras such as `duplicateResult`) | |
| OAuth error body | `{"error", "error_description"}` | |

## Error codes

Mapped to typed errors in `core/classify.py` (`CODE_MAP`). A code Salesforce adds or renames
falls back to the HTTP status mapping.

| Key | Error class |
|---|---|
| `MALFORMED_QUERY` | `MalformedQuery` |
| `INVALID_QUERY_LOCATOR` | `MalformedQuery` |
| `INVALID_QUERY_FILTER_OPERATOR` | `MalformedQuery` |
| `MALFORMED_SEARCH` | `MalformedQuery` |
| `INVALID_SEARCH` | `MalformedQuery` |
| `INVALID_FIELD` | `InvalidField` |
| `INVALID_FIELD_FOR_INSERT_UPDATE` | `InvalidField` |
| `INVALID_FIELD_WHEN_USING_TEMPLATE` | `InvalidField` |
| `INVALID_TYPE` | `InvalidType` |
| `NOT_FOUND` | `NotFound` |
| `ENTITY_IS_DELETED` | `NotFound` |
| `INSUFFICIENT_ACCESS` | `Forbidden` |
| `INSUFFICIENT_ACCESS_OR_READONLY` | `Forbidden` |
| `INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY` | `Forbidden` |
| `API_DISABLED_FOR_ORG` | `Forbidden` |
| `API_CURRENTLY_DISABLED` | `Forbidden` |
| `FUNCTIONALITY_NOT_ENABLED` | `Forbidden` |
| `FIELD_CUSTOM_VALIDATION_EXCEPTION` | `ValidationRuleFailed` |
| `REQUIRED_FIELD_MISSING` | `RequiredFieldMissing` |
| `DUPLICATES_DETECTED` | `DuplicateDetected` |
| `INVALID_CROSS_REFERENCE_KEY` | `InvalidReference` |
| `FIELD_INTEGRITY_EXCEPTION` | `InvalidReference` |
| `STRING_TOO_LONG` | `StringTooLong` |
| `INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST` | `InvalidPicklistValue` |
| `UNABLE_TO_LOCK_ROW` | `RowLocked` (retryable) |
| `CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY` | `RecordSaveBlocked` |
| `CANNOT_EXECUTE_FLOW_TRIGGER` | `FlowFailed` |
| `STORAGE_LIMIT_EXCEEDED` | `StorageLimitExceeded` |
| `REQUEST_LIMIT_EXCEEDED` | `RequestLimitExceeded` |
| `INVALID_SESSION_ID` | `SessionExpired` (triggers one refresh) |
| `SERVER_UNAVAILABLE` | `ServerError` (retryable) |
| `APEX_ERROR` | `ApexError` |

Also relied on: `ALL_OR_NONE_OPERATION_ROLLED_BACK` (skipped when picking the primary error of
an `all_or_none` batch) and HTTP 300, 304, 401, 403, 404, 409, 410, 412, 414, 420, 428, 429,
431 and 5xx (see `_STATUS_MAP`).

## Limits and sizes

| Item | Value | Where | Checked |
|---|---|---|---|
| Collections write chunk | 200 records | `COLLECTION_WRITE_CHUNK` | |
| Collections retrieve chunk | 2,000 ids | `COLLECTION_READ_CHUNK` | |
| Query batch size | 200-2,000 | `Sforce-Query-Options` validation | |
| Record id | 15 or 18 alphanumerics | `check_record_id` | |
| Gzip threshold | 64 KiB | `GZIP_THRESHOLD` | |

## Changing this page

Add a row in the same change that makes the library depend on something new (endpoint,
header, error code, flow, limit). When you check a row against the official documentation,
put the date in *Checked*.
