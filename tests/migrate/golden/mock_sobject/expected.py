from unittest.mock import AsyncMock


async def test_update(sf_client):
    # esf-migrate: REVIEW[mock-sobject] sobject() mocks don't distinguish objects; prefer esf.testing.FakeSalesforce
    sf_client.sobject("Lead").update = AsyncMock(return_value=204)
    # esf-migrate: REVIEW[mock-sobject] sobject() mocks don't distinguish objects; prefer esf.testing.FakeSalesforce
    sf_client.sobject("Lead").get.return_value = {"Id": "00Q000000000001AAA"}
    await service.close_lead(sf_client, "00Q000000000001AAA")
    # esf-migrate: REVIEW[mock-sobject] sobject() mocks don't distinguish objects; prefer esf.testing.FakeSalesforce
    sf_client.sobject("Lead").update.assert_awaited_once_with("00Q000000000001AAA", {"Status": "Closed"})
    # esf-migrate: REVIEW[mock-sobject] sobject() mocks don't distinguish objects; prefer esf.testing.FakeSalesforce
    assert sf_client.sobject("Task").create.call_count == 1
