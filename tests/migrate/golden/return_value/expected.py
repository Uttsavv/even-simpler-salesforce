from simple_salesforce import Salesforce

# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")

# esf-migrate: REVIEW[return-value] update() returned an HTTP status code; it now returns None
status = sf.sobject("Lead").update("00Q000000000001AAA", {"Status": "Working"})
# esf-migrate: REVIEW[return-value] upsert() returned an HTTP status code; it now returns an UpsertResult(id, created)
if sf.sobject("Account").upsert("Ext__c", "1", {"Name": "A"}) == 201:
    print("created")
sf.sobject("Lead").delete("00Q000000000001AAA")
