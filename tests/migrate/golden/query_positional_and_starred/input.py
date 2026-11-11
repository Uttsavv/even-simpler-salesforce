from simple_salesforce import Salesforce, format_soql


def rows(sf: Salesforce, q, flag, email, opts, parts, rest):
    a = sf.query("SELECT Id FROM Lead", flag)
    b = sf.query(q, True)
    c = sf.query(q, flag)
    d = sf.query(format_soql("SELECT Id FROM Lead WHERE Email = {}", email), True)
    e = sf.query("SELECT Id FROM Lead WHERE Email = {}", email)
    sf.restful("limits", **opts)
    sf.restful(*parts)
    sf.apexecute("orders", **opts)
    f = sf.query_all(q, *rest)
    return a, b, c, d, e, f
