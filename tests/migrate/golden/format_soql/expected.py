# esf-migrate: TODO[unsupported] still uses simple-salesforce (Salesforce, format_soql); replace before uninstalling it
from simple_salesforce import Salesforce, format_soql

# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")


def by_email(email, statuses):
    # esf-migrate: TODO[query-result] query() returns a QueryResult (records, total_size, done, cursor), not a dict; check how the result is used
    return sf.query("SELECT Id FROM Lead WHERE Email = {} AND Status IN {}", email, statuses)


def named(email):
    # esf-migrate: TODO[query-result] query() returns a QueryResult (records, total_size, done, cursor), not a dict; check how the result is used
    # esf-migrate: TODO[format-soql] only plain positional {} placeholders convert; use esf.soql() for {name} / {:like}
    return sf.query(format_soql("SELECT Id FROM Lead WHERE Email = {email}", email=email))


def built_elsewhere(email):
    # esf-migrate: TODO[format-soql] pass the values to the query method instead: sf.query("... = {}", value)
    q = format_soql("SELECT Id FROM Lead WHERE Email = {}", email)
    # esf-migrate: TODO[query-result] query() returns a QueryResult (records, total_size, done, cursor), not a dict; check how the result is used
    return sf.query(q)
