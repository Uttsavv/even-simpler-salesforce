from simple_salesforce import Salesforce

# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")

# a comment that must survive

lead = sf.sobject("Lead").get("00Q000000000001AAA")
sf.sobject("Contact").update("003000000000001AAA", {"LastName": "Doe"})
sf.sobject("Invoice__c").delete("a00000000000001AAA")
event = sf.sobject("Order_Event__e").describe()
account = sf.sobject("Account").get(sf.sobject("Lead").get("00Q000000000001AAA")["ConvertedAccountId"])
sf.sobject("Lead").get_by_external_id("Ext__c", "42")
