def handler(client, lead_id):
    # esf-migrate: TODO[not-a-client] looks like a simple-salesforce call, but the client couldn't be confirmed; pass its name with --client-names or add ignore[not-a-client]
    row = client.Lead.get(lead_id)
    cache.Store.get("key")
    return row
