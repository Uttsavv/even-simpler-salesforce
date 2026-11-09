# esf-migrate: TODO[unsupported] still uses simple-salesforce (Salesforce); replace before uninstalling it
from simple_salesforce import Salesforce
# esf-migrate: TODO[unsupported] still uses simple-salesforce (SalesforceExpiredSession, SalesforceMalformedRequest); replace before uninstalling it
from simple_salesforce.exceptions import (
    SalesforceExpiredSession,
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
    # esf-migrate: TODO[exceptions] SalesforceExpiredSession has no 1:1 class; refresh is automatic: SessionExpired means a 401 survived a refresh, and a dead refresh token raises ReauthorizationRequired; catch esf.AuthError or both (often the handler can go)
    except SalesforceExpiredSession as exc:
        raise RuntimeError("expired") from exc
    # esf-migrate: TODO[exceptions] SalesforceMalformedRequest has no 1:1 class; catch esf.RequestError / esf.RecordError or a specific class (InvalidField, MalformedQuery, ValidationRuleFailed, ...)
    except SalesforceMalformedRequest:
        raise
