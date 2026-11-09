from simple_salesforce import Salesforce

sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")

# esf-migrate: ignore[create-return]
r = sf.Lead.create({})
# esf-migrate: ignore[return-value]
status = sf.Lead.update("00Q000000000001AAA", {})
# esf-migrate: ignore[get-by-custom-id]
sf.Lead.get_by_custom_id("Ext__c", "1")
# esf-migrate: ignore[upsert-path]
sf.Lead.upsert("Ext__c/1", {})
