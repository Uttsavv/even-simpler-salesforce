"""Hints for common, fixable errors (§8.5). No I/O.

A hint is built only from data the client already has: the error, the request spec, whether
the call refreshed its token, and an already-cached describe. It never makes an API call and
never quotes record data or secrets: field names come from describe metadata, and SOQL text is
inspected but not echoed.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Mapping
from typing import Any

from .. import errors as E

__all__ = [
    "USAGE_PAGE",
    "add_hint",
    "field_suggestions",
    "hint_for",
    "missing_column",
    "unbalanced_quote",
]

USAGE_PAGE = "https://uttsavv.github.io/even-simpler-salesforce/latest/usage-and-budgets/"

# "No such column 'Emial' on entity 'Lead'." (query) and
# "No such column 'Emial' on sobject of type Lead" (DML).
_NO_SUCH_COLUMN = re.compile(
    r"No such column '(?P<field>[A-Za-z0-9_.]+)' on "
    r"(?:entity '(?P<entity>\w+)'|sobject of type (?P<type>\w+))"
)
_MAX_SUGGESTIONS = 3

#: ``sobject -> cached describe or None``; must not make an API call.
DescribeLookup = Callable[[str], "Mapping[str, Any] | None"]


def missing_column(message: str) -> tuple[str, str] | None:
    """``(field, sobject)`` from an ``INVALID_FIELD`` "No such column" message, else ``None``."""
    m = _NO_SUCH_COLUMN.search(message)
    if m is None:
        return None
    return m.group("field"), m.group("entity") or m.group("type") or ""


def field_suggestions(name: str, describe: Mapping[str, Any]) -> list[str]:
    """Field names in ``describe`` closest to ``name`` (case-insensitive, best first)."""
    fields: Any = describe.get("fields")
    names: list[str] = []
    if isinstance(fields, list):
        for f in fields:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(f, Mapping) and isinstance(f.get("name"), str):  # pyright: ignore[reportUnknownMemberType]
                names.append(f["name"])  # pyright: ignore[reportUnknownArgumentType]
    by_lower = {n.lower(): n for n in names}
    close = difflib.get_close_matches(name.lower(), list(by_lower), n=_MAX_SUGGESTIONS, cutoff=0.6)
    return [by_lower[c] for c in close if by_lower[c] != name]


def unbalanced_quote(soql: str) -> bool:
    """Whether ``soql`` has an odd number of unescaped single quotes.

    SOQL escapes a quote inside a string as ``\\'`` and a backslash as ``\\\\``
    (*Quoted String Escape Sequences*, SOQL and SOSL Reference). An odd count almost always
    means a value with an apostrophe was pasted into the query text.
    """
    count = 0
    escaped = False
    for ch in soql:
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "'":
            count += 1
    return count % 2 == 1


def _invalid_field_hint(err: E.SalesforceError, describe: DescribeLookup | None) -> str | None:
    found = missing_column(err.message)
    if found is None or describe is None:
        return None
    field, entity = found
    leaf = field.rsplit(".", 1)[-1]
    # The entity named in the message, also under the spelling the caller used for it.
    names = [entity]
    if err.sobject and err.sobject != entity and err.sobject.lower() == entity.lower():
        names.append(err.sobject)
    for sobject in names:
        cached = describe(sobject)
        if cached is None:
            continue
        close = field_suggestions(leaf, cached)
        if close:
            quoted = " or ".join(repr(n) for n in close)
            return f"did you mean {quoted}?"
        return None
    return None


def hint_for(
    err: E.SalesforceError,
    *,
    soql: str | None = None,
    refreshed: bool = False,
    describe: DescribeLookup | None = None,
) -> str | None:
    """The hint for ``err``, or ``None`` when there is nothing specific to say.

    Args:
        err: The error about to be raised.
        soql: The SOQL text of the call, if any (inspected, never quoted).
        refreshed: Whether the call already refreshed its token once.
        describe: Returns the cached describe of an sObject, or ``None`` when not cached.
    """
    code = err.error_code
    if isinstance(err, E.InvalidField):
        return _invalid_field_hint(err, describe)
    # SOAP faults carry INVALID_SESSION_ID as HTTP 500, not 401.
    if (
        isinstance(err, E.SessionExpired)
        and refreshed
        and (err.status == 401 or code == "INVALID_SESSION_ID")
    ):
        return (
            "Salesforce also rejected the token the client just refreshed. Check the connected "
            "app: it needs the 'api' OAuth scope, and its IP and session policies must allow "
            "this client."
        )
    if code == "API_DISABLED_FOR_ORG":
        return (
            "API access is off. The org edition must include API access, and the user needs "
            "the 'API Enabled' permission in a profile or permission set."
        )
    if code == "REQUEST_LIMIT_EXCEEDED":
        return (
            "The org is over its API request limit, or runs too many long requests at once. "
            f"Check sf.usage.snapshot() and set a budget: {USAGE_PAGE}"
        )
    if code == "MALFORMED_QUERY" and soql and unbalanced_quote(soql):
        return (
            "The query has an unescaped single quote, often from a value pasted into the text. "
            'Pass values as arguments instead: sf.query("... WHERE Name = {}", name).'
        )
    return None


def add_hint(
    err: E.SalesforceError,
    *,
    soql: str | None = None,
    refreshed: bool = False,
    describe: DescribeLookup | None = None,
) -> None:
    """Set ``err.hint`` unless it already has one. Never raises."""
    if err.hint is not None:
        return
    try:
        err.hint = hint_for(err, soql=soql, refreshed=refreshed, describe=describe)
    except Exception:  # a hint must never replace the real error
        err.hint = None
