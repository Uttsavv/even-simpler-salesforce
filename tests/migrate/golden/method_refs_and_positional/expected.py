import functools

# esf-migrate: TODO[unsupported] still uses simple-salesforce (Salesforce); replace before uninstalling it
from simple_salesforce import Salesforce

# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")

fn = sf.sobject("Lead").create
update = functools.partial(sf.sobject("Contact").update, "003000000000001AAA")
# esf-migrate: TODO[sobject-attr] a per-object method is replaced here; sf.sobject() returns a new object each time
sf.Lead.create = fake_create
rows = sf.query("SELECT Id FROM Lead", include_deleted=True)
