from simple_salesforce import Salesforce

sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="x")

print(f"{sf.Lead.get('00Q000000000001AAA')}")
print(f'{sf.Contact.get("003000000000001AAA")}')
print(f"""{sf.Account.upsert(f'Ext__c/{1}', {})}""")
