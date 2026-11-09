from simple_salesforce import Salesforce, SalesforceLogin

session_id, instance = SalesforceLogin(username="u", password="p", security_token="t")
sf = Salesforce(instance=instance, session_id=session_id)
sf.bulk.Lead.insert([{"LastName": "Doe"}])
