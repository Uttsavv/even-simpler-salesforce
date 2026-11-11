"""Valid-looking Salesforce ids for fakes and fixture masking. No I/O."""

from __future__ import annotations

import hashlib
import string

__all__ = ["B62", "key_prefix", "make_id", "to_18"]

B62 = string.digits + string.ascii_uppercase + string.ascii_lowercase
_SUFFIX = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"

#: Key prefixes of common standard objects.
KNOWN_PREFIXES: dict[str, str] = {
    "Account": "001",
    "Note": "002",
    "Contact": "003",
    "User": "005",
    "Opportunity": "006",
    "RecordType": "012",
    "ContentVersion": "068",
    "ContentDocument": "069",
    "ContentDocumentLink": "06A",
    "Organization": "00D",
    "UserRole": "00E",
    "Group": "00G",
    "Attachment": "00P",
    "Lead": "00Q",
    "Task": "00T",
    "Event": "00U",
    "Profile": "00e",
    "CampaignMember": "00v",
    "Pricebook2": "01s",
    "Product2": "01t",
    "Asset": "02i",
    "Case": "500",
    "Campaign": "701",
    "Contract": "800",
    "Order": "801",
    # Tooling API objects
    "ApexClass": "01p",
    "ApexTrigger": "01q",
    "ApexLog": "07L",
    "AsyncApexJob": "707",
    "DebugLevel": "7dl",
    "TraceFlag": "7tf",
}


def key_prefix(sobject: str) -> str:
    """The 3-character key prefix for ``sobject`` (stable; derived for custom objects)."""
    known = KNOWN_PREFIXES.get(sobject)
    if known is not None:
        return known
    h = int.from_bytes(hashlib.sha256(sobject.encode()).digest()[:4], "big")
    return "a" + B62[h % 62] + B62[(h // 62) % 62]


def to_18(id15: str) -> str:
    """Append the case-safety checksum that turns a 15-character id into its 18-character form."""
    suffix = ""
    for i in range(3):
        chunk = id15[i * 5 : (i + 1) * 5]
        bits = sum(1 << j for j, c in enumerate(chunk) if "A" <= c <= "Z")
        suffix += _SUFFIX[bits]
    return id15 + suffix


def make_id(prefix: str, n: int, *, pod: str = "8d") -> str:
    """Build an 18-character id: ``prefix`` + pod + ``0`` + base62 counter + checksum."""
    digits = ""
    while True:
        n, r = divmod(n, 62)
        digits = B62[r] + digits
        if n == 0:
            break
    if len(digits) > 9:
        raise ValueError("id counter overflow")
    return to_18(f"{prefix}{pod}0{digits.rjust(9, '0')}")
