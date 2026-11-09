def handler(client, lead_id):
    # esf-migrate: REVIEW[not-a-client] rewritten to sobject(); couldn't confirm this is a Salesforce client
    row = client.sobject("Lead").get(lead_id)
    # esf-migrate: REVIEW[not-a-client] rewritten to sobject(); couldn't confirm this is a Salesforce client
    cache.sobject("Store").get("key")
    return row
