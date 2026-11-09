from unittest.mock import AsyncMock


async def test_update(sf_client):
    sf_client.Lead.update = AsyncMock(return_value=204)
    sf_client.Lead.get.return_value = {"Id": "00Q000000000001AAA"}
    await service.close_lead(sf_client, "00Q000000000001AAA")
    sf_client.Lead.update.assert_awaited_once_with("00Q000000000001AAA", {"Status": "Closed"})
    assert sf_client.Task.create.call_count == 1
