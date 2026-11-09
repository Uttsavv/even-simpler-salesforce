"""The libcst transform behind ``esf migrate`` (§9.19). Original code (AGENTS.md licensing).

One module at a time: a collector pass finds client names, imports and how results are used
(libcst scope analysis, same module only), then a transformer pass rewrites what it can prove
safe and records findings. Markers are attached to the statement that contains each finding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, cast

import libcst as cst
import libcst.matchers as m
from libcst.metadata import (
    CodeRange,
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
    "SalesforceExpiredSession": ("SessionExpired", True),
    "SalesforceMalformedRequest": (
        "RequestError (or a specific class: InvalidField, MalformedQuery, ValidationRuleFailed, ...)",
        False,
    ),
    "SalesforceGeneralError": ("ServerError (or SalesforceError for any status)", False),
}
INTERNALS_TODO = {
    "session_id": "the access token isn't public; make calls with sf.request(...)",
    "headers": "make calls with sf.request(...); auth headers are added for you",
    "session": "make calls with sf.request(...); the HTTP client is managed for you",
    "base_url": "use sf.instance_url to build links, sf.request(...) for calls",
    "sf_instance": "use sf.instance_url (the instance of the current token; None before the first call)",
}
DISTINCTIVE_INTERNALS = frozenset({"sf_instance", "sf_version"})  # names only simple-salesforce uses
QUERY_KEYS = {"records": "records", "totalSize": "total_size", "done": "done"}
UNSUPPORTED = {
    "bulk": "Bulk API v1 isn't supported; use sf.bulk (Bulk 2.0, planned for 0.3)",
    "bulk2": "Bulk 2.0 arrives as sf.bulk in 0.3",
    "toolingexecute": "Tooling arrives as sf.tooling in 0.3; use sf.request(...) meanwhile",
    "mdapi": "the Metadata API arrives as sf.metadata in 0.4",
}
_PLACEHOLDER = re.compile(r"(?<!\{)\{([^{}]*)\}(?!\})")


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


def _str_node(text: str) -> cst.SimpleString:
    return cst.SimpleString('"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"')


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

    def visit_Import(self, node: cst.Import) -> None:
        for alias in node.names:
            full = _code(alias.name)
            local = (
                alias.asname.name.value if alias.asname and isinstance(alias.asname.name, cst.Name) else full
            )
            if full.split(".")[0] in SF_MODULES:
                self.sf_module_aliases.add(local.split(".")[0])
            if full == "even_simpler_salesforce":
                self.esf_alias = local

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        if node.module is None or isinstance(node.names, cst.ImportStar):
            return
        mod = _code(node.module)
        if mod.split(".")[0] not in SF_MODULES:
            return
        for alias in node.names:
            name = _code(alias.name)
            local = (
                alias.asname.name.value if alias.asname and isinstance(alias.asname.name, cst.Name) else name
            )
            if name in CLIENT_TYPES:
                self.client_types.add(local)
            if name in LOGIN_CALLS:
                self.login_names.add(local)
            if name in EXCEPTIONS:
                self.exception_names[local] = name
            if name == "format_soql":
                self.format_soql_names.add(local)

    def _is_client_type(self, node: cst.BaseExpression) -> bool:
        names = _annotation_names(node)
        return bool(names & (self.client_types | CLIENT_TYPES))

    def _is_client_value(self, value: cst.BaseExpression) -> bool:
        if isinstance(value, cst.Await):
            value = value.expression
        if isinstance(value, cst.Call):
            seg = _last_segment(value.func)
            return seg is not None and (seg in self.client_types or seg in self.client_functions)
        seg = _last_segment(value)
        return seg is not None and seg in self.clients

    def visit_Param(self, node: cst.Param) -> None:
        if node.annotation is not None and self._is_client_type(node.annotation.annotation):
            self.clients.add(node.name.value)

    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        seg = _last_segment(node.target)
        if seg and (
            self._is_client_type(node.annotation.annotation)
            or (node.value is not None and self._is_client_value(node.value))
        ):
            self.clients.add(seg)

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        if node.returns is not None and self._is_client_type(node.returns.annotation):
            self.client_functions.add(node.name.value)

    def leave_Assign(self, original_node: cst.Assign) -> None:
        if self._is_client_value(original_node.value):
            for t in original_node.targets:
                seg = _last_segment(t.target)
                if seg:
                    self.clients.add(seg)


class _Transformer(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (PositionProvider, ParentNodeProvider, ScopeProvider)

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

    # --- helpers -----------------------------------------------------------------------------

    def enabled(self, rule: str) -> bool:
        return self.opts.rules is None or rule in self.opts.rules

    def pos(self, node: cst.CSTNode) -> CodeRange:
        return cast(CodeRange, self.get_metadata(PositionProvider, node))

    def parent(self, node: cst.CSTNode) -> cst.CSTNode | None:
        return self.get_metadata(ParentNodeProvider, node, None)

    def ignored(self, rule: str) -> bool:
        return bool(self.holders) and rule in self.holders[-1].ignores

    def record(
        self,
        rule: str,
        status: Status,
        message: str,
        node: cst.CSTNode,
        after: cst.CSTNode | str | None = None,
        suggestion: str | None = None,
    ) -> None:
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
        else:  # pragma: no cover - every expression lives in a statement
            self.done.append(f)

    def confirm(self, expr: cst.BaseExpression, obj: str | None = None) -> str | None:
        """``"yes"`` (confirmed), ``"fallback"`` (§9.19.1 case 3) or ``None``."""
        seg = _last_segment(expr)
        if seg is not None and (seg in self.opts.client_names or seg in self.facts.clients):
            return "yes"
        if isinstance(expr, cst.Call) and self.facts._is_client_value(expr):
            return "yes"
        if not self.opts.client_names and obj is not None and obj[:1].isupper():
            return "fallback"
        return None

    def confirm_distinctive(self, expr: cst.BaseExpression) -> str | None:
        """For simple-salesforce-only method/attribute names (query_all, restful, ...)."""
        c = self.confirm(expr)
        if c:
            return c
        return None if self.opts.client_names else "fallback"

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

    def _push(self, node: cst.CSTNode) -> None:
        ignores: set[str] = set()
        for line in getattr(node, "leading_lines", ()):
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
        existing = {line.comment.value.strip() for line in updated.leading_lines if line.comment is not None}
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
        return self._pop(updated_node)

    def visit_ExceptHandler(self, node: cst.ExceptHandler) -> None:
        self._push(node)

    def leave_ExceptHandler(self, original_node: Any, updated_node: Any) -> Any:
        return self._pop(updated_node)

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
        if sf_name is None or not self.enabled("exceptions") or self.ignored("exceptions"):
            return updated_node
        parent = self.parent(original_node)
        if isinstance(parent, cst.Attribute) and parent.attr is original_node:
            return updated_node
        if isinstance(parent, (cst.FunctionDef, cst.ClassDef, cst.Param)):
            return updated_node
        target, exact = EXCEPTIONS[sf_name]
        if exact:
            alias = self.facts.esf_alias or "esf"
            new = cst.Attribute(value=cst.Name(alias), attr=cst.Name(target))
            self.need_esf = True
            self.rewritten_exceptions.add(original_node.value)
            self.record("exceptions", "rewritten", f"{sf_name} → esf.{target}", original_node, new)
            return new
        self.kept_exceptions.add(original_node.value)
        self.record(
            "exceptions",
            "todo",
            f"{sf_name} has no 1:1 class; catch esf.{target}",
            original_node,
            suggestion=f"esf.{target.split(' ')[0]}",
        )
        return updated_node

    # --- attributes: internals, stored refs, mocks ----------------------------------------------

    def leave_Attribute(self, original_node: cst.Attribute, updated_node: cst.Attribute) -> Any:
        self._flush_pending(original_node)
        if original_node in self.replace:
            return self.replace[original_node]
        attr = original_node.attr.value
        parent = self.parent(original_node)
        # client internals
        if attr in INTERNALS_TODO or attr == "sf_version":
            conf = (
                self.confirm_distinctive(original_node.value)
                if attr in DISTINCTIVE_INTERNALS
                else self.confirm(original_node.value)
            )
            if conf and self.enabled("client-internals") and not self.ignored("client-internals"):
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
        if attr in UNSUPPORTED and self.confirm(original_node.value) and self.enabled("unsupported"):
            if not self.ignored("unsupported"):
                self.record("unsupported", "todo", UNSUPPORTED[attr], original_node)
            return updated_node
        # mocks: X.Obj.method.<mock attr> / X.Obj.method = ... in test files
        inner = original_node.value
        if (
            self.opts.is_test_file
            and attr in PER_OBJECT
            and isinstance(inner, cst.Attribute)
            and not isinstance(parent, cst.Call)
            and self._is_mock_use(original_node, parent)
            and self.enabled("mock-sobject")
            and not self.ignored("mock-sobject")
        ):
            obj = inner.attr.value
            if obj not in CLIENT_ATTRS and self.confirm(inner.value, obj):
                new = updated_node.with_changes(
                    value=_sobject_call(cast(cst.Attribute, updated_node.value).value, _str_node(obj))
                )
                self.record(
                    "mock-sobject",
                    "needs-review",
                    "sobject() mocks don't distinguish objects; prefer esf.testing.FakeSalesforce",
                    original_node,
                    new,
                )
                return new
        return updated_node

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
                new_value = _sobject_call(cast(cst.Attribute, updated_node.value).value, _str_node(obj))
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
        if original_node in self.replace:
            return self.replace[original_node]
        func = original_node.func
        # login / construction
        seg = _last_segment(func)
        if seg in self.facts.login_names and self.enabled("login") and not self.ignored("login"):
            self.record(
                "login",
                "todo",
                "construct esf.Salesforce / esf.AsyncSalesforce with a login flow: ClientCredentials, "
                "JwtBearer or WebServerFlow; StaticToken(access_token, instance_url) replaces "
                "session_id=/instance=; username/password login isn't supported",
                original_node,
            )
            return updated_node
        # format_soql used outside a query call
        if seg in self.facts.format_soql_names:
            parent = self.parent(original_node)
            in_query = isinstance(parent, cst.Arg) and self._is_query_call(self.parent(parent))
            if not in_query and self.enabled("format-soql") and not self.ignored("format-soql"):
                self.format_soql_kept = True
                self.record(
                    "format-soql",
                    "todo",
                    'pass the values to the query method instead: sf.query("... = {}", value)',
                    original_node,
                )
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
        if method in PER_OBJECT and isinstance(target, cst.Attribute):
            return self._object_call(original_node, updated_node)
        if method in ("query_all", "query_all_iter"):
            return self._query_all(original_node, updated_node)
        if method == "query_more" and self.enabled("query-more") and not self.ignored("query-more"):
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
            self._query_result(original_node)
            return self._format_soql_arg(original_node, updated_node)
        return updated_node

    # sobject-attr and friends

    def _object_call(self, original: cst.Call, updated: cst.Call) -> Any:
        func = cast(cst.Attribute, original.func)
        inner = cast(cst.Attribute, func.value)
        obj = inner.attr.value
        method = func.attr.value
        if obj in CLIENT_ATTRS:
            return updated
        conf = self.confirm(inner.value, obj)
        if conf is None:
            return updated
        rule = "sobject-attr" if conf == "yes" else "not-a-client"
        if not self.enabled("sobject-attr") or self.ignored(rule) or self.ignored("sobject-attr"):
            return updated
        ufunc = cast(cst.Attribute, updated.func)
        new_func = ufunc.with_changes(
            value=_sobject_call(cast(cst.Attribute, ufunc.value).value, _str_node(obj))
        )
        new = updated.with_changes(func=new_func)
        if method == "get_by_custom_id" and self.enabled("get-by-custom-id"):
            new = new.with_changes(func=new_func.with_changes(attr=cst.Name("get_by_external_id")))
            self.record("get-by-custom-id", "rewritten", "get_by_custom_id → get_by_external_id", original)
        if method == "upsert" and self.enabled("upsert-path"):
            new = self._upsert_path(original, new)
        status: Status = "rewritten" if conf == "yes" else "needs-review"
        msg = "object accessed through sobject()"
        if conf != "yes":
            msg = "rewritten to sobject(); couldn't confirm this is a Salesforce client"
        self.record(rule, status, msg, original, new)
        if method == "create" and self.enabled("create-return"):
            self._create_return(original)
        elif method in ("update", "upsert", "delete") and self.enabled("return-value"):
            self._return_value(original, method)
        return new

    def _upsert_path(self, original: cst.Call, new: cst.Call) -> cst.Call:
        args = list(new.args)
        if not args or args[0].keyword is not None or self.ignored("upsert-path"):
            return new
        first = args[0].value
        orig_first = original.args[0].value  # positions live on original nodes
        text = _string_value(first)
        if text is not None and "/" in text:
            fld, _, val = text.partition("/")
            split = [
                args[0].with_changes(value=_str_node(fld)),
                cst.Arg(_str_node(val), comma=args[0].comma),
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
                        head_node = _str_node("".join(cast(cst.FormattedStringText, p).value for p in head))
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
        if conf is None:
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
        listed: cst.BaseExpression = (
            cst.ListComp(
                elt=cst.Name("r"),
                for_in=cst.CompFor(target=cst.Name("r"), iter=iter_call, asynchronous=cst.Asynchronous()),
            )
            if is_async
            else cst.Call(func=cst.Name("list"), args=[cst.Arg(iter_call)])
        )
        if isinstance(parent, cst.Subscript) and _string_value(_slice(parent) or cst.Name("x")) == "records":
            self.replace[parent] = listed
            self.record(
                "query-all", status, 'query_all(q)["records"] → list of rows' + suffix, parent, listed
            )
            return updated  # the subscript is replaced as a whole
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
        """``X.query(q)["records"]`` → ``.records`` (QueryResult isn't a dict)."""
        if not self.enabled("query-result") or self.ignored("query-result"):
            return
        _, parent = self._call_context(call)
        subs: list[cst.Subscript] = []
        if isinstance(parent, cst.Subscript):
            subs = [parent]
        elif isinstance(parent, cst.Assign) and isinstance(parent.targets[0].target, cst.Name):
            uses = self.scope_uses(parent.targets[0].target) or []
            for u in uses:
                p = self.parent(u)
                if isinstance(p, cst.Subscript) and p.value is u:
                    subs.append(p)
                elif isinstance(p, cst.Attribute) and p.value is u and p.attr.value == "get":
                    self._query_result_get(p)
        for sub in subs:
            key = _string_value(_slice(sub) or cst.Name("x"))
            if key in QUERY_KEYS:
                self.to_attr[sub] = QUERY_KEYS[key]
                new = cst.Attribute(value=sub.value, attr=cst.Name(QUERY_KEYS[key]))
                self.record("query-result", "rewritten", f'["{key}"] → .{QUERY_KEYS[key]}', sub, new)
            else:
                self.pending[sub] = (
                    "query-result",
                    "todo",
                    "query() returns a QueryResult (records, total_size, done, cursor), not a dict",
                )

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
        self.pending[attr] = (
            "query-result",
            "todo",
            "query() returns a QueryResult (records, total_size, done, cursor), not a dict",
        )

    # raw requests

    def _restful(self, original: cst.Call, updated: cst.Call) -> Any:
        func = cast(cst.Attribute, original.func)
        conf = self.confirm_distinctive(func.value)
        if conf is None:
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
            else (positional[2].value if len(positional) > 2 else _str_node("GET"))
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
        if conf is None:
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
            else (positional[1].value if len(positional) > 1 else _str_node("GET"))
        )
        data = kw["data"].value if "data" in kw else (positional[2].value if len(positional) > 2 else None)
        new_args = [
            cst.Arg(method, comma=_comma()),
            cst.Arg(_str_node("/services/apexrest/" + text.lstrip("/"))),
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
    """Drop rewritten simple-salesforce names from imports and add ``import ... as esf``."""

    def __init__(self, drop: set[str], add_esf: bool) -> None:
        super().__init__()
        self.drop = drop
        self.add_esf = add_esf

    def leave_ImportFrom(self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom) -> Any:
        if updated_node.module is None or isinstance(updated_node.names, cst.ImportStar):
            return updated_node
        if _code(updated_node.module).split(".")[0] not in SF_MODULES:
            return updated_node
        keep = [
            a
            for a in updated_node.names
            if (a.asname.name.value if a.asname and isinstance(a.asname.name, cst.Name) else _code(a.name))
            not in self.drop
        ]
        if len(keep) == len(updated_node.names):
            return updated_node
        if not keep:
            return cst.RemoveFromParent()
        # keep the original layout: the last kept name takes the original last name's comma
        keep[-1] = keep[-1].with_changes(comma=updated_node.names[-1].comma)
        return updated_node.with_changes(names=keep)

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        if not self.add_esf:
            return updated_node
        body = list(updated_node.body)
        idx = 0
        for i, stmt in enumerate(body):
            if m.matches(stmt, m.SimpleStatementLine(body=[m.Import() | m.ImportFrom()])):
                idx = i + 1
            elif i == 0 and m.matches(stmt, m.SimpleStatementLine(body=[m.Expr(m.SimpleString())])):
                idx = 1  # after the docstring
        line = cst.parse_statement("import even_simpler_salesforce as esf\n")
        body.insert(idx, line)
        return updated_node.with_changes(body=body)


def transform_source(source: str, opts: Options) -> tuple[str, list[Finding]]:
    """Migrate one module's source. Raises ``libcst.ParserSyntaxError`` on unparsable code."""
    module = cst.parse_module(source)
    facts = _Collector()
    module.visit(facts)
    wrapper = MetadataWrapper(module)
    tf = _Transformer(facts, opts)
    new_module = wrapper.visit(tf)
    drop = set(tf.rewritten_exceptions) - set(tf.kept_exceptions)
    if (
        not tf.format_soql_kept
        and facts.format_soql_names
        and _uses(new_module, facts.format_soql_names) == 0
    ):
        drop |= facts.format_soql_names
    if drop or (tf.need_esf and facts.esf_alias is None):
        new_module = new_module.visit(_ImportFixer(drop, tf.need_esf and facts.esf_alias is None))
    findings = sorted(tf.done, key=lambda f: (f.line, f.col, f.rule))
    return new_module.code, findings


def _uses(module: cst.Module, names: set[str]) -> int:
    count = 0
    for n in m.findall(module, m.Name()):
        if cast(cst.Name, n).value in names:
            count += 1
    imported = 0
    for imp in m.findall(module, m.ImportFrom()):
        node = cast(cst.ImportFrom, imp)
        if not isinstance(node.names, cst.ImportStar):
            imported += sum(1 for a in node.names if _code(a.name) in names)
    return count - imported


def shaped_sites(source: str) -> int:
    """How many simple-salesforce-shaped call sites remain (for ``--check``)."""
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError:
        return 0
    count = 0
    for node in m.findall(module, m.Call(func=m.Attribute(value=m.Attribute()))):
        call = cast(cst.Call, node)
        func = cast(cst.Attribute, call.func)
        inner = cast(cst.Attribute, func.value)
        if (
            func.attr.value in PER_OBJECT
            and inner.attr.value not in CLIENT_ATTRS
            and inner.attr.value[:1].isupper()
        ):
            count += 1
    for _ in m.findall(module, m.Call(func=m.Attribute(attr=m.Name("query_all") | m.Name("query_all_iter")))):
        count += 1
    return count
