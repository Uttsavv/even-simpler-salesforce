# esf-migrate: TODO[unsupported] still uses simple-salesforce (format_soql); replace before uninstalling it
from simple_salesforce import format_soql


def by_email(sf, email):
    # esf-migrate: TODO[format-soql] couldn't confirm the client of this query; pass the values to the query method: sf.query("... = {}", value)
    return sf.query(format_soql("SELECT Id FROM Lead WHERE Email = {}", email))
