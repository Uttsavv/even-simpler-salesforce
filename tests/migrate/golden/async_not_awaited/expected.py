import asyncio

# esf-migrate: TODO[unsupported] still uses simple-salesforce (AsyncSalesforce); replace before uninstalling it
from simple_salesforce.aio import AsyncSalesforce


async def both(sf: AsyncSalesforce):
    # esf-migrate: TODO[query-all] query_all() of the async client isn't awaited here; use [r async for r in sf.query_iter(q)] where the rows are needed
    leads, contacts = await asyncio.gather(
        sf.query_all("SELECT Id FROM Lead"), sf.query_all("SELECT Id FROM Contact")
    )
    # esf-migrate: TODO[query-all] query_all() of the async client isn't awaited here; use [r async for r in sf.query_iter(q)] where the rows are needed
    task = sf.query_all("SELECT Id FROM Lead")
    rows = [r async for r in sf.query_iter("SELECT Id FROM Lead")]
    return leads, contacts, await task, rows
