from simple_salesforce import Salesforce

# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")


def sync(code, data, path):
    sf.sobject("Account").upsert("Ext__c", "A-42", data)
    sf.sobject("Account").upsert("Ext__c", str(code), data)
    sf.sobject("Account").upsert("Ext__c", f"{code}-{data['n']}", data)
    # esf-migrate: TODO[upsert-path] upsert() now takes the external id field and value separately: upsert("Ext__c", value, data)
    sf.sobject("Account").upsert(path, data)
