from simple_salesforce import format_soql


def handler(client, lead_id):
    client.Lead.get(lead_id)
    client.My_Obj__c.update(lead_id, {})
    settings.MAX_ROWS.get("x")
