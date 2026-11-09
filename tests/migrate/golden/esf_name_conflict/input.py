from simple_salesforce import SalesforceResourceNotFound

esf = "estimated ship date"


def get(sf):
    try:
        return sf.query("SELECT Id FROM Lead")
    except SalesforceResourceNotFound:
        return esf
