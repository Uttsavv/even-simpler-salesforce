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

Each HTTP request runs inside `esf.capture()` and the roll-up (`sf_calls`, `sf_ms`, `sf_errors`,
`sf_api_used`, …) is logged on a `request.completed` line.

- [`fastapi_app/`](fastapi_app/app.py): one `AsyncSalesforce` created and closed in the lifespan.
- [`flask_app/`](flask_app/app.py): one sync `Salesforce` per process, closed at exit.

Run (from this directory, with `SF_DOMAIN`, `SF_CLIENT_ID`, `SF_CLIENT_SECRET` set):

```sh
uvicorn fastapi_app.app:app --reload
flask --app flask_app.app:create_app run
```

Tests inject `esf.testing`'s fakes (the `esf_fake` / `esf_async_fake` pytest fixtures) and also
run the real client against `respx`-mocked endpoints:

```sh
pytest examples
```

0.2 will package this wiring as `even_simpler_salesforce.contrib.fastapi` / `.flask`.
