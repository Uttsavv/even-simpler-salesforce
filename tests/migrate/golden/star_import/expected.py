# esf-migrate: TODO[unsupported] import * from simple-salesforce; import the names you still use, then remove it
from simple_salesforce import *
import even_simpler_salesforce as esf


def load(sf: Salesforce, record_id):
    try:
        return sf.sobject("Lead").get(record_id)
    except esf.NotFound:
        return None
