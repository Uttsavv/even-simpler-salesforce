import simple_salesforce

try:
    from simple_salesforce import SalesforceMoreThanOneRecord
except ImportError:
    SalesforceMoreThanOneRecord = None


def get(sf):
    try:
        return sf.query("SELECT Id FROM Lead")
    except simple_salesforce.SalesforceResourceNotFound:
        return None
    except simple_salesforce.exceptions.SalesforceMalformedRequest:
        raise
