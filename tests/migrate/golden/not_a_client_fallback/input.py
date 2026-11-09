def handler(client, lead_id):
    row = client.Lead.get(lead_id)
    cache.Store.get("key")
    return row
