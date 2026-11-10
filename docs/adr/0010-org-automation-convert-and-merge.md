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

## Consequences

- None of this is in 0.1. Actions, Apex REST helpers and convert/merge are planned for 0.2, and
  Tooling for 0.3. `esf generate` exits with "not available until 0.2".

## Implementation notes (0.2)

- `sf.actions.standard/flow/apex(name, inputs)` take one dict or a list. One input returns the
  output values, or raises the typed error of its first error entry. A list returns a
  `BatchResult[ActionResult]`. A `400` whose body is the per-invocation result list is not an
  HTTP error for these calls; the area maps each `isSuccess=false` entry itself.
- `sf.apex.*` errors become `ApexError` with the decoded body. Auth errors, limit errors, gateway
  errors with an empty body and non-JSON pages keep their own classes, so refresh and retries
  still work. `sf.request()` on an Apex REST path keeps the status-based mapping.
- `esf.Action[R]` stubs are dataclasses or Pydantic models with `__action__ = (kind, name)`;
  `sf.run(stub)` sends the fields under their API names and builds `R` from the outputs.
- `esf generate actions --org NAME` reads the login from `ESF_<NAME>_*` variables (the CLI reads
  the login only from the environment, §9.18), not from `sf` CLI aliases (rejected under D29).
- Sources: [Invoking Actions](https://developer.salesforce.com/docs/atlas.en-us.api_action.meta/api_action/actions_intro_invoking.htm),
  [Flow Actions](https://developer.salesforce.com/docs/atlas.en-us.api_action.meta/api_action/actions_obj_flow.htm),
  [Apex Actions](https://developer.salesforce.com/docs/atlas.en-us.api_action.meta/api_action/actions_obj_apex.htm),
  [Apex REST](https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_rest_intro.htm).
  Open points are V29–V32 in the spec's verification backlog.
