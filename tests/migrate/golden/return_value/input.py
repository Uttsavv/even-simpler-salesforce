from simple_salesforce import Salesforce

sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")

status = sf.Lead.update("00Q000000000001AAA", {"Status": "Working"})
if sf.Account.upsert("Ext__c/1", {"Name": "A"}) == 201:
    print("created")
sf.Lead.delete("00Q000000000001AAA")
