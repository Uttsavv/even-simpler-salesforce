"""The libcst transform behind ``esf migrate`` (§9.19). Original code (AGENTS.md licensing).

One module at a time: a collector pass finds client names, imports and how results are used
(libcst scope analysis, same module only), then a transformer pass rewrites what it can prove
safe and records findings. Markers are attached to the statement that contains each finding.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import libcst as cst
import libcst.matchers as m
from libcst.metadata import (
    CodeRange,
    ExpressionContext,
    ExpressionContextProvider,
    MetadataWrapper,
    ParentNodeProvider,
    PositionProvider,
    Scope,
    ScopeProvider,
)

from ._findings import MARKER_RE, Finding, Status, marker_text

__all__ = ["Options", "transform_source"]

PER_OBJECT = frozenset(
    {
        "get",
        "get_by_custom_id",
        "create",
        "update",
        "upsert",
        "delete",
        "describe",
        "describe_layout",
        "metadata",
        "updated",
        "deleted",
    }
)
#: Lower-case client attributes that are not sObjects (ours and simple-salesforce's).
CLIENT_ATTRS = frozenset(
    {
        "sobject",
        "collections",
        "auth",
        "apex",
        "usage",
        "bulk",
        "bulk2",
        "tooling",
        "composite",
        "ui",
        "reports",
        "metadata",
        "pubsub",
        "actions",
        "leads",
        "files",
        "platform_events",
        "mdapi",
        "session",
        "headers",
        "observers",
    }
)
MOCK_ATTR_PREFIXES = ("assert_", "call_args", "await_args")
MOCK_ATTRS = frozenset({"return_value", "side_effect", "call_count", "await_count", "mock_calls", "called"})
CLIENT_TYPES = frozenset({"Salesforce", "AsyncSalesforce"})
LOGIN_CALLS = frozenset({"Salesforce", "AsyncSalesforce", "SalesforceLogin", "AsyncSalesforceLogin"})
SF_MODULES = ("simple_salesforce",)

#: simple-salesforce exception -> (esf class, exact). Exact ones are rewritten.
EXCEPTIONS: dict[str, tuple[str, bool]] = {
    "SalesforceError": ("SalesforceError", True),
    "SalesforceResourceNotFound": ("NotFound", True),
    "SalesforceMoreThanOneRecord": ("MultipleMatches", True),
    "SalesforceRefusedRequest": ("Forbidden", True),
    "SalesforceAuthenticationFailed": ("AuthError", True),
    "SalesforceExpiredSession": (
        "refresh is automatic: SessionExpired means a 401 survived a refresh, and a dead refresh "
        "token raises ReauthorizationRequired; catch esf.AuthError or both (often the handler can go)",
        False,
    ),
    "SalesforceMalformedRequest": (
        "catch esf.RequestError / esf.RecordError or a specific class "
        "(InvalidField, MalformedQuery, ValidationRuleFailed, ...)",
        False,
    ),
    "SalesforceGeneralError": ("catch esf.ServerError (5xx) or esf.SalesforceError", False),
}
INTERNALS_TODO = {
    "session_id": "the access token isn't public; make calls with sf.request(...)",
    "headers": "make calls with sf.request(...); auth headers are added for you",
    "session": "make calls with sf.request(...); the HTTP client is managed for you",
    "base_url": "use sf.instance_url to build links, sf.request(...) for calls",
    "sf_instance": "use sf.instance_url (the instance of the current token; None before the first call)",
}
#: Attributes of simple-salesforce exceptions -> the closest attribute of esf errors.
CAUGHT_ATTRS = {"content": "raw", "resource_name": "sobject", "url": "path"}
DISTINCTIVE_INTERNALS = frozenset({"sf_instance", "sf_version"})  # names only simple-salesforce uses
QUERY_RESULT_ATTRS = frozenset({"records", "total_size", "done", "cursor"})
QUERY_RESULT_MSG = "query() returns a QueryResult (records, total_size, done, cursor), not a dict"
QUERY_KEYS = {"records": "records", "totalSize": "total_size", "done": "done"}
UNSUPPORTED = {
    "bulk": "Bulk API v1 isn't supported; use sf.bulk (Bulk 2.0, planned for 0.3)",
    "bulk2": "Bulk 2.0 arrives as sf.bulk in 0.3",
    "toolingexecute": "Tooling arrives as sf.tooling in 0.3; use sf.request(...) meanwhile",
    "mdapi": "the Metadata API arrives as sf.metadata in 0.4",
}
_PLACEHOLDER = re.compile(r"(?<!\{)\{([^{}]*)\}(?!\})")
#: Common standard objects: an unconfirmed ``x.<Name>.<method>(...)`` with one of these (or a
#: custom suffix) is reported as a TODO rather than ignored.
STANDARD_OBJECTS = frozenset(
    {
        "Account",
        "Asset",
        "Attachment",
        "Campaign",
        "CampaignMember",
        "Case",
        "CaseComment",
        "Contact",
        "ContentDocument",
        "ContentDocumentLink",
        "ContentVersion",
        "Contract",
        "Event",
        "Group",
        "GroupMember",
        "Lead",
        "Note",
        "Opportunity",
        "OpportunityLineItem",
        "Order",
        "OrderItem",
        "Pricebook2",
        "PricebookEntry",
        "Product2",
        "Profile",
        "Quote",
        "RecordType",
        "ServiceAppointment",
        "Task",
        "User",
        "UserRole",
        "WorkOrder",
    }
)
CUSTOM_SUFFIXES = ("__c", "__e", "__mdt", "__x", "__b", "__kav")
#: Methods simple-salesforce has per object that the sObject facade here doesn't.
NO_EQUIVALENT = {
    "describe_layout": "describe_layout() has no 0.1 method; use "
    'sf.request("GET", "sobjects/<Obj>/describe/layouts/<recordTypeId>")',
}
CLIENT_METHODS = frozenset({"describe", "quick_search", "search", "limits", "is_sandbox"})
#: Method references whose calls the tool can't see: (rule, review message).
REF_REVIEW = {
    "create": ("create-return", "create() now returns the id (str), not a dict; check the callers"),
    "upsert": (
        "upsert-path",
        'upsert() now takes the field and value separately: upsert("Ext__c", value, data); '
        "check the callers",
    ),
}
#: Keywords of simple-salesforce per-object calls with no equivalent here.
UNKNOWN_KEYWORDS = frozenset({"headers", "raw_response"})
SUB_RULES = frozenset({"create-return", "upsert-path", "get-by-custom-id", "return-value", "not-a-client"})


NO_QUOTE = "inside nested f-strings that use both quote kinds; rewrite by hand"
MOCK_MSG = "sobject() mocks don't distinguish objects; prefer esf.testing.FakeSalesforce"
EXCEPTION_SUGGESTIONS = {
    "SalesforceMalformedRequest": "esf.RequestError",
    "SalesforceGeneralError": "esf.ServerError",
    "SalesforceExpiredSession": "(esf.SessionExpired, esf.ReauthorizationRequired)",
}


def _root_name(expr: cst.BaseExpression) -> str | None:
    while isinstance(expr, cst.Attribute):
        expr = expr.value
    return expr.value if isinstance(expr, cst.Name) else None


class _NoQuote(Exception):
    """A new string literal can't be written inside the enclosing f-string(s) (Python < 3.12)."""


def looks_like_sobject(name: str) -> bool:
    return name.endswith(CUSTOM_SUFFIXES) or name in STANDARD_OBJECTS


@dataclass(frozen=True)
class Options:
    client_names: frozenset[str] = frozenset()
    rules: frozenset[str] | None = None  # None = all
    is_test_file: bool = False


@dataclass
class _Holder:
    node: cst.CSTNode
    ignores: frozenset[str]
    findings: list[Finding] = field(default_factory=lambda: [])


def _alias(alias: cst.ImportAlias) -> str | None:
    if alias.asname is not None and isinstance(alias.asname.name, cst.Name):
        return alias.asname.name.value
    return None


def _last_segment(expr: cst.BaseExpression) -> str | None:
    if isinstance(expr, cst.Name):
        return expr.value
    if isinstance(expr, cst.Attribute):
        return expr.attr.value
    return None


def _string_value(node: cst.BaseExpression) -> str | None:
    if isinstance(node, cst.SimpleString) and "b" not in node.prefix.lower():
        v = node.evaluated_value
        return v if isinstance(v, str) else None
    return None


def _code(node: cst.CSTNode) -> str:
    return cst.Module(body=[]).code_for_node(node)


def _sobject_call(value: cst.BaseExpression, obj: cst.BaseExpression) -> cst.Call:
    return cst.Call(func=cst.Attribute(value=value, attr=cst.Name("sobject")), args=[cst.Arg(obj)])


def _str_node(text: str, quote: str = '"') -> cst.SimpleString:
    return cst.SimpleString(quote + text.replace("\\", "\\\\").replace(quote, "\\" + quote) + quote)


def _annotation_names(node: cst.BaseExpression) -> set[str]:
    names: set[str] = set()
    for n in m.findall(node, m.Name() | m.Attribute()):
        if isinstance(n, cst.Name):
            names.add(n.value)
        elif isinstance(n, cst.Attribute):
            names.add(n.attr.value)
    for s in m.findall(node, m.SimpleString()):
        v = _string_value(cast(cst.SimpleString, s))
        if v:
            names.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", v))
    return names


class _Collector(cst.CSTVisitor):
    """Module facts: imported simple-salesforce names, client names, client-returning functions."""

    def __init__(self) -> None:
        self.client_types: set[str] = set()  # local names bound to Salesforce / AsyncSalesforce
        self.login_names: set[str] = set()
        self.exception_names: dict[str, str] = {}  # local alias -> simple-salesforce name
        self.format_soql_names: set[str] = set()
        self.sf_module_aliases: set[str] = set()  # `import simple_salesforce as ss`
        self.clients: set[str] = set()  # last segments known to hold a client
        self.client_functions: set[str] = set()
        self.esf_alias: str | None = None
        self.sf_imported: set[str] = set()  # every local name imported from simple_salesforce
        self.idents: set[str] = set()  # every identifier in the module
        self.async_module = False  # imports simple_salesforce.aio / AsyncSalesforce
        self.sync_module = False  # imports the sync simple_salesforce.Salesforce
        self.async_clients: set[str] = set()
        self.sync_clients: set[str] = set()

    def _note_kind(self, seg: str, value_or_type: cst.BaseExpression) -> None:
        names = _annotation_names(value_or_type)
        if isinstance(value_or_type, cst.Await):
            names |= _annotation_names(value_or_type.expression)
        if "AsyncSalesforce" in names or (self.async_module and not self.sync_module):
            self.async_clients.add(seg)
        elif names & {"Salesforce"}:
            self.sync_clients.add(seg)

    def visit_Name(self, node: cst.Name) -> None:
        self.idents.add(node.value)

    @property
    def sf_related(self) -> bool:
        """The module imports simple-salesforce or holds a known client (§9.19.1 case 3)."""
        return bool(self.sf_imported or self.sf_module_aliases or self.clients or self.client_types)

    @property
    def esf_conflict(self) -> bool:
        """``esf`` is already a different name in this module."""
        return self.esf_alias is None and "esf" in self.idents

    def visit_Import(self, node: cst.Import) -> None:
        for alias in node.names:
            full = _code(alias.name)
            local = (
                alias.asname.name.value if alias.asname and isinstance(alias.asname.name, cst.Name) else full
            )
            if full.split(".")[0] in SF_MODULES:
                self.sf_module_aliases.add(local.split(".")[0])
                if full.endswith(".aio"):
                    self.async_module = True
            if full == "even_simpler_salesforce":
                self.esf_alias = local

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        if node.module is None or isinstance(node.names, cst.ImportStar):
            return
        mod = _code(node.module)
        if mod.split(".")[0] not in SF_MODULES:
            return
        if mod.endswith(".aio"):
            self.async_module = True
        for alias in node.names:
            name = _code(alias.name)
            local = (
                alias.asname.name.value if alias.asname and isinstance(alias.asname.name, cst.Name) else name
            )
            self.sf_imported.add(local)
            if name == "AsyncSalesforce":
                self.async_module = True
            if name == "Salesforce" and not mod.endswith(".aio"):
                self.sync_module = True
            if name in CLIENT_TYPES:
                self.client_types.add(local)
            if name in LOGIN_CALLS:
                self.login_names.add(local)
            if name in EXCEPTIONS:
                self.exception_names[local] = name
            if name == "format_soql":
                self.format_soql_names.add(local)

    def _is_client_type(self, node: cst.BaseExpression) -> bool:
        """``node`` names a simple-salesforce client type: a bare name imported from
        simple-salesforce, or ``<simple_salesforce alias>.[...].Salesforce``. A bare
        ``Salesforce`` that wasn't imported from there (``esf.Salesforce``) doesn't count."""
        attrs = {id(a.attr) for a in m.findall(node, m.Attribute()) if isinstance(a, cst.Attribute)}
        for n in m.findall(node, m.Name() | m.Attribute()):
            if isinstance(n, cst.Attribute):
                if n.attr.value in CLIENT_TYPES and _root_name(n.value) in self.sf_module_aliases:
                    return True
            elif isinstance(n, cst.Name) and id(n) not in attrs and n.value in self.client_types:
                return True
        for s in m.findall(node, m.SimpleString()):  # "AsyncSalesforce" / "ss.Salesforce"
            v = _string_value(cast(cst.SimpleString, s))
            if not v:
                continue
            try:
                parsed = cst.parse_expression(v.strip())
            except Exception:  # not an expression: not a type
                continue
            if not m.findall(parsed, m.SimpleString()) and self._is_client_type(parsed):
                return True
        return False

    def _is_client_value(self, value: cst.BaseExpression) -> bool:
        if isinstance(value, cst.Await):
            value = value.expression
        if isinstance(value, cst.Call):
            func = value.func
            if isinstance(func, cst.Name):
                return func.value in self.client_types or func.value in self.client_functions
            if isinstance(func, cst.Attribute):  # simple_salesforce.Salesforce(...), self.make_client()
                root = func.value
                if isinstance(root, cst.Name) and root.value in self.sf_module_aliases:
                    return func.attr.value in CLIENT_TYPES
                return func.attr.value in self.client_functions
            return False
        seg = _last_segment(value)
        return seg is not None and seg in self.clients

    def visit_Param(self, node: cst.Param) -> None:
        if node.annotation is not None and self._is_client_type(node.annotation.annotation):
            self.clients.add(node.name.value)
            self._note_kind(node.name.value, node.annotation.annotation)

    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        seg = _last_segment(node.target)
        if seg and (
            self._is_client_type(node.annotation.annotation)
            or (node.value is not None and self._is_client_value(node.value))
        ):
            self.clients.add(seg)
            self._note_kind(seg, node.annotation.annotation)

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        if node.returns is not None and self._is_client_type(node.returns.annotation):
            self.client_functions.add(node.name.value)

    def leave_Assign(self, original_node: cst.Assign) -> None:
        if self._is_client_value(original_node.value):
            for t in original_node.targets:
                seg = _last_segment(t.target)
                if seg:
                    self.clients.add(seg)
                    value = original_node.value
                    src = _last_segment(value) if not isinstance(value, (cst.Call, cst.Await)) else None
                    if src in self.async_clients:
                        self.async_clients.add(seg)
                    elif src in self.sync_clients:
                        self.sync_clients.add(seg)
                    else:
                        self._note_kind(seg, value)


class _Transformer(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (PositionProvider, ParentNodeProvider, ScopeProvider, ExpressionContextProvider)

    def __init__(self, facts: _Collector, opts: Options) -> None:
        super().__init__()
        self.facts = facts
        self.opts = opts
        self.holders: list[_Holder] = []
        self.done: list[Finding] = []
        self.replace: dict[cst.CSTNode, cst.BaseExpression] = {}  # original node -> replacement
        self.unwrap: set[cst.CSTNode] = set()  # subscripts replaced by their (updated) value
        self.to_attr: dict[cst.CSTNode, str] = {}  # subscripts replaced by .<attr> on their value
        # findings about a later use site: recorded when that node is visited (marker on its line)
        self.pending: dict[cst.CSTNode, tuple[str, Status, str]] = {}
        self.need_esf = False
        self.rewritten_exceptions: set[str] = set()
        self.kept_exceptions: set[str] = set()
        self.format_soql_kept = False
        self._in_import = 0
        self.fquotes: list[str] = []  # quote characters of the enclosing f-strings
        self.async_defs: list[bool] = []  # enclosing functions: async def?
        self.caught: list[str | None] = []  # names bound by enclosing `except <sf exception> as e`
        self.leftover: set[str] = set()  # simple-salesforce names still used (second pass)
        self.alias = "even_simpler_salesforce" if facts.esf_conflict else (facts.esf_alias or "esf")

    # --- helpers -----------------------------------------------------------------------------

    def enabled(self, rule: str) -> bool:
        rules = self.opts.rules
        if rules is None or rule in rules:
            return True
        # sobject-attr and its sub-rules run together: a rewrite to sobject() without them (or
        # them without it) leaves upsert paths, get_by_custom_id and results broken and unmarked
        group = SUB_RULES | {"sobject-attr"}
        return rule in group and bool(rules & group)

    def pos(self, node: cst.CSTNode) -> CodeRange:
        return cast(CodeRange, self.get_metadata(PositionProvider, node))

    def parent(self, node: cst.CSTNode) -> cst.CSTNode | None:
        return self.get_metadata(ParentNodeProvider, node, None)

    def ignored(self, rule: str) -> bool:
        return bool(self.holders) and rule in self.holders[-1].ignores

    def is_store(self, node: cst.CSTNode) -> bool:
        ctx = self.get_metadata(ExpressionContextProvider, node, None)
        return ctx in (ExpressionContext.STORE, ExpressionContext.DEL)

    def s(self, text: str) -> cst.SimpleString:
        """A new string literal that is valid inside the enclosing f-strings on Python 3.10+."""
        for quote in ('"', "'"):
            if quote not in self.fquotes:
                return _str_node(text, quote)
        raise _NoQuote

    def visit_FormattedString(self, node: cst.FormattedString) -> None:
        self.fquotes.append(node.quote[0])

    def leave_FormattedString(self, original_node: Any, updated_node: Any) -> Any:
        self.fquotes.pop()
        return updated_node

    def record(
        self,
        rule: str,
        status: Status,
        message: str,
        node: cst.CSTNode,
        after: cst.CSTNode | str | None = None,
        suggestion: str | None = None,
    ) -> bool:
        if self.ignored(rule):
            return False  # `# esf-migrate: ignore[rule]` above the statement
        r = self.pos(node)
        f = Finding(
            rule=rule,
            status=status,
            message=message,
            line=r.start.line,
            col=r.start.column,
            end_line=r.end.line,
            end_col=r.end.column,
            before=_code(node),
            after=_code(after) if isinstance(after, cst.CSTNode) else after,
            suggestion=suggestion,
        )
        if self.holders:
            holder = self.holders[-1]
            f.holder_line = self.pos(holder.node).start.line
            holder.findings.append(f)
        else:  # pragma: no cover - every expression lives in a statement or a def/class header
            self.done.append(f)
        return True

    def confirm(self, expr: cst.BaseExpression, obj: str | None = None) -> str | None:
        """``"yes"`` (confirmed), ``"fallback"`` (§9.19.1 case 3) or ``None``."""
        seg = _last_segment(expr)
        if seg is not None and (seg in self.opts.client_names or seg in self.facts.clients):
            return "yes"
        if isinstance(expr, cst.Call) and self.facts._is_client_value(expr):
            return "yes"
        if (
            not self.opts.client_names
            and self.facts.sf_related
            and obj is not None
            and obj[:1].isupper()
            and not obj.isupper()  # SOME_CONSTANT
        ):
            return "fallback"
        return None

    def confirm_distinctive(self, expr: cst.BaseExpression) -> str | None:
        """For simple-salesforce-only method/attribute names (query_all, restful, ...)."""
        c = self.confirm(expr)
        if c:
            return c
        return None if self.opts.client_names or not self.facts.sf_related else "fallback"

    def scope_uses(self, name_node: cst.Name) -> list[cst.CSTNode] | None:
        """Every load of the name assigned at ``name_node`` (single assignment only)."""
        scope: Scope | None = self.get_metadata(ScopeProvider, name_node, None)
        if scope is None:
            return None
        name = name_node.value
        if len(scope.assignments[name]) != 1:
            return None
        uses: list[cst.CSTNode] = [a.node for a in scope.accesses[name]]
        start = self.pos(name_node).start
        if any(_before(self.pos(u).start, start) for u in uses):
            return None  # used before it's assigned (another value): don't plan rewrites
        return uses

    # --- holders (statements that carry markers) ----------------------------------------------

    def visit_Module(self, node: cst.Module) -> None:
        # Comments above the first statement live in the module header, not in its leading_lines.
        self.first_stmt: cst.CSTNode | None = node.body[0] if node.body else None
        self.header: list[cst.EmptyLine] = list(node.header)

    def _lines_above(self, node: cst.CSTNode, leading: Sequence[cst.EmptyLine]) -> list[cst.EmptyLine]:
        lines = list(leading)
        if node is getattr(self, "first_stmt", None):
            lines = self.header + lines
        return lines

    def _push(self, node: cst.CSTNode) -> None:
        ignores: set[str] = set()
        for line in self._lines_above(node, getattr(node, "leading_lines", ())):
            if line.comment is not None:
                mm = MARKER_RE.search(line.comment.value)
                if mm and mm.group("kind") == "ignore":
                    ignores.add(mm.group("rule"))
        self.holders.append(_Holder(node, frozenset(ignores)))

    def _pop(self, updated: Any) -> Any:
        holder = self.holders.pop()
        self.done.extend(holder.findings)
        marks = [f for f in holder.findings if f.status != "rewritten"]
        if not marks:
            return updated
        # One marker per finding; a rerun never adds a marker that is already there.
        above = self._lines_above(holder.node, updated.leading_lines)
        existing = {line.comment.value.strip() for line in above if line.comment is not None}
        new_lines = list(updated.leading_lines)
        added: set[str] = set()
        for f in marks:
            text = marker_text(f.status, f.rule, f.message)
            if text in existing or text in added:
                continue
            added.add(text)
            new_lines.append(cst.EmptyLine(comment=cst.Comment(text)))
        for f in holder.findings:
            f.markers_added = len(added)
        return updated.with_changes(leading_lines=new_lines)

    def visit_SimpleStatementLine(self, node: cst.SimpleStatementLine) -> None:
        self._push(node)

    def leave_SimpleStatementLine(self, original_node: Any, updated_node: Any) -> Any:
        if self.leftover:
            self._mark_leftover(original_node)
        return self._pop(updated_node)

    def _mark_leftover(self, stmt: cst.SimpleStatementLine) -> None:
        for small in stmt.body:
            names: list[str] = []
            if isinstance(small, cst.ImportFrom) and small.module is not None:
                if _code(small.module).split(".")[0] in SF_MODULES and not isinstance(
                    small.names, cst.ImportStar
                ):
                    names = [_alias(a) or _code(a.name) for a in small.names]
            elif isinstance(small, cst.Import):
                for a in small.names:
                    full = _code(a.name)
                    if full.split(".")[0] in SF_MODULES:
                        names.append((_alias(a) or full).split(".")[0])
            still = sorted(n for n in names if n in self.leftover)
            if still and self.enabled("unsupported"):
                self.record(
                    "unsupported",
                    "todo",
                    f"still uses simple-salesforce ({', '.join(still)}); replace before uninstalling it",
                    small,
                )

    def visit_ExceptHandler(self, node: cst.ExceptHandler) -> None:
        self._push(node)
        self.caught.append(self._caught_name(node))

    def leave_ExceptHandler(self, original_node: Any, updated_node: Any) -> Any:
        self.caught.pop()
        return self._pop(updated_node)

    def visit_ExceptStarHandler(self, node: cst.ExceptStarHandler) -> None:
        self._push(node)
        self.caught.append(self._caught_name(node))

    def leave_ExceptStarHandler(self, original_node: Any, updated_node: Any) -> Any:
        self.caught.pop()
        return self._pop(updated_node)

    def _caught_name(self, node: cst.ExceptHandler | cst.ExceptStarHandler) -> str | None:
        """``e`` in ``except SalesforceResourceNotFound as e`` (any simple-salesforce exception)."""
        if node.type is None or node.name is None or not isinstance(node.name.name, cst.Name):
            return None
        for n in m.findall(node.type, m.Name() | m.Attribute()):
            if isinstance(n, cst.Name) and n.value in self.facts.exception_names:
                return node.name.name.value
            if (
                isinstance(n, cst.Attribute)
                and n.attr.value in EXCEPTIONS
                and _root_name(n.value) in self.facts.sf_module_aliases
            ):
                return node.name.name.value
        return None

    def visit_If(self, node: cst.If) -> None:
        self._push(node)

    def leave_If(self, original_node: Any, updated_node: Any) -> Any:
        return self._pop(updated_node)

    def visit_For(self, node: cst.For) -> None:
        self._push(node)

    def leave_For(self, original_node: Any, updated_node: Any) -> Any:
        return self._pop(updated_node)

    def visit_While(self, node: cst.While) -> None:
        self._push(node)

    def leave_While(self, original_node: Any, updated_node: Any) -> Any:
        return self._pop(updated_node)

    def visit_With(self, node: cst.With) -> None:
        self._push(node)

    def leave_With(self, original_node: Any, updated_node: Any) -> Any:
        return self._pop(updated_node)

    # decorators, default arguments, bases and `match` subjects: marker above the header
    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._push(node)
        self.async_defs.append(node.asynchronous is not None)

    def leave_FunctionDef(self, original_node: Any, updated_node: Any) -> Any:
        self.async_defs.pop()
        return self._pop(updated_node)

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self._push(node)

    def leave_ClassDef(self, original_node: Any, updated_node: Any) -> Any:
        return self._pop(updated_node)

    def visit_Match(self, node: cst.Match) -> None:
        self._push(node)

    def leave_Match(self, original_node: Any, updated_node: Any) -> Any:
        return self._pop(updated_node)

    # The body of a compound statement opens new holders, so findings there go on the inner line.

    # --- imports --------------------------------------------------------------------------------

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        self._in_import += 1

    def leave_ImportFrom(self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom) -> Any:
        self._in_import -= 1
        return updated_node  # unused names are pruned in a second step (see transform_source)

    def visit_Import(self, node: cst.Import) -> None:
        self._in_import += 1

    def leave_Import(self, original_node: cst.Import, updated_node: cst.Import) -> Any:
        self._in_import -= 1
        return updated_node

    # --- names: exceptions, login -----------------------------------------------------------------

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> Any:
        if self._in_import:
            return updated_node
        sf_name = self.facts.exception_names.get(original_node.value)
        if sf_name is None or not self.enabled("exceptions"):
            return updated_node
        parent = self.parent(original_node)
        if isinstance(parent, cst.Attribute) and parent.attr is original_node:
            return updated_node
        if isinstance(parent, (cst.FunctionDef, cst.ClassDef, cst.Param)) or self.ignored("exceptions"):
            return updated_node  # left as is; the import stays because the name is still used
        if self.is_store(original_node):
            self.record(
                "exceptions",
                "todo",
                f"{sf_name} is assigned here; replace this fallback with the esf error class",
                original_node,
            )
            return updated_node
        return self._exception(sf_name, original_node, updated_node)

    def _exception(self, sf_name: str, node: cst.BaseExpression, updated: cst.BaseExpression) -> Any:
        target, exact = EXCEPTIONS[sf_name]
        call = self.parent(node)
        if exact and isinstance(call, cst.Call) and call.func is node and call.args:
            # simple-salesforce takes (url, status, resource_name, content); esf classes take a message
            self.record(
                "exceptions",
                "todo",
                f"{sf_name}(...) takes (url, status, resource_name, content); esf.{target} takes "
                "a message and keyword arguments (status=, sobject=, path=, raw=)",
                call,
                suggestion=f'esf.{target}("message", status=...)',
            )
            return updated
        if exact:
            new = cst.Attribute(value=cst.Name(self.alias), attr=cst.Name(target))
            self.need_esf = True
            if isinstance(node, cst.Name):
                self.rewritten_exceptions.add(node.value)
            self.record("exceptions", "rewritten", f"{sf_name} → esf.{target}", node, new)
            return new
        self.record(
            "exceptions",
            "todo",
            f"{sf_name} has no 1:1 class; {target}",
            node,
            suggestion=EXCEPTION_SUGGESTIONS.get(sf_name),
        )
        return updated

    # --- attributes: internals, stored refs, mocks ----------------------------------------------

    def leave_Attribute(self, original_node: cst.Attribute, updated_node: cst.Attribute) -> Any:
        self._flush_pending(original_node)
        if original_node in self.replace:
            return self.replace[original_node]
        try:
            return self._attribute(original_node, updated_node)
        except _NoQuote:
            self.record("sobject-attr", "todo", NO_QUOTE, original_node)
            return updated_node

    def _attribute(self, original_node: cst.Attribute, updated_node: cst.Attribute) -> Any:
        attr = original_node.attr.value
        parent = self.parent(original_node)
        inner = original_node.value
        # simple_salesforce.SalesforceError / simple_salesforce.exceptions.SalesforceError
        if attr in EXCEPTIONS and _root_name(inner) in self.facts.sf_module_aliases:
            if self.enabled("exceptions") and not self.is_store(original_node):
                return self._exception(attr, original_node, updated_node)
            return updated_node
        # e.content / e.resource_name / e.url on a caught simple-salesforce exception
        if (
            attr in CAUGHT_ATTRS
            and isinstance(inner, cst.Name)
            and inner.value in self.caught
            and self.enabled("exceptions")
        ):
            self.record(
                "exceptions",
                "todo",
                f"simple-salesforce exceptions had .{attr}; esf errors have .{CAUGHT_ATTRS[attr]}",
                original_node,
                suggestion=f"{inner.value}.{CAUGHT_ATTRS[attr]}",
            )
            return updated_node
        # client internals
        if attr in INTERNALS_TODO or attr == "sf_version":
            conf = self.confirm_distinctive(inner) if attr in DISTINCTIVE_INTERNALS else self.confirm(inner)
            if not conf or not self.enabled("client-internals"):
                return updated_node
            if self.is_store(original_node):
                self.record(
                    "client-internals",
                    "todo",
                    f"{attr} is assigned here; client settings are read-only (pass them to the constructor)",
                    original_node,
                )
                return updated_node
            if attr == "sf_version":
                new = updated_node.with_changes(attr=cst.Name("api_version"))
                status: Status = "rewritten" if conf == "yes" else "needs-review"
                msg = "sf_version → api_version"
                if conf != "yes":
                    msg += " (couldn't confirm this is a Salesforce client)"
                self.record("client-internals", status, msg, original_node, new)
                return new
            self.record("client-internals", "todo", f"{attr}: {INTERNALS_TODO[attr]}", original_node)
            return updated_node
        # unsupported API areas
        if attr in UNSUPPORTED and self.confirm(inner) and self.enabled("unsupported"):
            self.record("unsupported", "todo", UNSUPPORTED[attr], original_node)
            return updated_node
        # X.Obj.method that isn't called
        if (
            attr in PER_OBJECT
            and isinstance(inner, cst.Attribute)
            and not (isinstance(parent, cst.Call) and parent.func is original_node)
        ):
            return self._object_attr(original_node, updated_node, parent)
        # test files: `mock_sf.Lead = MagicMock()`, `patch.object(sf.Lead, "create")`
        if (
            self.opts.is_test_file
            and isinstance(inner, (cst.Name, cst.Attribute))
            and looks_like_sobject(attr)
            and self.enabled("mock-sobject")
            and (self.confirm(inner, attr) or not self.opts.client_names)
        ):
            if self.is_store(original_node):
                self.record(
                    "mock-sobject",
                    "todo",
                    f"replace the mock object: configure {_code(inner)}.sobject.return_value "
                    "or use esf.testing.FakeSalesforce",
                    original_node,
                )
                return updated_node
            call = self.parent(parent) if isinstance(parent, cst.Arg) else None
            if (
                isinstance(call, cst.Call)
                and isinstance(call.func, cst.Attribute)
                and call.func.attr.value == "object"
                and call.args
                and call.args[0] is parent
            ):
                new = _sobject_call(updated_node.value, self.s(attr))
                self.record("mock-sobject", "needs-review", MOCK_MSG, original_node, new)
                return new
        return updated_node

    def _object_attr(
        self, original: cst.Attribute, updated: cst.Attribute, parent: cst.CSTNode | None
    ) -> Any:
        """``X.Obj.method`` not called: a mock in a test, a method reference, or a monkeypatch."""
        inner = cast(cst.Attribute, original.value)
        obj = inner.attr.value
        if obj in CLIENT_ATTRS or _last_segment(inner.value) in CLIENT_ATTRS | set(UNSUPPORTED):
            return updated
        conf = self.confirm(inner.value, obj)
        mock = self.opts.is_test_file and self._is_mock_use(original, parent)
        if conf is None and mock and looks_like_sobject(obj) and not self.opts.client_names:
            conf = "fallback"  # test files rarely import simple-salesforce; the mock shape is enough
        if conf is None:
            return updated
        new = updated.with_changes(value=_sobject_call(cast(cst.Attribute, updated.value).value, self.s(obj)))
        method = original.attr.value
        if method == "get_by_custom_id" and not self.is_store(original):
            new = new.with_changes(attr=cst.Name("get_by_external_id"))
        if mock:
            if not self.enabled("mock-sobject"):
                return updated
            msg = MOCK_MSG
            if original.attr.value == "create":
                msg += "; create() now returns the id (str), so mocked return values change too"
            self.record("mock-sobject", "needs-review", msg, original, new)
            return new
        if self.is_store(original):
            if self.enabled("sobject-attr"):
                self.record(
                    "sobject-attr",
                    "todo",
                    "a per-object method is replaced here; sf.sobject() returns a new object each time",
                    original,
                )
            return updated
        if conf != "yes" or not self.enabled("sobject-attr") or self.ignored("sobject-attr"):
            return updated
        if method in NO_EQUIVALENT:
            self.record("unsupported", "todo", NO_EQUIVALENT[method].replace("<Obj>", obj), original)
            return updated
        self.record("sobject-attr", "rewritten", "method reference through sobject()", original, new)
        if method == "get_by_custom_id":
            self.record("get-by-custom-id", "rewritten", "get_by_custom_id → get_by_external_id", original)
        elif method in REF_REVIEW:
            self.record(REF_REVIEW[method][0], "needs-review", REF_REVIEW[method][1], original)
        return new

    def _is_mock_use(self, node: cst.Attribute, parent: cst.CSTNode | None) -> bool:
        if isinstance(parent, cst.Attribute) and parent.value is node:
            name = parent.attr.value
            return name in MOCK_ATTRS or name.startswith(MOCK_ATTR_PREFIXES)
        return isinstance(parent, cst.AssignTarget)

    def leave_Assign(self, original_node: cst.Assign, updated_node: cst.Assign) -> Any:
        value = original_node.value
        if not (
            isinstance(value, cst.Attribute)
            and len(original_node.targets) == 1
            and self.enabled("stored-ref")
            and not self.ignored("stored-ref")
        ):
            return updated_node
        obj = value.attr.value
        if obj in CLIENT_ATTRS or obj in INTERNALS_TODO or obj in UNSUPPORTED or obj == "sf_version":
            return updated_node
        # Storing an accessor needs a confirmed client: on a guess, `x = settings.SOME_CONST` would match.
        conf = self.confirm(value.value)
        if conf != "yes":
            return updated_node
        target = original_node.targets[0].target
        if isinstance(target, cst.Name):
            uses = self.scope_uses(target)
            if uses and all(self._is_object_method_use(u) for u in uses):
                new_value = _sobject_call(cast(cst.Attribute, updated_node.value).value, self.s(obj))
                status: Status = "rewritten" if conf == "yes" else "needs-review"
                self.record(
                    "stored-ref",
                    status,
                    "object accessor stored; now sf.sobject(name)"
                    + ("" if conf == "yes" else " (couldn't confirm this is a Salesforce client)"),
                    value,
                    new_value,
                )
                return updated_node.with_changes(value=new_value)
        if obj[:1].isupper() or obj.endswith(("__c", "__e", "__mdt")):
            self.record(
                "stored-ref",
                "todo",
                f"{_code(value)} is stored and used in ways the tool can't follow; use "
                f'{_code(value.value)}.sobject("{obj}")',
                value,
                suggestion=f'{_code(value.value)}.sobject("{obj}")',
            )
        return updated_node

    def _is_object_method_use(self, node: cst.CSTNode) -> bool:
        parent = self.parent(node)
        if not (
            isinstance(parent, cst.Attribute) and parent.value is node and parent.attr.value in PER_OBJECT
        ):
            return False
        grand = self.parent(parent)
        return isinstance(grand, cst.Call) and grand.func is parent

    # --- subscripts: create-return, query-result -------------------------------------------------

    def _flush_pending(self, node: cst.CSTNode) -> None:
        item = self.pending.pop(node, None)
        if item is not None:
            self.record(item[0], item[1], item[2], node)

    def leave_Subscript(self, original_node: cst.Subscript, updated_node: cst.Subscript) -> Any:
        self._flush_pending(original_node)
        if original_node in self.replace:
            return self.replace[original_node]
        if original_node in self.unwrap:
            return updated_node.value
        if original_node in self.to_attr:
            return cst.Attribute(value=updated_node.value, attr=cst.Name(self.to_attr[original_node]))
        return updated_node

    # --- calls -----------------------------------------------------------------------------------

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> Any:
        self._flush_pending(original_node)
        if original_node in self.replace:
            return self.replace[original_node]
        try:
            return self._call(original_node, updated_node)
        except _NoQuote:
            self.record("sobject-attr", "todo", NO_QUOTE, original_node)
            return updated_node

    def _is_login(self, func: cst.BaseExpression) -> bool:
        if isinstance(func, cst.Name):
            return func.value in self.facts.login_names
        if isinstance(func, cst.Attribute):  # simple_salesforce.Salesforce(...)
            return func.attr.value in LOGIN_CALLS and _root_name(func.value) in self.facts.sf_module_aliases
        return False

    def _call(self, original_node: cst.Call, updated_node: cst.Call) -> Any:
        func = original_node.func
        # login / construction
        if self._is_login(func) and self.enabled("login"):
            self.record(
                "login",
                "todo",
                "construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, "
                "JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces "
                "session_id=/instance=; username/password login isn't supported",
                original_node,
            )
            return updated_node
        seg = _last_segment(func)
        # format_soql(...)
        if seg in self.facts.format_soql_names:
            self._format_soql_site(original_node)
            return updated_node
        # getattr(X, name) used as an object
        if (
            isinstance(func, cst.Name)
            and func.value == "getattr"
            and len(original_node.args) == 2
            and self.enabled("sobject-getattr")
            and not self.ignored("sobject-getattr")
        ):
            return self._getattr(original_node, updated_node)
        if not isinstance(func, cst.Attribute):
            return updated_node
        method = func.attr.value
        target = func.value
        if method in CLIENT_METHODS and self.enabled("client-methods") and not self.ignored("client-methods"):
            done = self._client_method(original_node, updated_node)
            if done is not None:
                return done
        if method in PER_OBJECT and isinstance(target, cst.Attribute):
            return self._object_call(original_node, updated_node)
        if method in ("query_all", "query_all_iter"):
            return self._query_all(original_node, updated_node)
        if method == "query_more" and self.enabled("query-more"):
            if self.confirm_distinctive(target):
                self.record(
                    "query-more",
                    "todo",
                    "query_more() takes a Cursor (page.cursor), not a URL; or use query_iter()",
                    original_node,
                )
            return updated_node
        if method == "restful" and self.enabled("restful") and not self.ignored("restful"):
            return self._restful(original_node, updated_node)
        if method == "apexecute" and self.enabled("apexecute") and not self.ignored("apexecute"):
            return self._apexecute(original_node, updated_node)
        if method in ("query", "query_iter") and self.confirm(target) == "yes":  # "query" is a generic name
            if self._starred(original_node, "query-result"):
                return updated_node
            self._query_result(original_node)
            updated_node = self._query_positional(original_node, updated_node)
            return self._format_soql_arg(original_node, updated_node)
        return updated_node

    def _starred(self, call: cst.Call, rule: str) -> bool:
        """``*args`` / ``**kwargs`` hide which argument is which: mark the call and skip it."""
        if not any(a.star for a in call.args):
            return False
        if self.enabled(rule) and not self.ignored(rule):
            self.record(rule, "todo", "*args / **kwargs: rewrite this call by hand", call)
        return True

    def _query_positional(self, original: cst.Call, updated: cst.Call) -> cst.Call:
        """simple-salesforce ``query(q, flag)``: the second positional argument is include_deleted.

        Here positional values after the query fill ``{}`` placeholders, so a literal query with
        as many ``{}`` as values is already in the new form and stays as it is.
        """
        method = cast(cst.Attribute, original.func).attr.value
        positional = [a for a in original.args if a.keyword is None]
        if method != "query" or len(positional) < 2 or not self.enabled("query-result"):
            return updated
        if self.ignored("query-result"):
            return updated
        first = positional[0].value
        tmpl = _string_value(first)
        if tmpl is not None and len(_PLACEHOLDER.findall(tmpl)) == len(positional) - 1:
            return updated  # query("... = {}", value): the new form
        formatted = isinstance(first, cst.Call) and _last_segment(first.func) in self.facts.format_soql_names
        no_values = formatted or (tmpl is not None and not _PLACEHOLDER.findall(tmpl))
        flag = positional[1].value
        is_bool = isinstance(flag, cst.Name) and flag.value in ("True", "False")
        if len(positional) == 2 and (no_values or is_bool):
            idx = list(original.args).index(positional[1])
            args = list(updated.args)
            args[idx] = args[idx].with_changes(keyword=cst.Name("include_deleted"), equal=_eq())
            new = updated.with_changes(args=args)
            status: Status = "rewritten" if no_values else "needs-review"
            self.record(
                "query-result",
                status,
                "query(q, flag): include_deleted is a keyword argument now",
                original,
                new,
            )
            return new
        self.record(
            "query-result",
            "todo",
            "query(q, x) passed include_deleted positionally; positional values now fill {} "
            "placeholders, so write include_deleted=x",
            original,
        )
        return updated

    def _client_method(self, original: cst.Call, updated: cst.Call) -> cst.BaseExpression | None:
        """Client calls that changed name or result (``describe()``, ``limits()``, ...).

        Returns the new call, or ``None`` when the call isn't one of them.
        """
        func = cast(cst.Attribute, original.func)
        method, target = func.attr.value, func.value
        if method == "describe":  # sf.describe(); sf.Lead.describe() is per-object
            if original.args or self.confirm(target) != "yes":
                return None
            new = updated.with_changes(
                func=cast(cst.Attribute, updated.func).with_changes(attr=cst.Name("describe_global"))
            )
            self.record("client-methods", "rewritten", "describe() → describe_global()", original, new)
            return new
        if self.confirm_distinctive(target) is None:
            return None
        if method == "quick_search":
            self.record(
                "client-methods",
                "todo",
                "quick_search(term) → parameterized_search(term), which returns the list of records",
                original,
                suggestion=_code(updated.with_changes(func=func.with_changes(attr=cst.Name("parameterized_search")))),
            )
        elif method == "is_sandbox":
            self.record(
                "client-methods",
                "todo",
                "is_sandbox() isn't provided; query SELECT IsSandbox FROM Organization",
                original,
            )
        elif method == "search":
            if any(self._key_of(u) == "searchRecords" for u in self._result_uses(original)):
                self.record(
                    "client-methods",
                    "todo",
                    'search() now returns the list of records; drop ["searchRecords"]',
                    original,
                )
        elif method == "limits":
            for u in self._result_uses(original):
                outer = self.parent(u)
                if self._key_of(u) is not None and self._key_of(outer) in ("Max", "Remaining"):
                    self.record(
                        "client-methods",
                        "todo",
                        'limits() returns dict[str, Limit]: ["X"]["Remaining"] → ["X"].remaining, '
                        '["Max"] → .max',
                        original,
                    )
                    break
        return updated

    def _result_uses(self, call: cst.Call) -> list[cst.CSTNode]:
        """The call (or its ``await``) if used directly, else the loads of the one name it is
        assigned to."""
        node, parent = self._call_context(call)
        if (
            isinstance(parent, cst.Assign)
            and len(parent.targets) == 1
            and isinstance(parent.targets[0].target, cst.Name)
        ):
            return self.scope_uses(parent.targets[0].target) or []
        return [node]

    def _key_of(self, node: cst.CSTNode | None) -> str | None:
        """``"k"`` when ``node`` is used as ``node["k"]`` or ``node.get("k"...)``."""
        if node is None:
            return None
        p = self.parent(node)
        if isinstance(p, cst.Subscript) and p.value is node:
            return _string_value(_slice(p) or cst.Name("x"))
        if isinstance(p, cst.Attribute) and p.value is node and p.attr.value == "get":
            call = self.parent(p)
            if isinstance(call, cst.Call) and call.args:
                return _string_value(call.args[0].value)
        return None

    def _has_format_soql(self, call: cst.Call) -> bool:
        first = call.args[0].value if call.args else None
        return isinstance(first, cst.Call) and _last_segment(first.func) in self.facts.format_soql_names

    def _format_soql_site(self, node: cst.Call) -> None:
        """A ``format_soql(...)`` call: converted by the enclosing query call, or a TODO."""
        if not self.enabled("format-soql"):
            return
        parent = self.parent(node)
        outer = self.parent(parent) if isinstance(parent, cst.Arg) else None
        if isinstance(outer, cst.Call) and self._is_query_call(outer) and outer.args[0] is parent:
            func = cast(cst.Attribute, outer.func)
            method = func.attr.value
            handled = (
                self.confirm(func.value) == "yes"
                if method in ("query", "query_iter")
                else self.confirm_distinctive(func.value) is not None
            )
            if handled:
                return  # the query call converts it
            self.format_soql_kept = True
            self.record(
                "format-soql",
                "todo",
                "couldn't confirm the client of this query; pass the values to the query method: "
                'sf.query("... = {}", value)',
                node,
            )
            return
        self.format_soql_kept = True
        self.record(
            "format-soql",
            "todo",
            'pass the values to the query method instead: sf.query("... = {}", value)',
            node,
        )

    # sobject-attr and friends

    def _object_call(self, original: cst.Call, updated: cst.Call) -> Any:
        func = cast(cst.Attribute, original.func)
        inner = cast(cst.Attribute, func.value)
        obj = inner.attr.value
        method = func.attr.value
        if obj in CLIENT_ATTRS or _last_segment(inner.value) in CLIENT_ATTRS | set(UNSUPPORTED):
            return updated  # sf.collections.update(...), sf.bulk.Lead.insert(...)
        conf = self.confirm(inner.value, obj)
        if conf is None:
            if looks_like_sobject(obj) and self.enabled("not-a-client"):
                self.record(
                    "not-a-client",
                    "todo",
                    "looks like a simple-salesforce call, but the client couldn't be confirmed; "
                    "pass its name with --client-names or add ignore[not-a-client]",
                    original,
                    suggestion=f'{_code(inner.value)}.sobject("{obj}").{method}(...)',
                )
            return updated
        rule = "sobject-attr" if conf == "yes" else "not-a-client"
        if not self.enabled("sobject-attr") or self.ignored(rule) or self.ignored("sobject-attr"):
            return updated
        if method in NO_EQUIVALENT:
            self.record("unsupported", "todo", NO_EQUIVALENT[method].replace("<Obj>", obj), original)
            return updated
        ufunc = cast(cst.Attribute, updated.func)
        new_func = ufunc.with_changes(
            value=_sobject_call(cast(cst.Attribute, ufunc.value).value, self.s(obj))
        )
        new = updated.with_changes(func=new_func)
        if (
            method == "get_by_custom_id"
            and self.enabled("get-by-custom-id")
            and not self.ignored("get-by-custom-id")
        ):
            new = new.with_changes(func=new_func.with_changes(attr=cst.Name("get_by_external_id")))
            self.record("get-by-custom-id", "rewritten", "get_by_custom_id → get_by_external_id", original)
        if method == "upsert" and self.enabled("upsert-path"):
            new = self._upsert_path(original, new)
        status: Status = "rewritten" if conf == "yes" else "needs-review"
        msg = "object accessed through sobject()"
        if conf != "yes":
            msg = "rewritten to sobject(); couldn't confirm this is a Salesforce client"
        self.record(rule, status, msg, original, new)
        unknown = sorted(
            a.keyword.value
            for a in original.args
            if a.keyword is not None and a.keyword.value in UNKNOWN_KEYWORDS
        )
        if unknown:
            self.record(
                "unsupported",
                "todo",
                f"{', '.join(unknown)}= has no equivalent here; drop it or use sf.request(...)",
                original,
            )
        if method == "create" and self.enabled("create-return"):
            self._create_return(original)
        elif method in ("update", "upsert", "delete") and self.enabled("return-value"):
            self._return_value(original, method)
        return new

    def _upsert_path(self, original: cst.Call, new: cst.Call) -> cst.Call:
        args = list(new.args)
        if self.ignored("upsert-path") or not args:
            return new
        if args[0].keyword is not None:
            self.record(
                "upsert-path",
                "todo",
                'upsert() takes the external id field and value positionally: upsert("Ext__c", value, data)',
                original.args[0].value,
            )
            return new
        first = args[0].value
        orig_first = original.args[0].value  # positions live on original nodes
        text = _string_value(first)
        if text is not None and "/" in text:
            fld, _, val = text.partition("/")
            split = [
                args[0].with_changes(value=self.s(fld)),
                cst.Arg(self.s(val), comma=args[0].comma),
            ]
            self.record("upsert-path", "rewritten", '"Field/value" → "Field", "value"', orig_first)
            return new.with_changes(args=[*split, *args[1:]])
        if isinstance(first, cst.FormattedString):
            parts = list(first.parts)
            for i, part in enumerate(parts):
                if isinstance(part, cst.FormattedStringText) and "/" in part.value:
                    left, _, right = part.value.partition("/")
                    head = [*parts[:i], cst.FormattedStringText(left)] if left or i else parts[:i]
                    tail = [cst.FormattedStringText(right)] if right else []
                    tail += parts[i + 1 :]
                    head_node: cst.BaseExpression = first.with_changes(parts=head)
                    if all(isinstance(p, cst.FormattedStringText) for p in head):
                        head_node = self.s("".join(cast(cst.FormattedStringText, p).value for p in head))
                    if (
                        len(tail) == 1
                        and isinstance(tail[0], cst.FormattedStringExpression)
                        and not (tail[0].conversion or tail[0].format_spec)
                    ):
                        tail_node: cst.BaseExpression = cst.Call(
                            func=cst.Name("str"), args=[cst.Arg(tail[0].expression)]
                        )
                    else:
                        tail_node = first.with_changes(parts=tail)
                    split = [args[0].with_changes(value=head_node), cst.Arg(tail_node, comma=args[0].comma)]
                    self.record("upsert-path", "rewritten", 'f"Field/{value}" → "Field", value', orig_first)
                    return new.with_changes(args=[*split, *args[1:]])
        self.record(
            "upsert-path",
            "todo",
            'upsert() now takes the external id field and value separately: upsert("Ext__c", value, data)',
            orig_first,
        )
        return new

    def _call_context(self, call: cst.Call) -> tuple[cst.CSTNode, cst.CSTNode | None]:
        """(the call or its ``await``, that node's parent)."""
        node: cst.CSTNode = call
        parent = self.parent(call)
        if isinstance(parent, cst.Await):
            node, parent = parent, self.parent(parent)
        return node, parent

    def _create_return(self, call: cst.Call) -> None:
        if self.ignored("create-return"):
            return
        _, parent = self._call_context(call)
        if isinstance(parent, cst.Expr):
            return  # result unused
        if isinstance(parent, cst.Subscript) and _string_value(_slice(parent) or cst.Name("x")) == "id":
            self.unwrap.add(parent)
            self.record("create-return", "rewritten", 'create() returns the id: ["id"] dropped', parent)
            return
        if (
            isinstance(parent, cst.Assign)
            and len(parent.targets) == 1
            and isinstance(parent.targets[0].target, cst.Name)
        ):
            name = parent.targets[0].target
            uses = self.scope_uses(name)
            id_uses = [self._id_use(u) for u in uses] if uses else None
            if uses and id_uses is not None and all(x is not None for x in id_uses):
                for x in id_uses:
                    assert x is not None
                    self.replace[x] = cst.Name(name.value)
                    self.record("create-return", "rewritten", f'{name.value}["id"] → {name.value}', x)
                self.record(
                    "create-return",
                    "rewritten",
                    f'create() returns the id: {name.value}["id"] → {name.value}',
                    call,
                )
                return
        self.record(
            "create-return",
            "todo",
            "create() now returns the id (str), not a dict; check how the result is used",
            call,
        )

    def _id_use(self, node: cst.CSTNode) -> cst.CSTNode | None:
        parent = self.parent(node)
        if (
            isinstance(parent, cst.Subscript)
            and parent.value is node
            and _string_value(_slice(parent) or cst.Name("x")) == "id"
        ):
            return parent
        if isinstance(parent, cst.Attribute) and parent.value is node and parent.attr.value == "get":
            call = self.parent(parent)
            if (
                isinstance(call, cst.Call)
                and len(call.args) == 1
                and _string_value(call.args[0].value) == "id"
            ):
                return call
        return None

    def _records_use(self, node: cst.CSTNode) -> cst.CSTNode | None:
        """``r["records"]`` or ``r.get("records"[, default])`` → that node; else ``None``."""
        parent = self.parent(node)
        if isinstance(parent, cst.Subscript) and parent.value is node:
            return parent if _string_value(_slice(parent) or cst.Name("x")) == "records" else None
        if isinstance(parent, cst.Attribute) and parent.value is node and parent.attr.value == "get":
            call = self.parent(parent)
            if (
                isinstance(call, cst.Call)
                and 1 <= len(call.args) <= 2
                and _string_value(call.args[0].value) == "records"
            ):
                return call
        return None

    def _return_value(self, call: cst.Call, method: str) -> None:
        _, parent = self._call_context(call)
        if isinstance(parent, cst.Expr):
            return
        result = {"update": "None", "delete": "None", "upsert": "an UpsertResult(id, created)"}[method]
        self.record(
            "return-value",
            "needs-review",
            f"{method}() returned an HTTP status code; it now returns {result}",
            call,
        )

    def _getattr(self, original: cst.Call, updated: cst.Call) -> Any:
        target = original.args[0].value
        if self.confirm(target) != "yes":
            return updated
        parent = self.parent(original)
        used_as_object = isinstance(parent, cst.Attribute) and parent.attr.value in PER_OBJECT
        if isinstance(parent, cst.Assign) and isinstance(parent.targets[0].target, cst.Name):
            uses = self.scope_uses(parent.targets[0].target)
            used_as_object = bool(uses) and all(self._is_object_method_use(u) for u in uses or ())
        if not used_as_object:
            return updated
        new = _sobject_call(updated.args[0].value, updated.args[1].value)
        self.record("sobject-getattr", "rewritten", "getattr(sf, name) → sf.sobject(name)", original, new)
        return new

    # queries

    def _is_query_call(self, node: cst.CSTNode | None) -> bool:
        return (
            isinstance(node, cst.Call)
            and isinstance(node.func, cst.Attribute)
            and node.func.attr.value in ("query", "query_all", "query_all_iter", "query_iter")
        )

    def _format_soql_arg(self, original: cst.Call, call: cst.Call) -> cst.Call:
        """``X.query(format_soql("... {} ...", a))`` → ``X.query("... {} ...", a)``."""
        if not call.args or not self.enabled("format-soql") or self.ignored("format-soql"):
            return call
        inner = call.args[0].value
        orig_inner = original.args[0].value
        if not (isinstance(inner, cst.Call) and _last_segment(inner.func) in self.facts.format_soql_names):
            return call
        tmpl_node = inner.args[0].value if inner.args else None
        tmpl = _string_value(tmpl_node) if tmpl_node is not None else None
        rest = inner.args[1:]
        ok = (
            tmpl is not None
            and all(a.keyword is None and not a.star for a in rest)
            and all(p == "" for p in _PLACEHOLDER.findall(tmpl))
            and len(_PLACEHOLDER.findall(tmpl)) == len(rest)
        )
        if not ok:
            self.format_soql_kept = True
            self.record(
                "format-soql",
                "todo",
                "only plain positional {} placeholders convert; use esf.soql() for {name} / {:like}",
                orig_inner,
            )
            return call
        assert tmpl_node is not None
        new_args = [
            cst.Arg(
                tmpl_node,
                comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))
                if rest or len(call.args) > 1
                else cst.MaybeSentinel.DEFAULT,
            )
        ]
        for i, a in enumerate(rest):
            last = i == len(rest) - 1
            new_args.append(
                a.with_changes(comma=a.comma if not last or len(call.args) > 1 else cst.MaybeSentinel.DEFAULT)
            )
        if len(call.args) > 1 and rest:
            new_args[-1] = new_args[-1].with_changes(
                comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))
            )
        new = call.with_changes(args=[*new_args, *call.args[1:]])
        self.record("format-soql", "rewritten", "format_soql(...) → values in the query method", orig_inner)
        return new

    def _query_all(self, original: cst.Call, updated: cst.Call) -> Any:
        func = cast(cst.Attribute, original.func)
        if not self.enabled("query-all") or self.ignored("query-all"):
            return updated
        conf = self.confirm_distinctive(func.value)
        if conf is None or self._starred(original, "query-all"):
            return updated
        args = list(updated.args)
        if len(args) >= 2 and args[1].keyword is None:  # include_deleted is the second positional
            args[1] = args[1].with_changes(keyword=cst.Name("include_deleted"), equal=_eq())
        # values from format_soql(...) go right after the query, before include_deleted=
        call = self._format_soql_arg(original, updated.with_changes(args=args))
        ufunc = cast(cst.Attribute, call.func)
        iter_call = call.with_changes(func=ufunc.with_changes(attr=cst.Name("query_iter")))
        status: Status = "rewritten" if conf == "yes" else "needs-review"
        suffix = "" if conf == "yes" else " (couldn't confirm this is a Salesforce client)"
        if func.attr.value == "query_all_iter":
            self.record("query-all", status, "query_all_iter → query_iter" + suffix, original, iter_call)
            return iter_call
        node, parent = self._call_context(original)
        is_async = node is not original
        seg = _last_segment(func.value)
        unknown_kind = seg not in self.facts.async_clients and seg not in self.facts.sync_clients
        async_client = seg in self.facts.async_clients or (
            unknown_kind
            and (
                (self.facts.async_module and not self.facts.sync_module)
                or bool(self.async_defs and self.async_defs[-1])  # inside an async def: can't tell
            )
        )
        if not is_async and async_client:
            # an un-awaited async query_all (gathered, stored as a task): can't become a list here
            self.record(
                "query-all",
                "todo",
                "query_all() of the async client isn't awaited here; use "
                "[r async for r in sf.query_iter(q)] where the rows are needed",
                original,
                suggestion=f"[r async for r in {_code(iter_call)}]",
            )
            return updated
        listed: cst.BaseExpression = (
            cst.ListComp(
                elt=cst.Name("r"),
                for_in=cst.CompFor(target=cst.Name("r"), iter=iter_call, asynchronous=cst.Asynchronous()),
            )
            if is_async
            else cst.Call(func=cst.Name("list"), args=[cst.Arg(iter_call)])
        )
        records = self._records_use(node)  # query_all(q)["records"] / .get("records", [])
        if records is not None:
            self.replace[records] = listed
            self.record(
                "query-all", status, 'query_all(q)["records"] → list of rows' + suffix, records, listed
            )
            return updated  # the subscript / .get() call is replaced as a whole
        if is_async:
            self.replace[node] = listed
        if (
            isinstance(parent, cst.Assign)
            and len(parent.targets) == 1
            and isinstance(parent.targets[0].target, cst.Name)
        ):
            name = parent.targets[0].target
            uses = self.scope_uses(name)
            rec_uses = [self._records_use(u) for u in uses] if uses else None
            if uses and rec_uses is not None and all(x is not None for x in rec_uses):
                for x in rec_uses:
                    assert x is not None
                    self.replace[x] = cst.Name(name.value)
                    self.record("query-all", "rewritten", f'{name.value}["records"] → {name.value}', x)
                self.record(
                    "query-all",
                    status,
                    f'query_all() → list of rows; {name.value}["records"] → {name.value}' + suffix,
                    original,
                    listed,
                )
                return updated if is_async else listed
        self.record(
            "query-all",
            "needs-review",
            "query_all() returned a dict ({'records': [...], 'totalSize': ...}); this is now the list "
            "of rows",
            original,
            listed,
        )
        return updated if is_async else listed

    def _query_result(self, call: cst.Call) -> None:
        """``X.query(q)["records"]`` → ``.records`` (QueryResult isn't a dict).

        A ``query()`` result used in any way the tool can't follow (stored twice, returned,
        passed on, iterated) gets a TODO on the call.
        """
        if not self.enabled("query-result") or self.ignored("query-result"):
            return
        node, parent = self._call_context(call)
        subs: list[cst.Subscript] = []
        proven = True
        if isinstance(parent, cst.Expr):
            pass  # result unused
        elif isinstance(parent, (cst.Subscript, cst.Attribute)) and parent.value is node:
            proven = self._query_use(node, subs)
        elif (
            isinstance(parent, cst.Assign)
            and len(parent.targets) == 1
            and isinstance(parent.targets[0].target, cst.Name)
        ):
            uses = self.scope_uses(parent.targets[0].target)
            proven = uses is not None
            for u in uses or ():
                proven = self._query_use(u, subs) and proven
        else:
            proven = False
        for sub in subs:
            key = _string_value(_slice(sub) or cst.Name("x"))
            if key in QUERY_KEYS:
                self.to_attr[sub] = QUERY_KEYS[key]
                new = cst.Attribute(value=sub.value, attr=cst.Name(QUERY_KEYS[key]))
                self.record("query-result", "rewritten", f'["{key}"] → .{QUERY_KEYS[key]}', sub, new)
            else:
                self.pending[sub] = ("query-result", "todo", QUERY_RESULT_MSG)
        if not proven and cast(cst.Attribute, call.func).attr.value == "query":
            self.record("query-result", "todo", QUERY_RESULT_MSG + "; check how the result is used", call)

    def _query_use(self, node: cst.CSTNode, subs: list[cst.Subscript]) -> bool:
        """Handle one use of a query() result; ``False`` if the tool can't follow it."""
        p = self.parent(node)
        if isinstance(p, cst.Subscript) and p.value is node:
            subs.append(p)
            return True
        if isinstance(p, cst.Attribute) and p.value is node:
            if p.attr.value == "get":
                self._query_result_get(p)
                return True
            return p.attr.value in QUERY_RESULT_ATTRS  # already QueryResult attributes
        return False

    def _query_result_get(self, attr: cst.Attribute) -> None:
        """``r.get("records", [])`` → ``r.records`` (the attribute always exists)."""
        call = self.parent(attr)
        key = (
            _string_value(call.args[0].value)
            if isinstance(call, cst.Call) and call.func is attr and 1 <= len(call.args) <= 2
            else None
        )
        if isinstance(call, cst.Call) and key in QUERY_KEYS:
            new = cst.Attribute(value=attr.value, attr=cst.Name(QUERY_KEYS[key]))
            self.replace[call] = new
            self.record("query-result", "rewritten", f'.get("{key}") → .{QUERY_KEYS[key]}', call, new)
            return
        self.pending[attr] = ("query-result", "todo", QUERY_RESULT_MSG)

    # raw requests

    def _restful(self, original: cst.Call, updated: cst.Call) -> Any:
        func = cast(cst.Attribute, original.func)
        conf = self.confirm_distinctive(func.value)
        if conf is None or self._starred(original, "restful"):
            return updated
        args = list(updated.args)
        positional = [a for a in args if a.keyword is None]
        kw = {a.keyword.value: a for a in args if a.keyword is not None}
        if not positional or len(positional) > 3 or set(kw) - {"method", "params", "json"}:
            self.record("restful", "todo", "use sf.request(method, path, params=..., json=...)", original)
            return updated
        path = positional[0].value
        params = kw.get("params") or (positional[1] if len(positional) > 1 else None)
        method = (
            kw["method"].value
            if "method" in kw
            else (positional[2].value if len(positional) > 2 else self.s("GET"))
        )
        new_args = [cst.Arg(method, comma=_comma()), cst.Arg(path)]
        if params is not None:
            new_args[-1] = new_args[-1].with_changes(comma=_comma())
            new_args.append(cst.Arg(params.value, keyword=cst.Name("params"), equal=_eq()))
        if "json" in kw:
            new_args[-1] = new_args[-1].with_changes(comma=_comma())
            new_args.append(cst.Arg(kw["json"].value, keyword=cst.Name("json"), equal=_eq()))
        ufunc = cast(cst.Attribute, updated.func)
        new = updated.with_changes(func=ufunc.with_changes(attr=cst.Name("request")), args=new_args)
        status: Status = "rewritten" if conf == "yes" else "needs-review"
        self.record("restful", status, "restful(path, method=...) → request(method, path)", original, new)
        return new

    def _apexecute(self, original: cst.Call, updated: cst.Call) -> Any:
        func = cast(cst.Attribute, original.func)
        conf = self.confirm_distinctive(func.value)
        if conf is None or self._starred(original, "apexecute"):
            return updated
        args = list(updated.args)
        positional = [a for a in args if a.keyword is None]
        kw = {a.keyword.value: a for a in args if a.keyword is not None}
        action = positional[0].value if positional else None
        text = _string_value(action) if action is not None else None
        if text is None or len(positional) > 3 or set(kw) - {"method", "data"}:
            self.record(
                "apexecute",
                "todo",
                'use sf.request(METHOD, "/services/apexrest/<path>", json=data)',
                original,
            )
            return updated
        method = (
            kw["method"].value
            if "method" in kw
            else (positional[1].value if len(positional) > 1 else self.s("GET"))
        )
        data = kw["data"].value if "data" in kw else (positional[2].value if len(positional) > 2 else None)
        new_args = [
            cst.Arg(method, comma=_comma()),
            cst.Arg(self.s("/services/apexrest/" + text.lstrip("/"))),
        ]
        if data is not None:
            new_args[-1] = new_args[-1].with_changes(comma=_comma())
            new_args.append(cst.Arg(data, keyword=cst.Name("json"), equal=_eq()))
        ufunc = cast(cst.Attribute, updated.func)
        new = updated.with_changes(func=ufunc.with_changes(attr=cst.Name("request")), args=new_args)
        status: Status = "rewritten" if conf == "yes" else "needs-review"
        self.record(
            "apexecute", status, 'apexecute(path) → request(method, "/services/apexrest/path")', original, new
        )
        return new

    def leave_Await(self, original_node: cst.Await, updated_node: cst.Await) -> Any:
        if original_node in self.replace:
            return self.replace[original_node]
        return updated_node


def _before(a: Any, b: Any) -> bool:
    return (a.line, a.column) < (b.line, b.column)


def _slice(sub: cst.Subscript) -> cst.BaseExpression | None:
    if len(sub.slice) == 1 and isinstance(sub.slice[0].slice, cst.Index):
        return sub.slice[0].slice.value
    return None


def _comma() -> cst.Comma:
    return cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))


def _eq() -> cst.AssignEqual:
    return cst.AssignEqual(cst.SimpleWhitespace(""), cst.SimpleWhitespace(""))


class _ImportFixer(cst.CSTTransformer):
    """Drop simple-salesforce names that are no longer used and add the esf import once."""

    def __init__(self, drop: set[str], add_import: str | None) -> None:
        super().__init__()
        self.drop = drop
        self.add_import = add_import

    def leave_ImportFrom(self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom) -> Any:
        if updated_node.module is None or isinstance(updated_node.names, cst.ImportStar):
            return updated_node
        if _code(updated_node.module).split(".")[0] not in SF_MODULES:
            return updated_node
        keep = [a for a in updated_node.names if (_alias(a) or _code(a.name)) not in self.drop]
        if len(keep) == len(updated_node.names):
            return updated_node
        if not keep:
            return cst.RemoveFromParent()
        # keep the original layout: the last kept name takes the original last name's comma
        keep[-1] = keep[-1].with_changes(comma=updated_node.names[-1].comma)
        return updated_node.with_changes(names=keep)

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        if self.add_import is None:
            return updated_node
        body = list(updated_node.body)
        idx = 0
        for i, stmt in enumerate(body):
            if m.matches(stmt, m.SimpleStatementLine(body=[m.Import() | m.ImportFrom()])):
                idx = i + 1
            elif i == 0 and m.matches(stmt, m.SimpleStatementLine(body=[m.Expr(m.SimpleString())])):
                idx = 1  # after the docstring
        body.insert(idx, cst.parse_statement(self.add_import + "\n"))
        return updated_node.with_changes(body=body)


class _Referenced(cst.CSTVisitor):
    """Names used anywhere except in import statements."""

    def __init__(self) -> None:
        self.names: set[str] = set()
        self._in_import = 0
        self._skip: set[int] = set()  # Name nodes that are attribute or keyword names

    def visit_Import(self, node: cst.Import) -> None:
        self._in_import += 1

    def leave_Import(self, original_node: cst.Import) -> None:
        self._in_import -= 1

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        self._in_import += 1

    def leave_ImportFrom(self, original_node: cst.ImportFrom) -> None:
        self._in_import -= 1

    def visit_Attribute(self, node: cst.Attribute) -> None:
        self._skip.add(id(node.attr))  # `esf.SalesforceError` doesn't use a name SalesforceError

    def visit_Arg(self, node: cst.Arg) -> None:
        if node.keyword is not None:
            self._skip.add(id(node.keyword))

    def visit_Name(self, node: cst.Name) -> None:
        if not self._in_import and id(node) not in self._skip:
            self.names.add(node.value)


def _referenced(module: cst.Module) -> set[str]:
    v = _Referenced()
    module.visit(v)
    return v.names


def _run(
    module: cst.Module, facts: _Collector, opts: Options, leftover: set[str]
) -> tuple[cst.Module, _Transformer]:
    tf = _Transformer(facts, opts)
    tf.leftover = leftover
    return MetadataWrapper(module, unsafe_skip_copy=True).visit(tf), tf


def _compiles(source: str) -> bool:
    try:
        compile(source, "<esf-migrate>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return False
    return True


def transform_source(source: str, opts: Options) -> tuple[str, list[Finding]]:
    """Migrate one module's source.

    Raises:
        libcst.ParserSyntaxError: the source doesn't parse.
        RuntimeError: a rewrite would produce invalid code (the file must be left unchanged).
    """
    module = cst.parse_module(source)
    facts = _Collector()
    module.visit(facts)
    new_module, tf = _run(module, facts, opts, set())
    used = _referenced(new_module)
    sf_locals = facts.sf_imported | facts.sf_module_aliases
    leftover: set[str] = (sf_locals & used) if opts.rules is None or "unsupported" in opts.rules else set()
    if leftover:  # second pass, to mark the imports that are still needed
        new_module, tf = _run(module, facts, opts, leftover)
        used = _referenced(new_module)
    candidates = set(tf.rewritten_exceptions)
    if not tf.format_soql_kept:
        candidates |= facts.format_soql_names
    drop = {n for n in candidates if n not in used}
    add_import: str | None = None
    if tf.need_esf and facts.esf_alias is None:
        add_import = (
            "import even_simpler_salesforce"
            if facts.esf_conflict
            else "import even_simpler_salesforce as esf"
        )
    if drop or add_import:
        new_module = new_module.visit(_ImportFixer(drop, add_import))
    code = new_module.code
    if code != source and _compiles(source):  # the original may use syntax newer than this Python
        try:
            compile(code, "<esf-migrate>", "exec", dont_inherit=True)
        except SyntaxError as exc:  # safety net: never write code that doesn't compile
            raise RuntimeError(
                f"a rewrite would produce invalid code ({exc.msg}, line {exc.lineno})"
            ) from exc
    findings = sorted(tf.done, key=lambda f: (f.line, f.col, f.rule))
    return code, findings
