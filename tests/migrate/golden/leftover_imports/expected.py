# esf-migrate: TODO[unsupported] still uses simple-salesforce (simple_salesforce); replace before uninstalling it
import simple_salesforce
# esf-migrate: TODO[unsupported] still uses simple-salesforce (SFType); replace before uninstalling it
from simple_salesforce import SFType
# esf-migrate: TODO[unsupported] still uses simple-salesforce (date_to_iso8601); replace before uninstalling it
from simple_salesforce.util import date_to_iso8601

# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
sf = simple_salesforce.Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")
lead = SFType("Lead", "session", "host")
stamp = date_to_iso8601(today)
sf.sobject("Lead").get("00Q000000000001AAA")
