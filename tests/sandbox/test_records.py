"""Single-record smoke tests (``sf.sobject``) against a real org, on both clients.

Writes one Lead per test (LastName = the run tag), skipped with ``ESF_SANDBOX_READ_ONLY=1``.
Upsert by external id needs ``ESF_SANDBOX_EXTERNAL_ID_FIELD=<Lead|Contact>.<Field>`` naming a
writable external-id text field. Everything created is deleted by the test and, as a safety
net, by the session cleanup (query by tag).
"""

from __future__ import annotations

import uuid

import pytest

import even_simpler_salesforce as esf

from .conftest import Cleanup, Driver, new_row

pytestmark = pytest.mark.sandbox


@pytest.mark.usefixtures("writes")
def test_create_get_update_delete(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    sf = sb.client()
    lead = sf.sobject("Lead")
    rec_id = sb.run(lead.create(new_row("Lead", tag, f"-crud-{sb.kind}")))
    cleanup.add(rec_id)
    try:
        assert rec_id.startswith("00Q")
        got = sb.run(lead.get(rec_id, ["LastName", "Company"]))
        assert got["LastName"] == f"{tag}-crud-{sb.kind}"
        sb.run(lead.update(rec_id, {"Company": "esf-sandbox-updated"}))
        assert sb.run(lead.get(rec_id, ["Company"]))["Company"] == "esf-sandbox-updated"
        page = sb.run(sf.query(esf.soql("SELECT Id FROM Lead WHERE Id = {}", rec_id)))
        assert [r["Id"] for r in page.records] == [rec_id]
    finally:
        sb.run(lead.delete(rec_id))
    cleanup.discard(rec_id)
    with pytest.raises(esf.NotFound):
        sb.run(lead.get(rec_id))
    ops = {e["operation"] for e in sb.recorder.requests() if e["outcome"] == "ok"}
    assert {"sobject.create", "sobject.get", "sobject.update", "sobject.delete"} <= ops


def test_upsert_by_external_id(sb: Driver, tag: str, cleanup: Cleanup, external_id: tuple[str, str]) -> None:
    obj, field = external_id
    sf = sb.client()
    api = sf.sobject(obj)
    value = f"{tag}-{sb.kind}-{uuid.uuid4().hex[:6]}"
    created = sb.run(api.upsert(field, value, new_row(obj, tag, f"-upsert-{sb.kind}")))
    cleanup.add(created.id)
    try:
        assert created.created is True
        assert created.id
        again = sb.run(api.upsert(field, value, {"LastName": f"{tag}-upsert2-{sb.kind}"}))
        assert again.created is False
        got = sb.run(api.get_by_external_id(field, value, ["Id", "LastName"]))
        assert got["Id"][:15] == created.id[:15]
        assert got["LastName"] == f"{tag}-upsert2-{sb.kind}"
    finally:
        sb.run(api.delete(created.id))
    cleanup.discard(created.id)


@pytest.mark.usefixtures("writes")
def test_required_field_missing_is_classified(sb: Driver) -> None:
    """Creating a Lead without LastName/Company fails with REQUIRED_FIELD_MISSING, no record."""
    sf = sb.client()
    with pytest.raises(esf.RequiredFieldMissing) as ei:
        sb.run(sf.sobject("Lead").create({"FirstName": "esf-sandbox"}))
    assert ei.value.error_code == "REQUIRED_FIELD_MISSING"
    assert ei.value.status == 400
