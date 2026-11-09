import asyncio

from simple_salesforce.aio import AsyncSalesforce


async def both(sf: AsyncSalesforce):
    leads, contacts = await asyncio.gather(
        sf.query_all("SELECT Id FROM Lead"), sf.query_all("SELECT Id FROM Contact")
    )
    task = sf.query_all("SELECT Id FROM Lead")
    rows = (await sf.query_all("SELECT Id FROM Lead")).get("records", [])
    return leads, contacts, await task, rows
