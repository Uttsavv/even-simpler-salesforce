def handler(deps, lead_id):
    deps.admin_client.sobject("Lead").get(lead_id)
    deps.admin_client.sobject("my_obj__c").update(lead_id, {})
    cache.Store.get("key")
