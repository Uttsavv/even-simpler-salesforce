def handler(deps, lead_id):
    deps.admin_client.Lead.get(lead_id)
    deps.admin_client.my_obj__c.update(lead_id, {})
    cache.Store.get("key")
