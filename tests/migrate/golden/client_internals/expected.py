# esf-migrate: TODO[unsupported] still uses simple-salesforce (AsyncSalesforce); replace before uninstalling it
from simple_salesforce.aio import AsyncSalesforce


def links(sf: AsyncSalesforce, record_id: str, payload):
    # esf-migrate: TODO[client-internals] sf_instance: use sf.instance_url (the instance of the current token; None before the first call); a hand-built SOAP convertLead/merge call becomes sf.leads.convert(...) / sf.merge(...)
    base = f"https://{sf.sf_instance}/{record_id}"
    version = sf.api_version
    # esf-migrate: TODO[client-internals] session_id: use sf.auth.token().access_token (and sf.auth.refresh() after INVALID_SESSION_ID; await both on AsyncSalesforce); a hand-built SOAP convertLead/merge call becomes sf.leads.convert(...) / sf.merge(...)
    token = sf.session_id
    # esf-migrate: TODO[client-internals] headers: make calls with sf.request(...); auth headers are added for you
    headers = sf.headers
    other = payload.session_id
    return base, version, token, headers, other
