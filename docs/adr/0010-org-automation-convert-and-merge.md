# ADR 0010: Org automation, lead convert and merge

- **Status:** accepted, 2026-11-09
- **Source:** [`big-bang.md`](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md) §2, decisions D14, D26 (details in §9.7, §9.8)

## Context

Salesforce has no REST resource for lead convert or merge, anywhere on the platform. Org logic
lives in invocable actions, Flows and Apex.

## Decision

- Lead convert and merge will be normal methods with SOAP hidden inside, using OAuth tokens,
  never SOAP login (D14).
- Org automation is reached through generic calls to invocable actions (standard, Flow, Apex) and
  Apex REST. `esf generate actions` will produce typed stubs. Anonymous Apex lives under Tooling
  and runs only when called explicitly. Triggers can't be called directly (D26).

## Implementation (0.2)

- `sf.leads.convert()`, `sf.leads.convert_many()` (100 per call) and `sf.merge()` (Lead, Contact,
  Account; one or two duplicates) post Partner SOAP envelopes to `/services/Soap/u/{version}`. The
  access token goes in `SessionHeader.sessionId`, built per attempt so a refreshed token gets a new
  body. No `Authorization` header goes to the SOAP endpoint.
- Envelope building and parsing are pure functions in `core/soap.py`. The parser is stdlib `expat`
  and refuses any DTD or entity declaration. Faults map by `exceptionCode` to the §8 classes, else
  `SoapFault`.
- Sources: [convertLead()](https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_calls_convertlead.htm),
  [LeadConvertResult](https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_calls_convertlead_leadconvertresult.htm),
  [merge()](https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_calls_merge.htm),
  [Error handling](https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_concepts_errorhandling.htm).
  The wire details still need a sandbox check (V11, V29).

## Consequences

- None of this is in 0.1. Actions, Apex REST helpers and convert/merge are planned for 0.2, and
  Tooling for 0.3. `esf generate` exits with "not available until 0.2".
