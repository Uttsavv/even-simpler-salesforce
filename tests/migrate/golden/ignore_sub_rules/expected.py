# esf-migrate: TODO[unsupported] still uses simple-salesforce (Salesforce); replace before uninstalling it
from simple_salesforce import Salesforce

# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")

# esf-migrate: ignore[create-return]
r = sf.sobject("Lead").create({})
# esf-migrate: ignore[return-value]
status = sf.sobject("Lead").update("00Q000000000001AAA", {})
# esf-migrate: ignore[get-by-custom-id]
sf.sobject("Lead").get_by_custom_id("Ext__c", "1")
# esf-migrate: ignore[upsert-path]
sf.sobject("Lead").upsert("Ext__c/1", {})
