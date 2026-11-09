from simple_salesforce.aio import AsyncSalesforce


def links(sf: AsyncSalesforce, record_id: str, payload):
    base = f"https://{sf.sf_instance}/{record_id}"
    version = sf.sf_version
    token = sf.session_id
    headers = sf.headers
    other = payload.session_id
    return base, version, token, headers, other
