from simple_salesforce.aio import AsyncSalesforce


async def ok(sf: AsyncSalesforce, data):
    res = await sf.Lead.create(data)
    return res["id"], res.get("id")


async def inline(sf: AsyncSalesforce, data):
    return (await sf.Lead.create(data))["id"]


async def passed_on(sf: AsyncSalesforce, data):
    res = await sf.Task.create(data)
    save_audit(res)
    return res["success"]


async def unused(sf: AsyncSalesforce, data):
    await sf.Task.create(data)
