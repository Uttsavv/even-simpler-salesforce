# ADR 0012: Request headers are keyword arguments, scoped to the resources that document them

- **Status:** accepted, 2026-11-09
- **Implementation (2026-11-13):** partly shipped. The delete fix, the `; ` separator,
  `assignment_rule_id`, `client_name`, `default_namespace`, `update_mru`, `SalesforceWarning`
  and Collections write headers ship. Duplicate rules ship as `allow_save=`, not
  `DuplicateRules` (`big-bang.md` §9.1).
- **Source:** [`big-bang.md`](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md) §2, decision D30 (details in §7.1, §9.1, §9.3, §10.2, §21 V20/V24–V26)
- **Supersedes:** `delete(id, if_unmodified_since=)` and the comma separator for
  `Sforce-Call-Options` in the 0.1 drafts of §9.1 and §10.2.

## Context

Salesforce controls several behaviours through request headers: assignment rules, duplicate
rules, call options, MRU updates, conditional requests and query batch size. It reports others in
response headers: API usage and deprecation warnings. A "typed headers" layer was proposed, with
one class per header passed as a list. Two ways to set the same header break D29, and a generic
list can't tell pyright which headers a method accepts.

The *Headers* chapter of the REST API Developer Guide (v68.0, Winter '27) was checked header by
header. Two problems in the 0.1 code came up:

- *Conditional Request Headers*: **"DELETE requests aren't supported."** `delete(id,
  if_unmodified_since=)` looked like optimistic locking, but Salesforce would delete the record
  anyway.
- *Call Options Header*: the only example separates options with `; `
  (`client=caseSensitiveToken; defaultNamespace=battle`). The 0.1 code joined with `, ` and split
  the caller's header on `,` only.

## Decision

- Each request header is a **keyword argument**:
  - on the method, when it varies per call: `assignment_rule_id`, `duplicate_rules`,
    `if_modified_since`, `if_unmodified_since`, `page_size`;
  - on the client, when it is a property of the integration: `client_name`,
    `default_namespace`, `update_mru`.
- A header is sent only to resources whose Salesforce page documents it, or after a sandbox check
  shows it works there (V24 for Collections).
- `esf.DuplicateRules(allow_save, include_record_details, run_as_current_user)` replaces the
  `allow_save=` kwarg. All three flags default to `False`, as in Salesforce.
- `delete` takes no conditional headers.
- `Sforce-Call-Options` options are joined with `; `. A caller's header is split on `;` and `,`.
- A `Warning` response header becomes `warnings.warn(..., esf.SalesforceWarning)` once per
  distinct text per client family, plus an `sf_warning` field on the `sf.request` event.
- `update_mru` defaults to `None`, so no header is sent and Salesforce's default (`true`)
  applies. The docs recommend `False` for integrations.

**Rejected:**
- Typed header objects.
- ETag `If-Match`/`If-None-Match`: Account rows only, so a parameter that works on one object is
  a trap.
- `x-sfdc-packageversion-*`: niche. `sf.request(headers=)` covers it.

Bulk-only headers are designed with Bulk in 0.3.

## Consequences

- Header support shows up in signatures and docstrings, and each docstring cites its Salesforce
  page.
- Two public symbols are added: `DuplicateRules` and `SalesforceWarning`.
- `include_record_details=True` makes `DuplicateDetected.duplicates` carry the matched records'
  fields.
- Users who relied on `delete(if_unmodified_since=)` get a `TypeError` instead of an unsafe
  no-op. 0.1 is unreleased, so no deprecation is needed.
- Collections write headers depend on V24. If Salesforce ignores them there, the Collections
  guide says so and the kwargs are not added.

## Sources

- REST API Developer Guide v68.0, "Headers" chapter:
  <https://resources.docs.salesforce.com/latest/latest/en-us/sfdc/pdf/api_rest.pdf>. HTML:
  <https://developer.salesforce.com/docs/platform/api-rest/guide/headers.html>. Read 2026-11-09.
  Quoted facts are in `big-bang.md` Appendix A.
- Warning header:
  <https://developer.salesforce.com/docs/platform/api-rest/guide/headers-warning.html>
