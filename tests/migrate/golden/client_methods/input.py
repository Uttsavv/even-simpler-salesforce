from simple_salesforce import Salesforce


def org_info(sf: Salesforce, term, sosl):
    objects = sf.describe()
    lead = sf.Lead.describe()
    found = sf.quick_search(term)
    hits = sf.search(sosl)["searchRecords"]
    left = sf.limits()["DailyApiRequests"]["Remaining"]
    limits = sf.limits()
    most = limits["DailyApiRequests"]["Max"]
    return objects, lead, found, hits, left, most, sf.is_sandbox(), sf.search(sosl)
