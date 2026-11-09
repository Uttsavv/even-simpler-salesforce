def handler(sf_client, other, lead_id):
    sf_client.Lead.get(lead_id)
    other.Lead.get(lead_id)
    other.Session.get("u")
