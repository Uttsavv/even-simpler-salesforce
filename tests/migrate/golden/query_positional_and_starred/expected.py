# esf-migrate: TODO[unsupported] still uses simple-salesforce (Salesforce); replace before uninstalling it
from simple_salesforce import Salesforce


def rows(sf: Salesforce, q, flag, email, opts, parts, rest):
    # esf-migrate: TODO[query-result] query() returns a QueryResult (records, total_size, done, cursor), not a dict; check how the result is used
    a = sf.query("SELECT Id FROM Lead", include_deleted=flag)
    # esf-migrate: TODO[query-result] query() returns a QueryResult (records, total_size, done, cursor), not a dict; check how the result is used
    # esf-migrate: REVIEW[query-result] query(q, flag): include_deleted is a keyword argument now
    b = sf.query(q, include_deleted=True)
    # esf-migrate: TODO[query-result] query() returns a QueryResult (records, total_size, done, cursor), not a dict; check how the result is used
    # esf-migrate: TODO[query-result] query(q, x) passed include_deleted positionally; positional values now fill {} placeholders, so write include_deleted=x
    c = sf.query(q, flag)
    # esf-migrate: TODO[query-result] query() returns a QueryResult (records, total_size, done, cursor), not a dict; check how the result is used
    d = sf.query("SELECT Id FROM Lead WHERE Email = {}", email, include_deleted=True)
    # esf-migrate: TODO[query-result] query() returns a QueryResult (records, total_size, done, cursor), not a dict; check how the result is used
    e = sf.query("SELECT Id FROM Lead WHERE Email = {}", email)
    # esf-migrate: TODO[restful] *args / **kwargs: rewrite this call by hand
    sf.restful("limits", **opts)
    # esf-migrate: TODO[restful] *args / **kwargs: rewrite this call by hand
    sf.restful(*parts)
    # esf-migrate: TODO[apexecute] *args / **kwargs: rewrite this call by hand
    sf.apexecute("orders", **opts)
    # esf-migrate: TODO[query-all] *args / **kwargs: rewrite this call by hand
    f = sf.query_all(q, *rest)
    return a, b, c, d, e, f
