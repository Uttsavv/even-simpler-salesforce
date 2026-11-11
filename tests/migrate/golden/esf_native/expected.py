"""Code already written for even-simpler-salesforce: nothing changes, nothing is marked."""

import even_simpler_salesforce as esf
from even_simpler_salesforce import Salesforce


def active_users(sf: esf.Salesforce) -> list[str]:
    page = sf.query("SELECT Id FROM User WHERE IsActive = {}", True)
    ids = [r["Id"] for r in page.records]
    while page.cursor is not None:
        page = sf.query_more(page.cursor)
        ids += [r["Id"] for r in page.records]
    return ids


def make() -> Salesforce:
    return Salesforce(auth=esf.StaticToken("token", "https://example.my.salesforce.com"))


def version(sf: Salesforce) -> str:
    return sf.api_version
