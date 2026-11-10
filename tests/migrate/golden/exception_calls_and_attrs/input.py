from simple_salesforce import SalesforceError, SalesforceResourceNotFound


def load(sf, record_id):
    try:
        return sf.Account.get(record_id)
    except SalesforceResourceNotFound as e:
        print(e.content, e.resource_name, e.url, e.status)
        return None


def missing():
    raise SalesforceResourceNotFound("url", 404, "Account", b"[]")


def generic():
    raise SalesforceError()
