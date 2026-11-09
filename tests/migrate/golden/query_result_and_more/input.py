from simple_salesforce import Salesforce

sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")


def page():
    res = sf.query("SELECT Id FROM Lead")
    total = res["totalSize"]
    rows = res.get("records", [])
    if not res["done"]:
        more = sf.query_more(res["nextRecordsUrl"], True)
    return total, rows, sf.query("SELECT Id FROM Lead")["records"]


def other(db):
    return db.query("SELECT 1")["records"]
