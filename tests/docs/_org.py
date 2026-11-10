"""A small, stateful, *synthetic* Salesforce org used to run the documentation examples.

Every response here is hand-written (``synthetic``): it mimics the shapes the library parses
(see the recorded fixtures under ``tests/fixtures/recorded`` for real ones) closely enough for
the examples in ``docs/`` and ``README.md`` to run end to end. It is not a Salesforce emulator:
SOQL support is limited to ``SELECT <fields> FROM <object>`` with simple ``=`` / ``IN`` filters
joined by ``AND``, ``LIMIT``, ``COUNT()`` and one level of child subqueries.

The org enforces a few rules so error examples have something to hit:

- ``Lead`` needs ``LastName`` and ``Company`` (``REQUIRED_FIELD_MISSING``);
- a ``Lead`` with ``Country`` but no ``Street`` fails the validation rule
  ``[ADDR_REQUIRED] Street is required when Country is set`` (``FIELD_CUSTOM_VALIDATION_EXCEPTION``);
- custom fields other than ``External_Id__c`` / ``Score__c`` don't exist (``INVALID_FIELD``);
- objects other than Lead, Account, Contact, Opportunity, User and the Files objects
  (ContentVersion, ContentDocument, ContentDocumentLink, Attachment) don't exist (``INVALID_TYPE``);
- platform events (``*__e``) are accepted and kept in :attr:`FakeOrg.events`;
- the Partner SOAP endpoint answers ``convertLead`` and ``merge``.

Files: a multipart ``POST sobjects/ContentVersion`` stores the bytes and creates the
ContentDocument (plus a ContentDocumentLink for ``FirstPublishLocationId``);
``GET .../VersionData`` and ``GET sobjects/Attachment/{id}/Body`` return the bytes.

Tooling API (``tooling/...``): ``ApexClass``, ``TraceFlag``, ``DebugLevel`` and ``ApexLog``
records; ``executeAnonymous`` fails to compile code containing ``System debug``, throws for
``1 / 0`` and, while a ``USER_DEBUG`` trace flag exists, writes an ``ApexLog`` with one
``USER_DEBUG`` line per ``System.debug('...')``.
"""

from __future__ import annotations

import gzip
import itertools
import json
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any
from urllib.parse import parse_qsl

import httpx

INSTANCE = "https://acme.my.salesforce.com"
ORG_ID = "00Dxx0000000001EAA"
USER_ID = "005xx0000000001AAA"
ID_URL = f"https://login.salesforce.com/id/{ORG_ID}/{USER_ID}"
API_MAX = 100_000

PREFIXES = {
    "Lead": "00Q",
    "Account": "001",
    "Contact": "003",
    "Opportunity": "006",
    "User": "005",
    "ContentVersion": "068",
    "ContentDocument": "069",
    "ContentDocumentLink": "06A",
    "Attachment": "00P",
}
TOOLING_PREFIXES = {"ApexClass": "01p", "TraceFlag": "7tf", "DebugLevel": "7dl", "ApexLog": "07L"}
_DEBUG = re.compile(r"System\.debug\(\s*'((?:[^'\\]|\\.)*)'\s*\)")
CUSTOM_FIELDS = {"External_Id__c", "Score__c"}
CHILD_RELATIONSHIPS = {"Contacts": ("Contact", "AccountId"), "Opportunities": ("Opportunity", "AccountId")}
PARENT_RELATIONSHIPS = {"ContentDocument": ("ContentDocument", "ContentDocumentId")}
CHILD_PAGE = 2  # tiny on purpose, so ``expand_children`` has something to follow

_SOAP = re.compile(r"^/services/Soap/u/\d+\.\d$")
_P = "{urn:partner.soap.sforce.com}"
_O = "{urn:sobject.partner.soap.sforce.com}"
_ENV = "{http://schemas.xmlsoap.org/soap/envelope/}"
_DATA = re.compile(r"^/services/data/v(?P<v>\d+\.\d)/(?P<rest>.*)$")
_FIELD_LIST = re.compile(r"^\s*SELECT\s+(?P<fields>.+?)\s+FROM\s+(?P<obj>\w+)(?P<tail>.*)$", re.I | re.S)
_EQ = re.compile(r"(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|true|false|null|-?\d+(?:\.\d+)?)", re.I)
_IN = re.compile(r"(\w+)\s+IN\s*\(([^)]*)\)", re.I)
_LIMIT = re.compile(r"\bLIMIT\s+(\d+)", re.I)
_SUBQUERY = re.compile(r"\(\s*SELECT\s+(?P<fields>[^()]+?)\s+FROM\s+(?P<rel>\w+)\s*\)", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def _literal(text: str) -> Any:
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    if low == "null":
        return None
    if text.startswith("'"):
        return re.sub(r"\\(.)", r"\1", text[1:-1])
    return float(text) if "." in text else int(text)


def _split_top(text: str) -> list[str]:
    """Split a SELECT list on commas that are not inside parentheses."""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _value(sobject: str, rec: dict[str, Any], field: str) -> Any:
    if field == "Name" and sobject in ("Lead", "Contact") and "Name" not in rec:
        return " ".join(p for p in (rec.get("FirstName"), rec.get("LastName")) if p) or None
    return rec.get(field)


class Rejected(Exception):
    def __init__(self, code: str, message: str, fields: list[str] | None = None, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.fields, self.status = code, message, fields or [], status

    def body(self) -> list[dict[str, Any]]:
        return [{"errorCode": self.code, "message": self.message, "fields": self.fields}]


class FakeOrg:
    """In-memory org; :meth:`handle` is an httpx request handler."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in PREFIXES}
        self.deleted: list[tuple[str, str, datetime]] = []
        self.locators: dict[str, list[dict[str, Any]]] = {}
        self.api_used = 12_000
        self.tokens = itertools.count(1)
        self.ids = itertools.count(1)
        self.locs = itertools.count(1)
        self.revoked: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.describe_modified = _now() - timedelta(days=3)
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.blobs: dict[str, bytes] = {}
        self.tooling: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in TOOLING_PREFIXES}
        self.apex_logs: dict[str, str] = {}  # ApexLog id -> body
        self._seed()

    # --- data ------------------------------------------------------------------------

    def new_id(self, sobject: str) -> str:
        # The counter sits in the first 15 characters, which are unique per record in Salesforce
        # (the last 3 are a case checksum).
        return f"{PREFIXES[sobject]}8d{next(self.ids):010d}AAA"

    def _store(self, sobject: str, data: dict[str, Any]) -> dict[str, Any]:
        rid = data.get("Id") or self.new_id(sobject)
        now = _iso(_now())
        rec = {k: v for k, v in data.items() if k != "attributes"}
        rec.update(Id=rid, CreatedDate=now, LastModifiedDate=now)
        self.records[sobject][rid] = rec
        return rec

    def _seed(self) -> None:
        for name in ("OrderService", "OrderServiceTest", "LeadScoring"):
            rid = f"01p8d{next(self.ids):010d}AAA"
            self.tooling["ApexClass"][rid] = {"Id": rid, "Name": name, "Status": "Active"}
        self._store("User", {"Username": "integration@acme.example", "LastName": "Integration"})
        accounts = [self._store("Account", {"Name": n}) for n in ("Acme", "Globex", "Initech")]
        for i, last in enumerate(("Doe", "Roe", "Poe")):
            self._store(
                "Contact", {"LastName": last, "AccountId": accounts[0]["Id"], "Email": f"c{i}@acme.test"}
            )
        self._store(
            "Opportunity", {"Name": "Acme renewal", "AccountId": accounts[0]["Id"], "Amount": 12500.5}
        )
        statuses = ("Open", "Working", "Closed - Not Converted")
        named = [
            ("Ada", "Lovelace", "Analytical Engines"),
            ("Grace", "Hopper", "Navy"),
            ("Alan", "Turing", "Bletchley"),
        ]
        for first, last, company in named:
            self._store(
                "Lead",
                {
                    "FirstName": first,
                    "LastName": last,
                    "Company": company,
                    "Status": "Open",
                    "Email": f"{first.lower()}@example.com",
                    "External_Id__c": f"ext-{last.lower()}",
                },
            )
        self.upload("price-list.pdf", b"%PDF-1.7 price list", accounts[0]["Id"])
        self.blobs[
            self._store("Attachment", {"Name": "contract.pdf", "ParentId": accounts[0]["Id"]})["Id"]
        ] = b"%PDF-1.4 legacy attachment"
        for i in range(447):
            self._store(
                "Lead",
                {
                    "FirstName": "Test",
                    "LastName": f"Lead {i:03d}",
                    "Company": "Example Inc",
                    "Status": statuses[i % 3],
                    "Email": f"lead{i}@example.com",
                },
            )

    def _with_attrs(self, sobject: str, rec: dict[str, Any], version: str) -> dict[str, Any]:
        url = f"/services/data/v{version}/sobjects/{sobject}/{rec['Id']}"
        named = {"Name": _value(sobject, rec, "Name")} if sobject in ("Lead", "Contact") else {}
        return {"attributes": {"type": sobject, "url": url}, **named, **rec}

    def _validate(
        self, sobject: str, data: dict[str, Any], *, creating: bool, current: dict[str, Any] | None = None
    ) -> None:
        for key in data:
            if key.endswith("__c") and key not in CUSTOM_FIELDS:
                raise Rejected("INVALID_FIELD", f"No such column '{key}' on sobject of type {sobject}", [key])
        merged = {**(current or {}), **data}
        if sobject == "Lead":
            missing = [f for f in ("LastName", "Company") if not merged.get(f)]
            if missing and (creating or any(f in data for f in missing)):
                raise Rejected(
                    "REQUIRED_FIELD_MISSING", f"Required fields are missing: [{', '.join(missing)}]", missing
                )
            if merged.get("Country") and not merged.get("Street"):
                raise Rejected(
                    "FIELD_CUSTOM_VALIDATION_EXCEPTION",
                    "[ADDR_REQUIRED] Street is required when Country is set",
                    ["Street"],
                )

    def _known(self, sobject: str) -> None:
        if sobject not in self.records:
            raise Rejected("NOT_FOUND", "The requested resource does not exist", status=404)

    def upload(
        self, path_on_client: str, data: bytes, first_publish: str | None, title: str | None = None
    ) -> str:
        ext = path_on_client.rsplit(".", 1)[1].lower() if "." in path_on_client else None
        info = {"Title": title or path_on_client, "FileExtension": ext, "ContentSize": len(data)}
        doc = self._store("ContentDocument", info)
        version = self._store(
            "ContentVersion", {**info, "PathOnClient": path_on_client, "ContentDocumentId": doc["Id"]}
        )
        doc["LatestPublishedVersionId"] = version["Id"]
        self.blobs[version["Id"]] = data
        if first_publish:
            self.link(doc["Id"], first_publish, "I", "AllUsers")
        return version["Id"]

    def link(self, doc_id: str, record_id: str, share_type: str, visibility: str | None) -> str:
        if doc_id not in self.records["ContentDocument"]:
            raise Rejected("INVALID_CROSS_REFERENCE_KEY", "invalid cross reference id", ["ContentDocumentId"])
        rec = self._store(
            "ContentDocumentLink",
            {
                "ContentDocumentId": doc_id,
                "LinkedEntityId": record_id,
                "ShareType": share_type,
                "Visibility": visibility or "AllUsers",
            },
        )
        return rec["Id"]

    def _multipart(self, request: httpx.Request) -> httpx.Response:
        """A ContentVersion upload: ``entity_content`` JSON plus a ``VersionData`` file part."""
        m = re.search(r'boundary="?([^";]+)"?', request.headers.get("content-type", ""))
        if not m:
            raise Rejected("INVALID_MULTIPART_REQUEST", "multipart body expected")
        entity: dict[str, Any] = {}
        data = b""
        for part in request.content.split(b"--" + m.group(1).encode())[1:-1]:
            head, _, body = part.removeprefix(b"\r\n").partition(b"\r\n\r\n")
            body = body.removesuffix(b"\r\n")
            if b'name="entity_content"' in head:
                entity = json.loads(body)
            elif b'name="VersionData"' in head:
                data = body
        if not entity.get("PathOnClient"):
            raise Rejected(
                "REQUIRED_FIELD_MISSING", "Required fields are missing: [PathOnClient]", ["PathOnClient"]
            )
        rid = self.upload(
            entity["PathOnClient"], data, entity.get("FirstPublishLocationId"), entity.get("Title")
        )
        return self._json({"id": rid, "success": True, "errors": []}, 201)

    def create(self, sobject: str, data: dict[str, Any]) -> dict[str, Any]:
        if sobject == "ContentDocumentLink":
            rid = self.link(
                data["ContentDocumentId"],
                data["LinkedEntityId"],
                data.get("ShareType", "V"),
                data.get("Visibility"),
            )
            return self.records[sobject][rid]
        self._validate(sobject, data, creating=True)
        return self._store(sobject, data)

    def update(self, sobject: str, rid: str, data: dict[str, Any]) -> None:
        rec = self.records[sobject].get(rid)
        if rec is None:
            raise Rejected(
                "ENTITY_IS_DELETED" if self._was_deleted(rid) else "NOT_FOUND",
                "entity is deleted"
                if self._was_deleted(rid)
                else "Provided external ID field does not exist or is not accessible",
                status=404,
            )
        clean = {k: v for k, v in data.items() if k not in ("attributes", "Id")}
        self._validate(sobject, clean, creating=False, current=rec)
        rec.update(clean, LastModifiedDate=_iso(_now()))

    def upsert(self, sobject: str, field: str, value: str, data: dict[str, Any]) -> tuple[str, bool]:
        matches = [r for r in self.records[sobject].values() if str(r.get(field)) == value]
        if len(matches) > 1:
            raise Rejected("MULTIPLE_CHOICES", "multiple matches", status=300)
        if matches:
            self.update(sobject, matches[0]["Id"], data)
            return matches[0]["Id"], False
        rec = self.create(sobject, {**data, field: value})
        return rec["Id"], True

    def delete(self, sobject: str | None, rid: str) -> None:
        for name, table in self.records.items():
            if (sobject is None or name == sobject) and rid in table:
                del table[rid]
                self.deleted.append((name, rid, _now()))
                return
        if self._was_deleted(rid):
            raise Rejected("ENTITY_IS_DELETED", "entity is deleted", status=404)
        raise Rejected("NOT_FOUND", "The requested resource does not exist", status=404)

    def _was_deleted(self, rid: str) -> bool:
        return any(d[1] == rid for d in self.deleted)

    # --- query -----------------------------------------------------------------------

    def _project(self, sobject: str, rec: dict[str, Any], fields: list[str], version: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "attributes": {
                "type": sobject,
                "url": f"/services/data/v{version}/sobjects/{sobject}/{rec['Id']}",
            }
        }
        for f in fields:
            sub = _SUBQUERY.fullmatch(f)
            if sub:
                rel = sub.group("rel")
                child_obj, fk = CHILD_RELATIONSHIPS.get(rel, (None, None))
                if child_obj is None:
                    raise Rejected("INVALID_TYPE", f"Didn't understand relationship '{rel}'")
                cfields = _split_top(sub.group("fields"))
                kids = [
                    self._project(child_obj, c, cfields, version)
                    for c in self.records[child_obj].values()
                    if c.get(fk) == rec["Id"]
                ]
                out[rel] = self._page(kids, CHILD_PAGE, version) if kids else None
            elif "." in f:
                rel, field = f.split(".", 1)
                parent_obj, fk = PARENT_RELATIONSHIPS.get(rel, ("", ""))
                parent = self.records.get(parent_obj, {}).get(str(rec.get(fk)))
                if parent is None:
                    out[rel] = None
                else:
                    url = f"/services/data/v{version}/sobjects/{parent_obj}/{parent['Id']}"
                    out.setdefault(rel, {"attributes": {"type": parent_obj, "url": url}})[field] = parent.get(
                        field
                    )
            else:
                out[f] = _value(sobject, rec, f)
        return out

    def _page(self, rows: list[dict[str, Any]], size: int, version: str) -> dict[str, Any]:
        first, rest = rows[:size], rows[size:]
        body: dict[str, Any] = {"totalSize": len(rows), "done": not rest, "records": first}
        if rest:
            loc = f"01g8d{next(self.locs):013d}"
            self.locators[f"{loc}-{size}"] = rest
            body["nextRecordsUrl"] = f"/services/data/v{version}/query/{loc}-{size}"
        return body

    def query(self, soql: str, batch: int, version: str) -> dict[str, Any]:
        masked = _SUBQUERY.sub("__SUBQUERY__", soql)  # find the top-level FROM
        m = _FIELD_LIST.match(masked)
        if not m:
            raise Rejected("MALFORMED_QUERY", "unexpected token: " + (soql.split() or ["''"])[0])
        sobject, tail = m.group("obj"), m.group("tail")
        if sobject not in self.records:
            raise Rejected("INVALID_TYPE", f"sObject type '{sobject}' is not supported.")
        rows = list(self.records[sobject].values())
        where = re.split(r"\b(?:ORDER\s+BY|LIMIT|GROUP\s+BY)\b", tail, flags=re.I)[0]
        where_wo_sub = where.replace("__SUBQUERY__", "")
        for field, lit in _EQ.findall(where_wo_sub):
            rows = [r for r in rows if r.get(field) == _literal(lit)]
        for field, items in _IN.findall(where_wo_sub):
            values = {_literal(x.strip()) for x in items.split(",") if x.strip()}
            rows = [r for r in rows if r.get(field) in values]
        lim = _LIMIT.search(tail)
        if lim:
            rows = rows[: int(lim.group(1))]
        it = iter(_SUBQUERY.finditer(soql))
        fields = [next(it).group(0) if f == "__SUBQUERY__" else f for f in _split_top(m.group("fields"))]
        if [f.upper() for f in fields] == ["COUNT()"]:
            return {"totalSize": len(rows), "done": True, "records": []}
        for f in fields:
            if not _SUBQUERY.fullmatch(f) and f.endswith("__c") and f not in CUSTOM_FIELDS:
                raise Rejected("INVALID_FIELD", f"No such column '{f}' on entity '{sobject}'.")
        projected = [self._project(sobject, r, fields, version) for r in rows]
        return self._page(projected, batch, version)

    def explain(self, soql: str) -> dict[str, Any]:
        """Synthetic ``query?explain=`` (shape from the REST API Developer Guide, v264)."""
        m = _FIELD_LIST.match(_SUBQUERY.sub("__SUBQUERY__", soql))
        sobject = m.group("obj") if m else "Lead"
        total = len(self.records.get(sobject, {}))
        indexed = "Email" in soql or "Id =" in soql
        plans: list[dict[str, Any]] = []
        if indexed:
            plans.append(
                {
                    "cardinality": 1,
                    "fields": ["Email" if "Email" in soql else "Id"],
                    "leadingOperationType": "Index",
                    "notes": [],
                    "relativeCost": 0.0,
                    "sobjectCardinality": total,
                    "sobjectType": sobject,
                }
            )
        plans.append(
            {
                "cardinality": total,
                "fields": [],
                "leadingOperationType": "TableScan",
                "notes": [
                    {
                        "description": "Not considering filter for optimization because unindexed",
                        "fields": ["IsDeleted"],
                        "tableEnumOrId": sobject,
                    }
                ],
                "relativeCost": 2.8,
                "sobjectCardinality": total,
                "sobjectType": sobject,
            }
        )
        return {"plans": plans, "sourceQuery": soql}

    def more(self, locator: str, batch: int, version: str) -> dict[str, Any]:
        rows = self.locators.pop(locator, None)
        if rows is None:
            raise Rejected("INVALID_QUERY_LOCATOR", "invalid query locator")
        body = self._page(rows, batch, version)
        return body

    # --- HTTP ------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        try:
            if path.startswith("/services/oauth2/"):
                return self._oauth(request, path.rsplit("/", 1)[1])
            if _SOAP.match(path):
                return self._soap(request)
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] in self.revoked:
                raise Rejected("INVALID_SESSION_ID", "Session expired or invalid", status=401)
            self.api_used += 1
            if path.startswith("/services/apexrest/"):
                return self._apexrest(request, path.removeprefix("/services/apexrest"))
            m = _DATA.match(path)
            if not m:
                raise Rejected("NOT_FOUND", "The requested resource does not exist", status=404)
            return self._data(request, m.group("v"), m.group("rest").rstrip("/"))
        except Rejected as exc:
            if exc.status == 300:
                return self._json(["/services/data/v68.0/sobjects/Lead/00Q8d0000000000001"], 300)
            return self._json(exc.body(), exc.status)

    def _json(self, body: Any, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
        h = {
            "content-type": "application/json;charset=UTF-8",
            "sforce-limit-info": f"api-usage={self.api_used}/{API_MAX}",
        }
        h.update(headers or {})
        content = b"" if body is None else json.dumps(body).encode()
        return httpx.Response(status, headers=h, content=content)

    def _body(self, request: httpx.Request) -> Any:
        raw = request.content
        if request.headers.get("content-encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw) if raw else None

    def _oauth(self, request: httpx.Request, what: str) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode())) if request.method == "POST" else {}
        if what == "token":
            grant = form.get("grant_type", "")
            n = next(self.tokens)
            body: dict[str, Any] = {
                "access_token": f"AT-docs-{n:04d}",
                "instance_url": INSTANCE,
                "id": ID_URL,
                "token_type": "Bearer",
                "issued_at": str(int(_now().timestamp() * 1000)),
                "signature": "c2lnbmF0dXJl",
                "scope": "api refresh_token",
            }
            if grant in ("authorization_code", "refresh_token"):
                body["refresh_token"] = f"RT-docs-{n:04d}"
            return self._json(body)
        if what == "revoke":
            self.revoked.add(form.get("token", ""))
            return httpx.Response(200)
        if what == "introspect":
            exp = int((_now() + timedelta(hours=2)).timestamp())
            return self._json(
                {
                    "active": True,
                    "scope": "api refresh_token",
                    "client_id": form.get("client_id"),
                    "token_type": "access_token",
                    "exp": exp,
                }
            )
        if what == "userinfo":
            return self._json(
                {
                    "user_id": USER_ID,
                    "organization_id": ORG_ID,
                    "name": "Integration User",
                    "preferred_username": "integration@acme.example",
                    "email": "integration@acme.example",
                }
            )
        return self._json({"error": "unsupported", "error_description": what}, 400)

    def _data(self, request: httpx.Request, version: str, rest: str) -> httpx.Response:
        method = request.method
        params = dict(request.url.params)
        parts = rest.split("/")
        batch = 2000
        opts = request.headers.get("sforce-query-options", "")
        if opts.startswith("batchSize="):
            batch = int(opts.split("=", 1)[1])

        if parts[0] in ("query", "queryAll"):
            if len(parts) == 2:
                return self._json(self.more(parts[1], batch, version))
            if "explain" in params:
                return self._json(self.explain(params["explain"]))
            return self._json(self.query(params["q"], batch, version))
        if rest == "search":
            return self._json(
                {
                    "searchRecords": [
                        self._with_attrs("Lead", r, version) for r in list(self.records["Lead"].values())[:3]
                    ]
                }
            )
        if rest == "parameterizedSearch":
            return self._json(
                {
                    "searchRecords": [
                        self._with_attrs("Lead", r, version) for r in list(self.records["Lead"].values())[:2]
                    ]
                }
            )
        if rest == "limits":
            return self._json(
                {
                    "DailyApiRequests": {"Max": API_MAX, "Remaining": API_MAX - self.api_used},
                    "DailyBulkV2QueryJobs": {"Max": 10000, "Remaining": 10000},
                    "DataStorageMB": {"Max": 1024, "Remaining": 1000},
                    "SingleEmail": {"Max": 5000, "Remaining": 4990},
                }
            )
        if rest == "limits/recordCount":
            names = params.get("sObjects", "").split(",")
            return self._json(
                {"sObjects": [{"name": n, "count": len(self.records.get(n, {}))} for n in names if n]}
            )
        if parts[0] == "tooling":
            return self._tooling(request, method, parts[1:], params, version)
        if parts[0] == "actions":
            return self._actions(request, parts[1:])
        if parts[0] == "sobjects" and len(parts) == 2 and parts[1].endswith("__e") and method == "POST":
            return self._json(self._publish(parts[1], self._body(request) or {}), 201)
        if parts[0] == "composite" and len(parts) >= 2 and parts[1] == "sobjects":
            return self._collections(request, method, parts[2:], params)
        if parts[0] == "sobjects":
            return self._sobjects(request, method, parts[1:], params, version)
        raise Rejected("NOT_FOUND", "The requested resource does not exist", status=404)

    def _sobjects(
        self, request: httpx.Request, method: str, parts: list[str], params: dict[str, str], version: str
    ) -> httpx.Response:
        if not parts or parts == [""]:
            return self._json(
                {
                    "encoding": "UTF-8",
                    "maxBatchSize": 200,
                    "sobjects": [
                        {"name": n, "label": n, "keyPrefix": p, "queryable": True}
                        for n, p in PREFIXES.items()
                    ],
                }
            )
        sobject = parts[0]
        self._known(sobject)
        if len(parts) == 1:
            if method == "POST" and sobject == "ContentVersion":
                return self._multipart(request)
            if method == "POST":
                rec = self.create(sobject, self._body(request) or {})
                return self._json({"id": rec["Id"], "success": True, "errors": []}, 201)
            return self._json(
                {"objectDescribe": {"name": sobject, "keyPrefix": PREFIXES[sobject]}, "recentItems": []}
            )
        if parts[1] == "describe":
            since = request.headers.get("if-modified-since")
            if since:
                return httpx.Response(
                    304, headers={"sforce-limit-info": f"api-usage={self.api_used}/{API_MAX}"}
                )
            fields = [
                {"name": "Id", "type": "id"},
                {"name": "Name", "type": "string"},
                {"name": "LastName", "type": "string", "length": 80},
                *({"name": f, "type": "string", "custom": True} for f in sorted(CUSTOM_FIELDS)),
            ]
            return self._json(
                {"name": sobject, "label": sobject, "createable": True, "fields": fields},
                headers={"last-modified": format_datetime(self.describe_modified, usegmt=True)},
            )
        if parts[1] in ("updated", "deleted"):
            if parts[1] == "updated":
                return self._json(
                    {"ids": list(self.records[sobject])[:5], "latestDateCovered": params.get("end")}
                )
            gone = [
                {"id": rid, "deletedDate": _iso(when)} for name, rid, when in self.deleted if name == sobject
            ]
            return self._json(
                {
                    "deletedRecords": gone,
                    "earliestDateAvailable": params.get("start"),
                    "latestDateCovered": params.get("end"),
                }
            )
        if len(parts) == 3 and (sobject, parts[2]) in (
            ("ContentVersion", "VersionData"),
            ("Attachment", "Body"),
        ):
            blob = self.blobs.get(parts[1])
            if blob is None:
                raise Rejected("NOT_FOUND", "The requested resource does not exist", status=404)
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/octet-stream",
                    "sforce-limit-info": f"api-usage={self.api_used}/{API_MAX}",
                },
                content=blob,
            )
        if len(parts) == 3:  # external id
            field, value = parts[1], parts[2]
            if method == "PATCH":
                rid, created = self.upsert(sobject, field, value, self._body(request) or {})
                return self._json(
                    {"id": rid, "success": True, "errors": [], "created": created}, 201 if created else 200
                )
            for rec in self.records[sobject].values():
                if str(rec.get(field)) == value:
                    return self._json(self._with_attrs(sobject, rec, version))
            raise Rejected(
                "NOT_FOUND", "Provided external ID field does not exist or is not accessible", status=404
            )
        rid = parts[1]
        if method == "GET":
            rec = self.records[sobject].get(rid)
            if rec is None:
                raise Rejected("NOT_FOUND", "The requested resource does not exist", status=404)
            wanted = params.get("fields")
            data = {k: _value(sobject, rec, k) for k in wanted.split(",")} if wanted else dict(rec)
            data["Id"] = rid
            return self._json(self._with_attrs(sobject, data, version))
        if method == "PATCH":
            self.update(sobject, rid, self._body(request) or {})
            return httpx.Response(204, headers={"sforce-limit-info": f"api-usage={self.api_used}/{API_MAX}"})
        if method == "DELETE":
            self.delete(sobject, rid)
            return httpx.Response(204, headers={"sforce-limit-info": f"api-usage={self.api_used}/{API_MAX}"})
        raise Rejected("METHOD_NOT_ALLOWED", "method not allowed", status=405)

    # --- collections -------------------------------------------------------------------

    def _collections(
        self, request: httpx.Request, method: str, parts: list[str], params: dict[str, str]
    ) -> httpx.Response:
        if method == "DELETE":
            ids = params.get("ids", "").split(",")
            return self._json(
                self._batch(
                    ids, params.get("allOrNone") == "true", lambda rid: (self.delete(None, rid), rid)[1]
                )
            )
        body = self._body(request) or {}
        if method == "POST" and len(parts) == 1:  # retrieve
            sobject = parts[0]
            self._known(sobject)
            out = []
            for rid in body.get("ids", []):
                rec = self.records[sobject].get(rid)
                out.append(
                    None
                    if rec is None
                    else {"attributes": {"type": sobject}, **{f: rec.get(f) for f in body.get("fields", [])}}
                )
            return self._json(out)
        rows = body.get("records", [])
        aon = bool(body.get("allOrNone"))
        if method == "POST" and rows and all(str(r["attributes"]["type"]).endswith("__e") for r in rows):
            return self._json([self._publish(r["attributes"]["type"], r) for r in rows])
        if method == "POST":
            return self._json(self._batch(rows, aon, lambda r: self.create(r["attributes"]["type"], r)["Id"]))
        if method == "PATCH" and not parts:

            def upd(r: dict[str, Any]) -> str:
                self.update(r["attributes"]["type"], r["Id"], r)
                return r["Id"]

            return self._json(self._batch(rows, aon, upd))
        if method == "PATCH" and len(parts) == 2:
            sobject, field = parts
            out_created: list[bool] = []

            def ups(r: dict[str, Any]) -> str:
                rid, created = self.upsert(
                    sobject, field, str(r[field]), {k: v for k, v in r.items() if k != field}
                )
                out_created.append(created)
                return rid

            results = self._batch(rows, aon, ups)
            it = iter(out_created)
            for res in results:
                if res["success"]:
                    res["created"] = next(it)
            return self._json(results)
        raise Rejected("NOT_FOUND", "The requested resource does not exist", status=404)

    def _batch(self, items: list[Any], all_or_none: bool, op: Any) -> list[dict[str, Any]]:
        snapshot = (
            {k: {i: dict(r) for i, r in v.items()} for k, v in self.records.items()} if all_or_none else None
        )
        results: list[dict[str, Any]] = []
        failed = False
        for item in items:
            try:
                rid = op(item)
                results.append({"id": rid, "success": True, "errors": []})
            except Rejected as exc:
                failed = True
                results.append(
                    {
                        "id": None,
                        "success": False,
                        "errors": [{"statusCode": exc.code, "message": exc.message, "fields": exc.fields}],
                    }
                )
        if all_or_none and failed and snapshot is not None:
            self.records = snapshot
            for res in results:
                if res["success"]:
                    res.update(
                        id=None,
                        success=False,
                        errors=[
                            {
                                "statusCode": "ALL_OR_NONE_OPERATION_ROLLED_BACK",
                                "message": "Record rolled back because not all records were valid "
                                "and the request was using AllOrNone header",
                                "fields": [],
                            }
                        ],
                    )
        return results

    # --- Tooling API (synthetic) -----------------------------------------------------

    def _tooling(
        self, request: httpx.Request, method: str, parts: list[str], params: dict[str, str], version: str
    ) -> httpx.Response:
        if parts == ["query"]:
            m = _FIELD_LIST.match(params["q"])
            name = m.group("obj") if m else ""
            if name not in self.tooling:
                raise Rejected("INVALID_TYPE", f"sObject type '{name}' is not supported.")
            rows = list(self.tooling[name].values())
            for field, lit in _EQ.findall(m.group("tail") if m else ""):
                if field in ("Name", "DeveloperName", "TracedEntityId", "LogUserId"):
                    rows = [r for r in rows if r.get(field) == _literal(lit)]
            if name == "ApexLog":  # the client asks ORDER BY StartTime DESC
                rows.reverse()
            recs = [{"attributes": {"type": name, "url": ""}, **r} for r in rows]
            return self._json({"size": len(recs), "totalSize": len(recs), "done": True, "records": recs})
        if parts == ["executeAnonymous"]:
            return self._json(self._anonymous(params["anonymousBody"]))
        if parts == ["runTestsAsynchronous"] and method == "POST":
            return self._json(f"7078d{next(self.ids):010d}")
        if parts == ["completions"]:
            return self._json({"publicDeclarations": {"System": {"Math": [{"name": "abs"}], "String": []}}})
        if len(parts) == 4 and parts[1] == "ApexLog" and parts[3] == "Body":
            body = self.apex_logs.get(parts[2])
            if body is None:
                raise Rejected("NOT_FOUND", "The requested resource does not exist", status=404)
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=body.encode())
        if parts[0] == "sobjects" and len(parts) >= 2 and parts[1] in self.tooling:
            store = self.tooling[parts[1]]
            if len(parts) == 2 and method == "POST":
                rid = f"{TOOLING_PREFIXES[parts[1]]}8d{next(self.ids):010d}AAA"
                store[rid] = {"Id": rid, **(self._body(request) or {})}
                return self._json({"id": rid, "success": True, "errors": []}, 201)
            rec = store.get(parts[2]) if len(parts) == 3 else None
            if rec is None:
                raise Rejected("NOT_FOUND", "The requested resource does not exist", status=404)
            if method == "DELETE":
                del store[parts[2]]
                return self._json(None, 204)
            if method == "PATCH":
                rec.update(self._body(request) or {})
                return self._json(None, 204)
            return self._json({"attributes": {"type": parts[1]}, **rec})
        raise Rejected("NOT_FOUND", "The requested resource does not exist", status=404)

    def _anonymous(self, code: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "line": -1,
            "column": -1,
            "compiled": True,
            "success": True,
            "compileProblem": None,
            "exceptionMessage": None,
            "exceptionStackTrace": None,
        }
        if "System debug" in code:
            col = code.index("System debug") + 8
            return {
                **result,
                "compiled": False,
                "success": False,
                "line": 1,
                "column": col,
                "compileProblem": "Unexpected token 'debug'.",
            }
        if "1 / 0" in code:
            result.update(
                success=False,
                line=1,
                column=1,
                exceptionMessage="System.MathException: Divide by 0",
                exceptionStackTrace="AnonymousBlock: line 1, column 1",
            )
        if any(f.get("LogType") == "USER_DEBUG" for f in self.tooling["TraceFlag"].values()):
            rid = f"07L8d{next(self.ids):010d}AAA"
            start = _now().strftime("%Y-%m-%dT%H:%M:%S.000+0000")
            self.tooling["ApexLog"][rid] = {
                "Id": rid,
                "LogUserId": USER_ID,
                "Operation": "/services/data/v68.0/tooling/executeAnonymous/",
                "StartTime": start,
            }
            lines = ["68.0 APEX_CODE,DEBUG", "Execute Anonymous: " + code.splitlines()[0]]
            lines += [f"12:00:00.0 (1)|USER_DEBUG|[1]|DEBUG|{msg}" for msg in _DEBUG.findall(code)]
            self.apex_logs[rid] = "\n".join(lines) + "\n"
        return result

    # --- org automation (synthetic) -------------------------------------------------

    def _apexrest(self, request: httpx.Request, path: str) -> httpx.Response:
        """``/orders/{id}`` answers like an order service; a closed order returns 422."""
        if path.startswith("/orders/closed"):
            return self._json({"error": "ORDER_CLOSED", "message": "Order is closed"}, 422)
        if path.startswith("/orders/"):
            body = self._body(request) if request.method in ("POST", "PUT", "PATCH") else None
            if request.method == "DELETE":
                return self._json(None, 204)
            return self._json({"id": path.rsplit("/", 1)[1], "status": "Submitted", **(body or {})})
        return self._json({"ok": True, "path": path})

    def _actions(self, request: httpx.Request, parts: list[str]) -> httpx.Response:
        """Flow ``Assign_Lead``, Apex ``LeadScoring`` and standard ``chatterPost``."""
        known = {
            ("custom", "flow", "Assign_Lead"): self._assign_lead,
            ("custom", "apex", "LeadScoring"): lambda i: {"score": 42 if i.get("leadId") else 0},
            ("standard", "chatterPost"): lambda i: {"feedItemId": "0D5xx0000000001AAA"},
        }
        key = tuple(parts)
        if request.method == "GET":
            listed = [k for k in known if k[:-1] == key]
            if listed:
                return self._json({"actions": [self._action_summary(k) for k in listed]})
            if key in known:
                return self._json(self._action_describe(key))
            raise Rejected("NOT_FOUND", "The requested resource does not exist", status=404)
        if key not in known:
            raise Rejected("NOT_FOUND", "The requested resource does not exist", status=404)
        results = []
        for item in (self._body(request) or {}).get("inputs", []):
            try:
                out = known[key](item)
                results.append(
                    {"actionName": key[-1], "errors": None, "isSuccess": True, "outputValues": out}
                )
            except Rejected as exc:
                errors = [{"statusCode": exc.code, "message": exc.message, "fields": exc.fields}]
                results.append(
                    {"actionName": key[-1], "errors": errors, "isSuccess": False, "outputValues": None}
                )
        failed = all(not r["isSuccess"] for r in results)
        return self._json(results, 400 if results and failed else 200)

    def _assign_lead(self, inputs: dict[str, Any]) -> dict[str, Any]:
        if not inputs.get("leadId"):
            raise Rejected("REQUIRED_FIELD_MISSING", "Required fields are missing: [leadId]", ["leadId"])
        if inputs.get("region") == "Mars":
            raise Rejected("FIELD_CUSTOM_VALIDATION_EXCEPTION", "[REGION_UNKNOWN] Region Mars does not exist")
        return {"assignedTo": USER_ID, "Flow__InterviewStatus": "Finished"}

    @staticmethod
    def _action_summary(key: tuple[str, ...]) -> dict[str, Any]:
        return {"name": key[-1], "label": key[-1].replace("_", " "), "type": key[-2].upper(), "url": ""}

    def _action_describe(self, key: tuple[str, ...]) -> dict[str, Any]:
        params = {
            "Assign_Lead": (
                [("leadId", "STRING", True), ("region", "STRING", False)],
                [("assignedTo", "STRING"), ("Flow__InterviewStatus", "PICKLIST")],
            ),
            "LeadScoring": ([("leadId", "ID", True)], [("score", "INTEGER")]),
            "chatterPost": (
                [("text", "TEXTAREA", True), ("subjectNameOrId", "STRING", True)],
                [("feedItemId", "ID")],
            ),
        }[key[-1]]
        return {
            **self._action_summary(key),
            "description": f"{key[-1].replace('_', ' ')} (synthetic docs org)",
            "inputs": [{"name": n, "type": t, "required": r, "maxOccurs": 1} for n, t, r in params[0]],
            "outputs": [{"name": n, "type": t, "required": False, "maxOccurs": 1} for n, t in params[1]],
        }

    # --- platform events ---------------------------------------------------------------

    def _publish(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        self.events.append((name, {k: v for k, v in data.items() if k != "attributes"}))
        return {
            "id": f"e008d{next(self.ids):010d}AAA",
            "success": True,
            "errors": [{"statusCode": "OPERATION_ENQUEUED", "message": str(uuid.uuid4()), "fields": []}],
        }

    # --- Partner SOAP (convertLead, merge) ---------------------------------------------

    def _soap_reply(self, inner: str, status: int = 200) -> httpx.Response:
        body = (
            '<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope '
            'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns="urn:partner.soap.sforce.com" '
            'xmlns:sf="urn:fault.partner.soap.sforce.com" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<soapenv:Body>{inner}</soapenv:Body></soapenv:Envelope>"
        )
        return httpx.Response(
            status, headers={"content-type": "text/xml; charset=utf-8"}, content=body.encode()
        )

    def _soap_fault(self, code: str, message: str) -> httpx.Response:
        return self._soap_reply(
            f"<soapenv:Fault><faultcode>sf:{code}</faultcode><faultstring>{code}: {message}</faultstring>"
            f'<detail><sf:UnexpectedErrorFault xsi:type="sf:UnexpectedErrorFault">'
            f"<sf:exceptionCode>{code}</sf:exceptionCode><sf:exceptionMessage>{message}</sf:exceptionMessage>"
            "</sf:UnexpectedErrorFault></detail></soapenv:Fault>",
            500,
        )

    @staticmethod
    def _soap_errors(exc: Rejected) -> str:
        fields = "".join(f"<fields>{f}</fields>" for f in exc.fields)
        return f"<errors>{fields}<message>{exc.message}</message><statusCode>{exc.code}</statusCode></errors>"

    def _soap(self, request: httpx.Request) -> httpx.Response:
        root = ET.fromstring(request.content)
        session = root.findtext(f"{_ENV}Header/{_P}SessionHeader/{_P}sessionId") or ""
        if not session or session in self.revoked:
            return self._soap_fault(
                "INVALID_SESSION_ID", "Invalid Session ID found in SessionHeader: Illegal Session"
            )
        self.api_used += 1
        body = root.find(f"{_ENV}Body")
        call = body[0] if body is not None and len(body) else None
        if call is not None and call.tag == f"{_P}convertLead":
            results = [self._convert(c) for c in call.findall(f"{_P}leadConverts")]
            return self._soap_reply(f"<convertLeadResponse>{''.join(results)}</convertLeadResponse>")
        if call is not None and call.tag == f"{_P}merge":
            results = [self._merge(r) for r in call.findall(f"{_P}request")]
            return self._soap_reply(f"<mergeResponse>{''.join(results)}</mergeResponse>")
        return self._soap_fault("INVALID_OPERATION", "unsupported call")

    def _convert(self, c: ET.Element) -> str:
        lead_id = c.findtext(f"{_P}leadId") or ""
        lead = self.records["Lead"].get(lead_id)
        if lead is None or lead.get("IsConverted"):
            exc = Rejected("INVALID_CROSS_REFERENCE_KEY", "invalid cross reference id", [])
            return f"<result>{self._soap_errors(exc)}<success>false</success></result>"
        account = c.findtext(f"{_P}accountId") or self._store("Account", {"Name": lead["Company"]})["Id"]
        contact = (
            c.findtext(f"{_P}contactId")
            or self._store(
                "Contact", {"LastName": lead["LastName"], "AccountId": account, "Email": lead.get("Email")}
            )["Id"]
        )
        opp = ""
        if c.findtext(f"{_P}doNotCreateOpportunity") != "true":
            name = c.findtext(f"{_P}opportunityName") or lead["Company"]
            opp = self._store("Opportunity", {"Name": name, "AccountId": account})["Id"]
        lead.update(Status=c.findtext(f"{_P}convertedStatus"), IsConverted=True)
        return (
            f"<result><accountId>{account}</accountId><contactId>{contact}</contactId><leadId>{lead_id}</leadId>"
            + (f"<opportunityId>{opp}</opportunityId>" if opp else '<opportunityId xsi:nil="true"/>')
            + "<success>true</success></result>"
        )

    def _merge(self, r: ET.Element) -> str:
        sobject = r.findtext(f"{_P}masterRecord/{_O}type") or ""
        master = r.findtext(f"{_P}masterRecord/{_O}Id") or ""
        dups = [e.text or "" for e in r.findall(f"{_P}recordToMergeIds")]
        table = self.records.get(sobject, {})
        if master not in table or any(d not in table for d in dups):
            exc = Rejected("ENTITY_IS_DELETED", "entity is deleted", [])
            return f'<result>{self._soap_errors(exc)}<id xsi:nil="true"/><success>false</success></result>'
        related: list[str] = []
        for recs in self.records.values():
            for rec in recs.values():
                for k, v in list(rec.items()):
                    if k != "Id" and v in dups:
                        rec[k] = master
                        related.append(rec["Id"])
        for d in dups:
            self.delete(sobject, d)
        merged = "".join(f"<mergedRecordIds>{d}</mergedRecordIds>" for d in dups)
        moved = "".join(f"<updatedRelatedIds>{i}</updatedRelatedIds>" for i in related)
        return f"<result><id>{master}</id>{merged}<success>true</success>{moved}</result>"
