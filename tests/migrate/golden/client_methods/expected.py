# esf-migrate: TODO[unsupported] still uses simple-salesforce (Salesforce); replace before uninstalling it
from simple_salesforce import Salesforce


def org_info(sf: Salesforce, term, sosl):
    objects = sf.describe_global()
    lead = sf.sobject("Lead").describe()
    # esf-migrate: TODO[client-methods] quick_search(term) → parameterized_search(term), which returns the list of records
    found = sf.quick_search(term)
    # esf-migrate: TODO[client-methods] search() now returns the list of records; drop ["searchRecords"]
    hits = sf.search(sosl)["searchRecords"]
    # esf-migrate: TODO[client-methods] limits() returns dict[str, Limit]: ["X"]["Remaining"] → ["X"].remaining, ["Max"] → .max
    left = sf.limits()["DailyApiRequests"]["Remaining"]
    # esf-migrate: TODO[client-methods] limits() returns dict[str, Limit]: ["X"]["Remaining"] → ["X"].remaining, ["Max"] → .max
    limits = sf.limits()
    most = limits["DailyApiRequests"]["Max"]
    # esf-migrate: TODO[client-methods] is_sandbox() isn't provided; query SELECT IsSandbox FROM Organization
    return objects, lead, found, hits, left, most, sf.is_sandbox(), sf.search(sosl)
