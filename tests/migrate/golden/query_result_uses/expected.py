# esf-migrate: TODO[unsupported] still uses simple-salesforce (Salesforce); replace before uninstalling it
from simple_salesforce import Salesforce


def reassigned(sf: Salesforce):
    # esf-migrate: TODO[query-result] query() returns a QueryResult (records, total_size, done, cursor), not a dict; check how the result is used
    res = sf.query("SELECT Id FROM Lead")
    # esf-migrate: TODO[query-result] query() returns a QueryResult (records, total_size, done, cursor), not a dict; check how the result is used
    res = sf.query("SELECT Id FROM Contact")
    return res["records"]


def returned(sf: Salesforce):
    # esf-migrate: TODO[query-result] query() returns a QueryResult (records, total_size, done, cursor), not a dict; check how the result is used
    r = sf.query("SELECT Id FROM Lead")
    return r


def passed_on(sf: Salesforce, process):
    # esf-migrate: TODO[query-result] query() returns a QueryResult (records, total_size, done, cursor), not a dict; check how the result is used
    data = sf.query("SELECT Id FROM Lead")
    process(data)


def followed(sf: Salesforce):
    page = sf.query("SELECT Id FROM Lead")
    sf.query("SELECT Id FROM Contact")
    return page.records, page.total_size, sf.query("SELECT Id FROM Case").records
