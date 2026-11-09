# esf-migrate: TODO[unsupported] still uses simple-salesforce (Salesforce); replace before uninstalling it
from simple_salesforce import Salesforce

# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")


def follow():
    leads = sf.sobject("Lead")
    leads.update("00Q000000000001AAA", {"Status": "Working"})
    leads.get("00Q000000000001AAA")


def leaks():
    # esf-migrate: TODO[stored-ref] sf.Contact is stored and used in ways the tool can't follow; use sf.sobject("Contact")
    contacts = sf.Contact
    register(contacts)


limit = settings.MAX_ROWS
