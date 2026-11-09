from simple_salesforce import SalesforceResourceNotFound


def a():
    try:
        pass
    # esf-migrate: ignore[exceptions]
    except SalesforceResourceNotFound:
        pass


def b():
    try:
        pass
    except SalesforceResourceNotFound:
        pass
