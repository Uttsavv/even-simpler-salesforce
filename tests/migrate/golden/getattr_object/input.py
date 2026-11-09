from simple_salesforce.aio import AsyncSalesforce


async def describe(sf_client: AsyncSalesforce, name: str):
    return await getattr(sf_client, name).describe()


async def stored(sf_client: AsyncSalesforce, name: str):
    obj = getattr(sf_client, name)
    return await obj.get("001000000000001AAA")


def unrelated(sf_client: AsyncSalesforce):
    return getattr(sf_client, "session_id")
