# API reference

This reference is generated from the docstrings. The **public, SemVer-covered surface** is
every name in `even_simpler_salesforce.__all__`, plus the documented submodules
`even_simpler_salesforce.testing` and `even_simpler_salesforce.observability`. Classes shown
here under private module paths (the objects returned by `sf.sobject()`, `sf.collections` and
`sf.auth`) are public through those attributes, not through their import path.

`Salesforce` is generated from `AsyncSalesforce` and has the same methods without `async`, so
only the async classes are shown in full.

The reasons behind the design are written down in the
[architecture decision records](../adr/index.md).

## Clients

::: even_simpler_salesforce.AsyncSalesforce

::: even_simpler_salesforce.Salesforce
    options:
      members: false

::: even_simpler_salesforce._async.api.sobject.AsyncSObject

::: even_simpler_salesforce._async.api.collections.AsyncCollections

::: even_simpler_salesforce._async.api.auth_api.AsyncAuthApi

## Login flows and tokens

::: even_simpler_salesforce.Auth

::: even_simpler_salesforce.ClientCredentials

::: even_simpler_salesforce.JwtBearer

::: even_simpler_salesforce.WebServerFlow

::: even_simpler_salesforce.StaticToken

::: even_simpler_salesforce.AuthStart

::: even_simpler_salesforce.Token

## Token stores

::: even_simpler_salesforce.AsyncTokenStore

::: even_simpler_salesforce.TokenStore

::: even_simpler_salesforce.AsyncInMemoryTokenStore

::: even_simpler_salesforce.InMemoryTokenStore

## Query building

::: even_simpler_salesforce.soql

::: even_simpler_salesforce.soql_like

::: even_simpler_salesforce.soql_identifier

::: even_simpler_salesforce.sosl_term

::: even_simpler_salesforce.Cursor

::: even_simpler_salesforce.flatten

## Results

::: even_simpler_salesforce.QueryResult

::: even_simpler_salesforce.BatchResult

::: even_simpler_salesforce.RecordResult

::: even_simpler_salesforce.UpsertResult

::: even_simpler_salesforce.QueryPlan

::: even_simpler_salesforce.QueryPlanNote

::: even_simpler_salesforce.Limit

::: even_simpler_salesforce.UsageSnapshot

## Configuration

::: even_simpler_salesforce.Timeouts

::: even_simpler_salesforce.RetryPolicy

::: even_simpler_salesforce.Budget

::: even_simpler_salesforce.DescribeCacheConfig

::: even_simpler_salesforce.Cache

::: even_simpler_salesforce.MemoryCache

::: even_simpler_salesforce.call_context

## Observability

::: even_simpler_salesforce.Event

::: even_simpler_salesforce.Observer

::: even_simpler_salesforce.capture

::: even_simpler_salesforce.observability.Summary

::: even_simpler_salesforce.observability.StdlibLogging

::: even_simpler_salesforce.observability.Structlog

::: even_simpler_salesforce.observability.OpenTelemetry

::: even_simpler_salesforce.observability.DebugPrinter

## Errors

::: even_simpler_salesforce.errors
    options:
      show_root_heading: false
      members_order: source

::: even_simpler_salesforce.EsfDeprecationWarning

## Testing

::: even_simpler_salesforce.testing.FakeSalesforce
    options:
      inherited_members: true

::: even_simpler_salesforce.testing.AsyncFakeSalesforce
    options:
      members: false

::: even_simpler_salesforce.testing.FakeSObject

::: even_simpler_salesforce.testing.AsyncFakeSObject

::: even_simpler_salesforce.testing.FakeCollections

::: even_simpler_salesforce.testing.AsyncFakeCollections

::: even_simpler_salesforce.testing.key_prefix

::: even_simpler_salesforce.testing.FakeCall

::: even_simpler_salesforce.testing.FakeRequest

::: even_simpler_salesforce.testing.QueryScript

::: even_simpler_salesforce.testing.UnscriptedCall

::: even_simpler_salesforce.testing.record

::: even_simpler_salesforce.testing.replay

::: even_simpler_salesforce.testing.RecordingTransport

::: even_simpler_salesforce.testing.Redactor

::: even_simpler_salesforce.testing.ReplayMismatch

::: even_simpler_salesforce.testing.TokenStoreContract

::: even_simpler_salesforce.testing.AsyncTokenStoreContract

::: even_simpler_salesforce.testing.plugin
    options:
      show_root_heading: true
      members: false
