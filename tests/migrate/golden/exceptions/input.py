from simple_salesforce import Salesforce
from simple_salesforce.exceptions import (
    SalesforceExpiredSession,
    SalesforceMalformedRequest,
    SalesforceResourceNotFound,
)

sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")


def get(lead_id):
    try:
        return sf.Lead.get(lead_id)
    except SalesforceResourceNotFound:
        return None
    except SalesforceExpiredSession as exc:
        raise RuntimeError("expired") from exc
    except SalesforceMalformedRequest:
        raise
