# esf-migrate: TODO[unsupported] still uses simple-salesforce (AsyncSalesforce); replace before uninstalling it
from simple_salesforce.aio import AsyncSalesforce


async def ok(sf: AsyncSalesforce, data):
    res = await sf.sobject("Lead").create(data)
    return res, res


async def inline(sf: AsyncSalesforce, data):
    return (await sf.sobject("Lead").create(data))


async def passed_on(sf: AsyncSalesforce, data):
    # esf-migrate: TODO[create-return] create() now returns the id (str), not a dict; check how the result is used
    res = await sf.sobject("Task").create(data)
    save_audit(res)
    return res["success"]


async def unused(sf: AsyncSalesforce, data):
    await sf.sobject("Task").create(data)
