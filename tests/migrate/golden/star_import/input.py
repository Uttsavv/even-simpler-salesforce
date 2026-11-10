from simple_salesforce import *


def load(sf: Salesforce, record_id):
    try:
        return sf.Lead.get(record_id)
    except SalesforceResourceNotFound:
        return None
