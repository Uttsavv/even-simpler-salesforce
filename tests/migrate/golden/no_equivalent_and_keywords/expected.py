# esf-migrate: TODO[unsupported] still uses simple-salesforce (Salesforce); replace before uninstalling it
from simple_salesforce import Salesforce

# esf-migrate: TODO[login] construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces session_id=/instance=; username/password login isn't supported
sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")

# esf-migrate: TODO[unsupported] describe_layout() has no method here; use sf.request("GET", "sobjects/Lead/describe/layouts/<recordTypeId>")
sf.Lead.describe_layout("012000000000001AAA")
# esf-migrate: TODO[unsupported] headers= has no equivalent here; drop it or use sf.request(...)
sf.sobject("Lead").update("00Q000000000001AAA", {}, headers={"Sforce-Auto-Assign": "FALSE"})
# esf-migrate: TODO[unsupported] raw_response= has no equivalent here; drop it or use sf.request(...)
sf.sobject("Lead").get("00Q000000000001AAA", raw_response=True)
# esf-migrate: TODO[upsert-path] upsert() takes the external id field and value positionally: upsert("Ext__c", value, data)
sf.sobject("Lead").upsert(record_id="Ext__c/1", data={})
# esf-migrate: TODO[bulk] Bulk API v1 isn't supported; use job = sf.bulk.ingest("Lead", "update", rows=[{"Id": "00Q000000000001AAA"}]), then job.wait(); read job.successful_results() / job.failed_results()
sf.bulk.Lead.update([{"Id": "00Q000000000001AAA"}])
# esf-migrate: TODO[unsupported] use sf.metadata (Metadata API)
sf.mdapi.CustomObject.create({})
