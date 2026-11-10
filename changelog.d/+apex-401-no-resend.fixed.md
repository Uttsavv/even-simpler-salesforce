A `401` without `INVALID_SESSION_ID` or `invalid_token`, such as one an Apex REST class returns itself, no longer refreshes and resends a write. Reads still get one refresh and resend.
