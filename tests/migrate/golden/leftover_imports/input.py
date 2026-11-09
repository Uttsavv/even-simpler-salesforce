import simple_salesforce
from simple_salesforce import SFType
from simple_salesforce.util import date_to_iso8601

sf = simple_salesforce.Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")
lead = SFType("Lead", "session", "host")
stamp = date_to_iso8601(today)
sf.Lead.get("00Q000000000001AAA")
