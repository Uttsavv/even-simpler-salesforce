from simple_salesforce import Salesforce

# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")


def page():
    res = sf.query("SELECT Id FROM Lead")
    total = res.total_size
    rows = res.records
    if not res.done:
        # esf-migrate: TODO[query-result] query() returns a QueryResult (records, total_size, done, cursor), not a dict
        # esf-migrate: TODO[query-more] query_more() takes a Cursor (page.cursor), not a URL; or use query_iter()
        more = sf.query_more(res["nextRecordsUrl"], True)
    return total, rows, sf.query("SELECT Id FROM Lead").records


def other(db):
    return db.query("SELECT 1")["records"]
