from simple_salesforce.aio import AsyncSalesforce


class LeadRepository:
    def __init__(self, sf_client: AsyncSalesforce) -> None:
        self.sf_client = sf_client

    async def fetch(self, lead_id: str) -> dict:
        return await self.sf_client.sobject("Lead").get(lead_id)

    async def close(self, task_id: str) -> None:
        await self.sf_client.sobject("Task").update(task_id, {"Status": "Completed"})

    async def layout(self) -> dict:
        return await self.sf_client.sobject("Custom_Object__c").describe_layout("012000000000001AAA")
