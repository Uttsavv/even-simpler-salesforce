import functools

from simple_salesforce import Salesforce

sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")

fn = sf.Lead.create
update = functools.partial(sf.Contact.update, "003000000000001AAA")
sf.Lead.create = fake_create
rows = sf.query("SELECT Id FROM Lead", True)
