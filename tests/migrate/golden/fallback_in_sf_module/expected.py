

def handler(client, lead_id):
    # esf-migrate: REVIEW[not-a-client] rewritten to sobject(); couldn't confirm this is a Salesforce client
    client.sobject("Lead").get(lead_id)
    # esf-migrate: REVIEW[not-a-client] rewritten to sobject(); couldn't confirm this is a Salesforce client
    client.sobject("My_Obj__c").update(lead_id, {})
    settings.MAX_ROWS.get("x")
