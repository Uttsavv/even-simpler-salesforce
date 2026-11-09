from simple_salesforce import Salesforce

sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")


def sync(code, data, path):
    sf.Account.upsert("Ext__c/A-42", data)
    sf.Account.upsert(f"Ext__c/{code}", data)
    sf.Account.upsert(f"Ext__c/{code}-{data['n']}", data)
    sf.Account.upsert(path, data)
