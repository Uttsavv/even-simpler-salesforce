# esf-migrate: TODO[unsupported] still uses simple-salesforce (AsyncSalesforce); replace before uninstalling it
from simple_salesforce.aio import AsyncSalesforce


async def describe(sf_client: AsyncSalesforce, name: str):
    return await sf_client.sobject(name).describe()


async def stored(sf_client: AsyncSalesforce, name: str):
    obj = sf_client.sobject(name)
    return await obj.get("001000000000001AAA")


def unrelated(sf_client: AsyncSalesforce):
    return getattr(sf_client, "session_id")
