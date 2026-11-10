from simple_salesforce import Salesforce


def reassigned(sf: Salesforce):
    res = sf.query("SELECT Id FROM Lead")
    res = sf.query("SELECT Id FROM Contact")
    return res["records"]


def returned(sf: Salesforce):
    r = sf.query("SELECT Id FROM Lead")
    return r


def passed_on(sf: Salesforce, process):
    data = sf.query("SELECT Id FROM Lead")
    process(data)


def followed(sf: Salesforce):
    page = sf.query("SELECT Id FROM Lead")
    sf.query("SELECT Id FROM Contact")
    return page.records, page["totalSize"], sf.query("SELECT Id FROM Case").get("records", [])
