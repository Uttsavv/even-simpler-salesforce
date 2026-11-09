def handler(sf_client, other, lead_id):
    sf_client.sobject("Lead").get(lead_id)
    # esf-migrate: TODO[not-a-client] looks like a simple-salesforce call, but the client couldn't be confirmed; pass its name with --client-names or add ignore[not-a-client]
    other.Lead.get(lead_id)
    other.Session.get("u")
