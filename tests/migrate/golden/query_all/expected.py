# esf-migrate: TODO[unsupported] still uses simple-salesforce (Salesforce); replace before uninstalling it
from simple_salesforce import Salesforce
# esf-migrate: TODO[unsupported] still uses simple-salesforce (AsyncSalesforce); replace before uninstalling it
from simple_salesforce.aio import AsyncSalesforce

# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")


def sync_records():
    return list(sf.query_iter("SELECT Id FROM Lead"))


def sync_assigned():
    res = list(sf.query_iter("SELECT Id FROM Lead", include_deleted=True))
    return res, res


def sync_other():
    # esf-migrate: REVIEW[query-all] query_all() returned a dict ({'records': [...], 'totalSize': ...}); this is now the list of rows
    res = list(sf.query_iter("SELECT Id FROM Lead"))
    return res["totalSize"]


async def async_forms(asf: AsyncSalesforce):
    rows = [r async for r in asf.query_iter("SELECT Id FROM Lead")]
    async for row in asf.query_iter("SELECT Id FROM Lead", include_deleted=True):
        rows.append(row)
    return rows


def iterate():
    for row in sf.query_iter("SELECT Id FROM Lead", include_deleted=True):
        print(row)
