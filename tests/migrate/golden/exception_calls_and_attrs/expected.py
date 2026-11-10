# esf-migrate: TODO[unsupported] still uses simple-salesforce (SalesforceResourceNotFound); replace before uninstalling it
from simple_salesforce import SalesforceResourceNotFound
import even_simpler_salesforce as esf


def load(sf, record_id):
    try:
        # esf-migrate: REVIEW[not-a-client] rewritten to sobject(); couldn't confirm this is a Salesforce client
        return sf.sobject("Account").get(record_id)
    except esf.NotFound as e:
        # esf-migrate: TODO[exceptions] simple-salesforce exceptions had .content; esf errors have .raw
        # esf-migrate: TODO[exceptions] simple-salesforce exceptions had .resource_name; esf errors have .sobject
        # esf-migrate: TODO[exceptions] simple-salesforce exceptions had .url; esf errors have .path
        print(e.content, e.resource_name, e.url, e.status)
        return None


def missing():
    # esf-migrate: TODO[exceptions] SalesforceResourceNotFound(...) takes (url, status, resource_name, content); esf.NotFound takes a message and keyword arguments (status=, sobject=, path=, raw=)
    raise SalesforceResourceNotFound("url", 404, "Account", b"[]")


def generic():
    raise esf.SalesforceError()
