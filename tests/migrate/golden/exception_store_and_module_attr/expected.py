# esf-migrate: TODO[unsupported] still uses simple-salesforce (simple_salesforce); replace before uninstalling it
import simple_salesforce
import even_simpler_salesforce as esf

try:
    # esf-migrate: TODO[unsupported] still uses simple-salesforce (SalesforceMoreThanOneRecord); replace before uninstalling it
    from simple_salesforce import SalesforceMoreThanOneRecord
except ImportError:
    # esf-migrate: TODO[exceptions] SalesforceMoreThanOneRecord is assigned here; replace this fallback with the esf error class
    SalesforceMoreThanOneRecord = None


def get(sf):
    try:
        return sf.query("SELECT Id FROM Lead")
    except esf.NotFound:
        return None
    # esf-migrate: TODO[exceptions] SalesforceMalformedRequest has no 1:1 class; catch esf.RequestError / esf.RecordError or a specific class (InvalidField, MalformedQuery, ValidationRuleFailed, ...)
    except simple_salesforce.exceptions.SalesforceMalformedRequest:
        raise
