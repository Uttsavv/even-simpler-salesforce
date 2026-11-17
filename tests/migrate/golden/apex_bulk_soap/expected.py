# esf-migrate: TODO[unsupported] still uses simple-salesforce (Salesforce); replace before uninstalling it
from simple_salesforce import Salesforce
import even_simpler_salesforce as esf


class Repo:
    def __init__(self, sf_client: Salesforce) -> None:
        self.sf_client = sf_client

    def consent(self, path, data):
        # esf-migrate: REVIEW[apexecute] apexecute(path) → apex.post("/path"); errors from the Apex class raise esf.ApexError, also an unknown path (status 404, not esf.NotFound); check the path has no query string (use params=)
        return self.sf_client.apex.post("/" + path, json=data)

    def otp(self):
        return self.sf_client.apex.post("/api/v1/pilot-otp/send", json={"a": 1})

    def ping(self):
        return self.sf_client.apex.get("/api/v1/ping")

    def odd(self, verb):
        # esf-migrate: TODO[apexecute] use sf.apex.get("/<path>") / sf.apex.post("/<path>", json=data) (also put, patch, delete)
        a = self.sf_client.apexecute("api/v1/x", method=verb)
        # esf-migrate: TODO[apexecute] use sf.apex.get("/<path>") / sf.apex.post("/<path>", json=data) (also put, patch, delete)
        b = self.sf_client.apexecute("api/v1/x", method="GET", data={"q": 1})
        # esf-migrate: TODO[apexecute] use sf.apex.get("/<path>") / sf.apex.post("/<path>", json=data) (also put, patch, delete); pass query parameters with params=
        return a, b, self.sf_client.apexecute("api/v1/x?y=1")

    def load(self, payloads):
        # esf-migrate: TODO[bulk] Bulk API v1 isn't supported; use job = sf.bulk.ingest("Lead", "insert", rows=payloads), then job.wait(); read job.successful_results() / job.failed_results() (await ingest() and wait() on AsyncSalesforce); batch_size= and use_serial= have no equivalent (the client splits large data)
        return self.sf_client.bulk.Lead.insert(payloads, batch_size=10000, use_serial=False)

    def soap(self):
        # esf-migrate: TODO[client-internals] sf_instance: use sf.instance_url (the instance of the current token; None before the first call); a hand-built SOAP convertLead/merge call becomes sf.leads.convert(...) / sf.merge(...)
        url = f"https://{self.sf_client.sf_instance}/services/Soap/u/60.0"
        # esf-migrate: TODO[client-internals] session_id: use sf.auth.token().access_token (and sf.auth.refresh() after INVALID_SESSION_ID; await both on AsyncSalesforce); a hand-built SOAP convertLead/merge call becomes sf.leads.convert(...) / sf.merge(...)
        return url, self.sf_client.session_id


def top(sf: Salesforce, rows, soql):
    # esf-migrate: TODO[bulk] Bulk API v1 isn't supported; use job = sf.bulk.ingest("Contact", "upsert", rows=rows, external_id_field="Ext__c"), then job.wait(); read job.successful_results() / job.failed_results() (await ingest() and wait() on AsyncSalesforce)
    sf.bulk.Contact.upsert(rows, external_id_field="Ext__c")
    # esf-migrate: TODO[bulk] Bulk API v1 isn't supported; use job = sf.bulk.ingest("Lead", "hardDelete", rows=rows), then job.wait(); read job.successful_results() / job.failed_results() (await ingest() and wait() on AsyncSalesforce)
    sf.bulk.Lead.hard_delete(rows)
    # esf-migrate: TODO[bulk] Bulk API v1 isn't supported; use sf.bulk.query(soql, include_deleted=True) (Bulk API 2.0)
    found = sf.bulk.Account.query_all(soql)
    # esf-migrate: TODO[bulk] Bulk API v1 isn't supported; use sf.bulk (Bulk API 2.0): sf.bulk.ingest(sobject, operation, rows=...) or sf.bulk.query(soql)
    jobs = sf.bulk
    # esf-migrate: TODO[unsupported] use sf.tooling.query(soql), sf.tooling.sobject(name) or sf.tooling.execute_anonymous(code); other Tooling paths: sf.request(method, "tooling/<path>")
    return found, jobs, sf.toolingexecute("query/?q=SELECT+Id+FROM+ApexClass")


def more(sf: Salesforce, payloads, flag):
    # esf-migrate: TODO[bulk] Bulk API v1 isn't supported; use job = sf.bulk.ingest("Lead", "insert", rows=payloads), then job.wait(); read job.successful_results() / job.failed_results() (await ingest() and wait() on AsyncSalesforce); batch_size= has no equivalent (the client splits large data)
    sf.bulk.Lead.insert(data=payloads, batch_size=500)
    # esf-migrate: TODO[bulk] Bulk API v1 isn't supported; use job = sf.bulk.ingest("Lead", "insert", rows=rows), then job.wait(); read job.successful_results() / job.failed_results() (await ingest() and wait() on AsyncSalesforce)
    sf.bulk.Lead.insert(load(token="abc123secret"))
    # esf-migrate: REVIEW[apexecute] apexecute(path) → apex.get("/path"); errors from the Apex class raise esf.ApexError, also an unknown path (status 404, not esf.NotFound); check the path has no query string (use params=)
    return sf.apex.get("/" + ("a" if flag else "b"))


def guarded(sf: Salesforce):

    try:
        # esf-migrate: REVIEW[apexecute] apexecute(path) → apex.get("/path"); errors from the Apex class raise esf.ApexError, also an unknown path (status 404, not esf.NotFound); the except SalesforceResourceNotFound around it becomes esf.NotFound, which this call doesn't raise: catch esf.ApexError and check .status == 404
        return sf.apex.get("/api/v1/maybe")
    except esf.NotFound:
        return None


def aliased(sf: Salesforce):

    try:
        # esf-migrate: REVIEW[apexecute] apexecute(path) → apex.get("/path"); errors from the Apex class raise esf.ApexError, also an unknown path (status 404, not esf.NotFound); the except SalesforceResourceNotFound around it becomes esf.NotFound, which this call doesn't raise: catch esf.ApexError and check .status == 404
        return sf.apex.get("/api/v1/other")
    except (ValueError, esf.NotFound):
        return None
