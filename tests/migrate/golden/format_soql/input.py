from simple_salesforce import Salesforce, format_soql

sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")


def by_email(email, statuses):
    return sf.query(format_soql("SELECT Id FROM Lead WHERE Email = {} AND Status IN {}", email, statuses))


def named(email):
    return sf.query(format_soql("SELECT Id FROM Lead WHERE Email = {email}", email=email))


def built_elsewhere(email):
    q = format_soql("SELECT Id FROM Lead WHERE Email = {}", email)
    return sf.query(q)
