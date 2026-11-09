from simple_salesforce import Salesforce
from simple_salesforce.exceptions import (
    SalesforceMalformedRequest,
)
import even_simpler_salesforce as esf

# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")


def get(lead_id):
    try:
        return sf.sobject("Lead").get(lead_id)
    except esf.NotFound:
        return None
    except esf.SessionExpired as exc:
        raise RuntimeError("expired") from exc
    # esf-migrate: TODO[exceptions] SalesforceMalformedRequest has no 1:1 class; catch esf.RequestError (or a specific class: InvalidField, MalformedQuery, ValidationRuleFailed, ...)
    except SalesforceMalformedRequest:
        raise
