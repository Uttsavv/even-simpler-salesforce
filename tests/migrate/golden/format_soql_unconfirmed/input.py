from simple_salesforce import format_soql


def by_email(sf, email):
    return sf.query(format_soql("SELECT Id FROM Lead WHERE Email = {}", email))
