from simple_salesforce import Salesforce

sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")


def follow():
    leads = sf.Lead
    leads.update("00Q000000000001AAA", {"Status": "Working"})
    leads.get("00Q000000000001AAA")


def leaks():
    contacts = sf.Contact
    register(contacts)


limit = settings.MAX_ROWS
