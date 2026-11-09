# esf-migrate: TODO[unsupported] still uses simple-salesforce (SalesforceResourceNotFound); replace before uninstalling it
from simple_salesforce import SalesforceResourceNotFound
import even_simpler_salesforce as esf


def a():
    try:
        pass
    # esf-migrate: ignore[exceptions]
    except SalesforceResourceNotFound:
        pass


def b():
    try:
        pass
    except esf.NotFound:
        pass
