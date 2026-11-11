# Examples

Two small apps that use only the public `even_simpler_salesforce` API for query, records and
collections (the §19 acceptance criterion for 0.1). Both expose the same endpoints:

| Endpoint | Salesforce calls |
|---|---|
| `GET /leads?status=Open[&cursor=…]` | `sf.query(esf.soql(...), page_size=200)` / `sf.query_more(cursor)` |
| `GET /leads/{id}` | `sf.sobject("Lead").get(id, fields=[...])` |
| `POST /leads` | `sf.sobject("Lead").create(data)` |
| `PATCH /leads/{id}` | `sf.sobject("Lead").update(id, changes)` |
| `POST /leads/bulk-status` | `sf.collections.update("Lead", rows)` (partial failures reported per id) |
| `GET /usage` | `sf.usage.snapshot()` (no API call) |

The wiring comes from the packaged integrations. Each HTTP request's Salesforce roll-up
(`sf_calls`, `sf_ms`, `sf_errors`, `sf_api_used`, …) is logged on a `request.completed` line,
and typed errors become JSON responses (404, 422, 400 for a bad cursor, 503 with `Retry-After`).

- [`fastapi_app/`](fastapi_app/app.py): `even_simpler_salesforce.contrib.fastapi`, one
  `AsyncSalesforce` created and closed in the lifespan.
- [`flask_app/`](flask_app/app.py): `even_simpler_salesforce.contrib.flask`, one sync `Salesforce`
  per process, closed at exit.

Run (from this directory, with `SF_DOMAIN`, `SF_CLIENT_ID`, `SF_CLIENT_SECRET` and `SF_CURSOR_KEY` set. `SF_CURSOR_KEY` signs [cursors](../docs/guides/query.md#cursors): generate it once with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`, keep it secret, and use the same value on every worker):

```sh
uvicorn fastapi_app.app:app --reload
flask --app flask_app.app:create_app run
```

Tests inject `esf.testing`'s fakes (the `esf_fake` / `esf_async_fake` pytest fixtures) and also
run the real client against `respx`-mocked endpoints:

```sh
pytest examples
```
