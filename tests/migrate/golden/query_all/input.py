from simple_salesforce import Salesforce
from simple_salesforce.aio import AsyncSalesforce

sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")


def sync_records():
    return sf.query_all("SELECT Id FROM Lead")["records"]


def sync_assigned():
    res = sf.query_all("SELECT Id FROM Lead", True)
    return res["records"], res.get("records", [])


def sync_other():
    res = sf.query_all("SELECT Id FROM Lead")
    return res["totalSize"]


async def async_forms(asf: AsyncSalesforce):
    rows = (await asf.query_all("SELECT Id FROM Lead"))["records"]
    async for row in asf.query_all_iter("SELECT Id FROM Lead", include_deleted=True):
        rows.append(row)
    return rows


def iterate():
    for row in sf.query_all_iter("SELECT Id FROM Lead", True):
        print(row)
