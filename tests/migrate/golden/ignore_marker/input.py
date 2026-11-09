from simple_salesforce import Salesforce

sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")

# esf-migrate: ignore[sobject-attr]
sf.Lead.get("00Q000000000001AAA")
sf.Contact.get("003000000000001AAA")
