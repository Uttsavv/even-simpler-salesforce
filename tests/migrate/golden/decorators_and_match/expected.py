# esf-migrate: TODO[unsupported] still uses simple-salesforce (Salesforce); replace before uninstalling it
from simple_salesforce import Salesforce

# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")


# esf-migrate: TODO[create-return] create() now returns the id (str), not a dict; check how the result is used
@register(sf.sobject("Lead").create({"LastName": "Doe"}))
def handler(default=sf.sobject("Contact").describe()):
    pass


class Model(Base, meta=sf.sobject("Account").metadata()):
    pass


# esf-migrate: TODO[create-return] create() now returns the id (str), not a dict; check how the result is used
match sf.sobject("Lead").create({"LastName": "Doe"}):
    case _:
        pass
