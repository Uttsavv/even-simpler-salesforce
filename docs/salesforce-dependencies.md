<!-- Generated from salesforce-dependencies.yaml by scripts/render_sf_index.py. Do not edit. -->

# Salesforce dependency index

Every Salesforce endpoint, OAuth flow, header, response shape, error code and limit this
library depends on. Review it against each Salesforce release; anything here that changes
or is retired is a potential break. The source of truth is
[`salesforce-dependencies.yaml`](salesforce-dependencies.yaml), which
`tests/unit/test_salesforce_index.py` checks against the code.

**Last full review:** not yet reviewed against a release.
*Checked* is the date an entry was last compared with the official documentation.

## API versions

| Item | Value |
|---|---|
| Default API version (`DEFAULT_API_VERSION`) | `68.0` |
| Oldest supported version (`MIN_API_VERSION`) | `59.0` |
| End-of-life policy | [Salesforce API retirement](https://help.salesforce.com/s/articleView?id=000354473&type=1) |

## REST endpoints

Paths are relative to `/services/data/vXX.X/` unless they start with `/services/`;
`{}` is a value filled in at runtime.

| Path | Methods | Used by | Docs | Checked |
|---|---|---|---|---|
| `query` | GET | sf.query, sf.query_iter, sf.count, sf.query_one, sf.query_first (?q=); sf.explain (?explain=, Salesforce Beta) | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_query.htm) |  |
| `queryAll` | GET | sf.query(..., include_deleted=True) | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_queryall.htm) |  |
| `query/{}` | GET | sf.query_more, sf.query_iter (nextRecordsUrl, absolute path) | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_query.htm) |  |
| `search` | GET | sf.search (?q= SOSL) | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_search.htm) |  |
| `parameterizedSearch` | POST | sf.parameterized_search | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_search_parameterized.htm) |  |
| `limits` | GET | sf.limits | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_limits.htm) |  |
| `sobjects` | GET | sf.describe_global() (If-Modified-Since → 304) | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_describeGlobal.htm) |  |
| `sobjects/{}` | GET | sobject.metadata() | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_sobject_basic_info.htm) |  |
| `sobjects/{}/` | POST | sobject.create() | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_sobject_basic_info.htm) |  |
| `sobjects/{}/describe` | GET | sobject.describe() (If-Modified-Since → 304) | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_sobject_describe.htm) |  |
| `sobjects/{}/{}` | GET, PATCH, DELETE | sobject.get/update/delete() by id | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_sobject_retrieve.htm) |  |
| `sobjects/{}/{}/{}` | GET, PATCH | sobject.get_by_external_id/upsert() (300 on multiple matches) | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_sobject_upsert.htm) |  |
| `sobjects/{}/updated/` | GET | sobject.updated(start, end) | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_getupdated.htm) |  |
| `sobjects/{}/deleted/` | GET | sobject.deleted(start, end) | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_getdeleted.htm) |  |
| `composite/sobjects` | POST, PATCH, DELETE | collections.create/update/delete() | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_sobjects_collections.htm) |  |
| `composite/sobjects/{}` | POST | collections.retrieve() | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_sobjects_collections_retrieve.htm) |  |
| `composite/sobjects/{}/{}` | PATCH | collections.upsert() | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_sobjects_collections_upsert.htm) |  |
| `/services/oauth2/userinfo` | GET | sf.auth.userinfo() | [docs](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_using_userinfo_endpoint.htm&type=5) |  |

## OAuth

Hosts: `login` = https://login.salesforce.com; `test` = https://test.salesforce.com; `my_domain` = https://<name>.my.salesforce.com (required for client credentials).

| Endpoint | Methods | Used by | Docs | Checked |
|---|---|---|---|---|
| `/services/oauth2/authorize` | GET | Web server flow start (code_challenge, S256, state, scope, prompt) | [docs](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_web_server_flow.htm&type=5) |  |
| `/services/oauth2/token` | POST | Every grant type below (form-encoded) |  |  |
| `/services/oauth2/revoke` | POST | sf.auth.revoke() | [docs](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_revoke_token.htm&type=5) |  |
| `/services/oauth2/introspect` | POST | sf.auth.introspect(), expiry="introspect" (reads active, exp) | [docs](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oidc_token_introspection_endpoint.htm&type=5) |  |

| grant_type | Used by | Docs | Checked |
|---|---|---|---|
| `authorization_code` | WebServerFlow.exchange_code (+ PKCE) | [docs](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_web_server_flow.htm&type=5) |  |
| `refresh_token` | WebServerFlow refresh | [docs](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_refresh_token_flow.htm&type=5) |  |
| `client_credentials` | ClientCredentials | [docs](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_client_credentials_flow.htm&type=5) |  |
| `urn:ietf:params:oauth:grant-type:jwt-bearer` | JwtBearer (RS256; aud = login/test/My Domain) | [docs](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_jwt_flow.htm&type=5) |  |

Behaviour relied on:

- Token success body: access_token and instance_url (required), id (identity URL; org and user ids parsed from /id/<org>/<user>), issued_at (ms), signature, scope, optional refresh_token (a new one means rotation; saved before use).
- No expires_in: expiry is learnt from a 401, refresh_ahead, or introspection exp.
- Errors are {error, error_description}. invalid_grant → ReauthorizationRequired; the text "token request is already being processed" → bounded back-off (3 POSTs).
- API calls answer an expired token with 401 and INVALID_SESSION_ID (or OAuth invalid_token): one refresh, one resend, then SessionExpired.

## Headers

| Header | Direction | Used for | Docs | Checked |
|---|---|---|---|---|
| `Sforce-Call-Options` | request | client_name= → client=<name> | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/headers_calloptions.htm) |  |
| `Sforce-Query-Options` | request | page_size= → batchSize=200..2000 | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/headers_queryoptions.htm) |  |
| `Sforce-Auto-Assign` | request | assignment_rule_id= (TRUE / FALSE / rule id) | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/headers_autoassign.htm) |  |
| `Sforce-Duplicate-Rule-Header` | request | allow_save= → allowSave=true/false | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/headers_duplicaterules.htm) |  |
| `If-Modified-Since` | request | describe revalidation, get(if_modified_since=) → 304 | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/intro_rest_conditional_requests.htm) |  |
| `If-Unmodified-Since` | request | update/delete(if_unmodified_since=) → 412 | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/intro_rest_conditional_requests.htm) |  |
| `Content-Encoding` | request | gzip for bodies over 64 KiB (Collections) | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/intro_rest_compression.htm) |  |
| `Sforce-Limit-Info` | response | api-usage=N/M; api-bursts=N/M → sf.usage, budgets | [docs](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/headers_api_usage.htm) |  |
| `Sforce-Request-Id` | response | error.request_id, sf.request events |  |  |
| `Retry-After` | response | 429 / REQUEST_LIMIT_EXCEEDED back-off (seconds or HTTP date) |  |  |
| `Last-Modified` | response | describe cache revalidation |  |  |

## Response shapes

| Resource | Relied on |
|---|---|
| Query / QueryAll / query more | `records`, `totalSize`, `done`, `nextRecordsUrl` |
| Search | `searchRecords` |
| sObject Collections writes | `list, one per input in order`, `id`, `success`, `errors[]`, `created` |
| sObject Collections retrieve | `list, one per id in order`, `null when not found` |
| Upsert by external id | `id`, `created`, `201 = created, 204/200 = updated`, `300 = list of matching URLs` |
| Limits | `{name: {Max, Remaining, ...}}` |
| REST error body | `list of {message, errorCode or statusCode, fields}`, `extras such as duplicateResult` |
| OAuth error body | `error`, `error_description` |

## Error codes

| errorCode | Error class |
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
| `UNABLE_TO_LOCK_ROW` | `RowLocked` |
| `CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY` | `RecordSaveBlocked` |
| `CANNOT_EXECUTE_FLOW_TRIGGER` | `FlowFailed` |
| `STORAGE_LIMIT_EXCEEDED` | `StorageLimitExceeded` |
| `REQUEST_LIMIT_EXCEEDED` | `RequestLimitExceeded` |
| `INVALID_SESSION_ID` | `SessionExpired` |
| `SERVER_UNAVAILABLE` | `ServerError` |
| `APEX_ERROR` | `ApexError` |
| `UNSUPPORTED_API_VERSION` | `UnsupportedApiVersion` |

Also relied on: `ALL_OR_NONE_OPERATION_ROLLED_BACK` (skipped when picking the primary error of an all_or_none batch). HTTP statuses: 300, 304, 401, 403, 404, 409, 410, 412, 414, 420, 428, 429, 431, 500, 502, 503, 504.

## Limits and sizes

| Name | Value |
|---|---|
| `collection_write_chunk` | `200` |
| `collection_read_chunk` | `2000` |
| `query_batch_size` | `[200, 2000]` |
| `record_id_lengths` | `[15, 18]` |
| `gzip_threshold_bytes` | `65536` |
