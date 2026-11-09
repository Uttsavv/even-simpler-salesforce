from simple_salesforce import Salesforce

sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")

# a comment that must survive

lead = sf.Lead.get("00Q000000000001AAA")
sf.Contact.update("003000000000001AAA", {"LastName": "Doe"})
sf.Invoice__c.delete("a00000000000001AAA")
event = sf.Order_Event__e.describe()
account = sf.Account.get(sf.Lead.get("00Q000000000001AAA")["ConvertedAccountId"])
sf.Lead.get_by_custom_id("Ext__c", "42")
