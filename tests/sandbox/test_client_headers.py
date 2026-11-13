"""Client-wide headers against a real org (D30, ADR 0012), on both clients.

- ``update_mru=``: the REST API Developer Guide (*MRU Header*,
  https://developer.salesforce.com/docs/platform/api-rest/guide/headers-mru.html) says
  ``Sforce-Mru: updateMru=false`` keeps sObject Rows calls from updating the user's most
  recently used list, and that the default is ``true``. Checked with
  ``SELECT Id FROM RecentlyViewed WHERE Id = ...``. SOQL never updates the list, so the check
  itself doesn't change it.
- ``default_namespace=``: an org without a registered namespace accepts any
  ``defaultNamespace`` value and ignores it (V26), so only acceptance is checked here. The
  prefix stripping can't be tested without a namespaced org.
- ``Warning`` header: tries deprecated API versions (31.0-40.0 are deprecated, see the
  *API End-of-Life Policy*,
  https://developer.salesforce.com/docs/platform/api-rest/guide/api-rest-eol.html) and skips
  when the org sends no ``Warning`` header.

Writes one Lead per MRU test (LastName = the run tag); skipped with ``ESF_SANDBOX_READ_ONLY=1``.
"""

from __future__ import annotations

import warnings

import pytest

import even_simpler_salesforce as esf

from .conftest import Cleanup, Driver, new_row

pytestmark = pytest.mark.sandbox


def _recently_viewed(sb: Driver, sf: object, rec_id: str) -> bool:
    page = sb.run(sf.query(esf.soql("SELECT Id FROM RecentlyViewed WHERE Id = {}", rec_id)))  # type: ignore[attr-defined]
    return bool(page.records)


@pytest.mark.usefixtures("writes")
def test_update_mru_false_keeps_records_out_of_recently_viewed(
    sb: Driver, tag: str, cleanup: Cleanup
) -> None:
    sf = sb.client(update_mru=False)
    lead = sf.sobject("Lead")
    rec_id = sb.run(lead.create(new_row("Lead", tag, f"-mru-off-{sb.kind}")))
    cleanup.add(rec_id)
    try:
        sb.run(lead.get(rec_id, ["LastName"]))
        sb.run(lead.update(rec_id, {"Company": "esf-sandbox-mru"}))
        assert not _recently_viewed(sb, sf, rec_id), "update_mru=False still added the record"
        # The same record, read with updateMru=true, is added.
        on = sb.client(update_mru=True)
        sb.run(on.sobject("Lead").get(rec_id, ["LastName"]))
        assert _recently_viewed(sb, on, rec_id), "update_mru=True didn't add the record"
    finally:
        sb.run(lead.delete(rec_id))
    cleanup.discard(rec_id)
    sent = [e for e in sb.recorder.requests() if e["operation"] in ("sobject.get", "sobject.update")]
    assert sent
    assert all(e["outcome"] == "ok" for e in sent)


@pytest.mark.usefixtures("writes")
def test_no_mru_header_keeps_salesforce_default(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    sf = sb.client()
    lead = sf.sobject("Lead")
    off = sb.client(update_mru=False)
    rec_id = sb.run(off.sobject("Lead").create(new_row("Lead", tag, f"-mru-default-{sb.kind}")))
    cleanup.add(rec_id)
    try:
        assert not _recently_viewed(sb, sf, rec_id)
        sb.run(lead.get(rec_id, ["LastName"]))
        assert _recently_viewed(sb, sf, rec_id), "a get without Sforce-Mru didn't add the record"
    finally:
        sb.run(lead.delete(rec_id))
    cleanup.discard(rec_id)


@pytest.mark.usefixtures("writes")
def test_default_namespace_is_accepted(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    sf = sb.client(default_namespace="esfns", client_name="esf-sandbox")
    lead = sf.sobject("Lead")
    rec_id = sb.run(lead.create(new_row("Lead", tag, f"-ns-{sb.kind}")))
    cleanup.add(rec_id)
    try:
        got = sb.run(lead.get(rec_id, ["LastName"]))
        assert got["LastName"] == f"{tag}-ns-{sb.kind}"
        page = sb.run(sf.query(esf.soql("SELECT Id, LastName FROM Lead WHERE Id = {}", rec_id)))
        assert [r["Id"] for r in page.records] == [rec_id]
    finally:
        sb.run(lead.delete(rec_id))
    cleanup.discard(rec_id)


@pytest.mark.parametrize("version", ["31.0", "40.0"])
def test_warning_header_on_deprecated_versions(sb: Driver, version: str) -> None:
    sf = sb.client()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        sb.run(sf.request("GET", f"/services/data/v{version}/limits"))
    event = sb.recorder.requests()[-1]
    ours = [str(w.message) for w in caught if issubclass(w.category, esf.SalesforceWarning)]
    if event["sf_warning"] is None:
        assert ours == []
        pytest.skip(f"org sent no Warning header for API v{version} (2026-11-13: none seen)")
    assert ours == [event["sf_warning"]]
