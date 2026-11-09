from simple_salesforce import Salesforce

sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")

sf.Lead.describe_layout("012000000000001AAA")
sf.Lead.update("00Q000000000001AAA", {}, headers={"Sforce-Auto-Assign": "FALSE"})
sf.Lead.get("00Q000000000001AAA", raw_response=True)
sf.Lead.upsert(record_id="Ext__c/1", data={})
sf.bulk.Lead.update([{"Id": "00Q000000000001AAA"}])
sf.mdapi.CustomObject.create({})
