import functools

# esf-migrate: TODO[unsupported] still uses simple-salesforce (Salesforce); replace before uninstalling it
from simple_salesforce import Salesforce

# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")

# esf-migrate: REVIEW[create-return] create() now returns the id (str), not a dict; check the callers
fn = sf.sobject("Lead").create
update = functools.partial(sf.sobject("Contact").update, "003000000000001AAA")
# esf-migrate: TODO[sobject-attr] a per-object method is replaced here; sf.sobject() returns a new object each time
sf.Lead.create = fake_create
rows = sf.query("SELECT Id FROM Lead", include_deleted=True)
by_ext = sf.sobject("Account").get_by_external_id
# esf-migrate: REVIEW[upsert-path] upsert() now takes the field and value separately: upsert("Ext__c", value, data); check the callers
put = sf.sobject("Account").upsert
# esf-migrate: TODO[unsupported] describe_layout() has no 0.1 method; use sf.request("GET", "sobjects/Account/describe/layouts/<recordTypeId>")
layout = sf.Account.describe_layout
