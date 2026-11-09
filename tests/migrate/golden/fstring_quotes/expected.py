# esf-migrate: TODO[unsupported] still uses simple-salesforce (Salesforce); replace before uninstalling it
from simple_salesforce import Salesforce

# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")

print(f"{sf.sobject('Lead').get('00Q000000000001AAA')}")
print(f'{sf.sobject("Contact").get("003000000000001AAA")}')
# esf-migrate: REVIEW[return-value] upsert() returned an HTTP status code; it now returns an UpsertResult(id, created)
print(f"""{sf.sobject('Account').upsert('Ext__c', str(1), {})}""")
