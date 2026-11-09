from simple_salesforce import Salesforce, SalesforceLogin

# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
session_id, instance = SalesforceLogin(username="u", password="p", security_token="t")
# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
sf = Salesforce(instance=instance, session_id=session_id)
# esf-migrate: TODO[unsupported] Bulk API v1 isn't supported; use sf.bulk (Bulk 2.0, planned for 0.3)
sf.bulk.Lead.insert([{"LastName": "Doe"}])
