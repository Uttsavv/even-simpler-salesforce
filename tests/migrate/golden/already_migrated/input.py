import even_simpler_salesforce as esf


async def fetch(sf: esf.AsyncSalesforce, lead_id: str) -> dict:
    lead = await sf.sobject("Lead").get(lead_id)
    await sf.collections.update("Lead", [{"Id": lead_id, "Status": "Working"}])
    rows = [r async for r in sf.query_iter("SELECT Id FROM Lead WHERE Email = {}", "a@x.com")]
    return {"lead": lead, "rows": rows}
