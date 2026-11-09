# esf-migrate: TODO[unsupported] still uses simple-salesforce (AsyncSalesforce); replace before uninstalling it
from simple_salesforce.aio import AsyncSalesforce


class LeadRepository:
    def __init__(self, sf_client: AsyncSalesforce) -> None:
        self.sf_client = sf_client

    async def fetch(self, lead_id: str) -> dict:
        return await self.sf_client.sobject("Lead").get(lead_id)

    async def close(self, task_id: str) -> None:
        await self.sf_client.sobject("Task").update(task_id, {"Status": "Completed"})

    async def layout(self) -> dict:
        # esf-migrate: TODO[unsupported] describe_layout() has no 0.1 method; use sf.request("GET", "sobjects/Custom_Object__c/describe/layouts/<recordTypeId>")
        return await self.sf_client.Custom_Object__c.describe_layout("012000000000001AAA")
