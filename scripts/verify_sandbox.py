#!/usr/bin/env python3
"""Probe the open §21 verification items against a sandbox org and write redacted evidence.

Usage::

    # login: same ESF_SANDBOX_* variables as tests/sandbox (see tests/sandbox/README.md)
    uv run python scripts/verify_sandbox.py                      # V4, V5b (if configured), V20
    uv run python scripts/verify_sandbox.py --allow-writes       # + V19 (creates and deletes 1-2 records)
    uv run python scripts/verify_sandbox.py --probe-v3           # + V3 if rotation is OFF
    uv run python scripts/verify_sandbox.py --probe-v3 --allow-risky   # + V3 even with rotation ON
    uv run python scripts/verify_sandbox.py --only V4,V20 --out /tmp/evidence.json

Items:

- **V4** ``POST /services/oauth2/introspect``: which response fields exist, is ``exp`` among
  them (access token, and refresh token in web-server mode). Needs a flow with a client id
  (web-server, client credentials or JWT); not possible with a bare access token.
- **V5b** error codes for a validation rule, a Flow custom error, an Apex ``addError()`` and an
  unhandled trigger exception. You configure a record that triggers each one with
  ``ESF_SANDBOX_V5B_VALIDATION_RULE``, ``ESF_SANDBOX_V5B_FLOW_ERROR``,
  ``ESF_SANDBOX_V5B_APEX_ADD_ERROR`` and ``ESF_SANDBOX_V5B_TRIGGER_EXCEPTION``, each a JSON object
  ``{"sobject": "Lead", "record": {"LastName": "x", "Company": "ESF_VR"}}``; unset ones are
  recorded as "not configured". A create that unexpectedly succeeds is deleted again.
- **V19** ``GET sobjects/{o}/{extIdField}/{value}`` with a value ending in ``.json`` (needs
  ``ESF_SANDBOX_EXTERNAL_ID_FIELD`` and ``--allow-writes`` / ``ESF_SANDBOX_ALLOW_WRITES=1``).
  Creates one record with ``<tag>.json`` as external id, reads it back as-is, with the dot
  percent-encoded, and upserts it; deletes everything it created.
- **V20** ``Sforce-Call-Options: client=<name>`` accepted on several REST resources.
- **V3** the exact body of "Token request is already being processed": 8 simultaneous raw
  refresh POSTs with the same refresh token (web-server mode only, ``--probe-v3``). **With
  refresh-token rotation ON this can revoke the whole token chain** (reuse of a rotated-out
  token), so it is skipped unless ``--allow-risky`` too; afterwards the script checks whether
  the stored token still works and tells you to log in again if not.

Output: ``tests/fixtures/recorded/verification/<YYYY-MM-DD>.json`` (``--out`` to change). The
file holds field names, status codes, error codes and Salesforce's error descriptions only:
no tokens, no record values, no ids beyond their 3-character key prefix, no host names. The
script refuses to write it if anything that looks like a token or a known secret slipped in.
It prints a short human summary at the end.

Needs only the library and the stdlib (plus ``tests/sandbox`` from this checkout for the
shared settings and token store).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import platform
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402  (a core dependency of the library)
from tests.sandbox.settings import Settings, load_settings  # noqa: E402

import even_simpler_salesforce as esf  # noqa: E402
from even_simpler_salesforce.auth.token import merge_token, parse_token_response  # noqa: E402

OUT_DIR = ROOT / "tests" / "fixtures" / "recorded" / "verification"
ITEMS = ("V3", "V4", "V5b", "V19", "V20")
V5B_CASES = {
    "validation_rule": ("V5B_VALIDATION_RULE", "FIELD_CUSTOM_VALIDATION_EXCEPTION"),
    "flow_custom_error": ("V5B_FLOW_ERROR", "FIELD_CUSTOM_VALIDATION_EXCEPTION"),
    "apex_add_error": ("V5B_APEX_ADD_ERROR", "FIELD_CUSTOM_VALIDATION_EXCEPTION"),
    "trigger_exception": ("V5B_TRIGGER_EXCEPTION", "CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY"),
}
CLIENT_NAME = "esf-verify"
SF_ID = re.compile(r"\b([a-zA-Z0-9]{3})[a-zA-Z0-9]{12}(?:[a-zA-Z0-9]{3})?\b")
TOKEN_LIKE = re.compile(r"00D[A-Za-z0-9]{12,15}![A-Za-z0-9._-]+|[A-Za-z0-9._=-]{60,}")


# --- redaction -------------------------------------------------------------------------------


def redact_text(text: str | None, limit: int = 300) -> str | None:
    """Salesforce error text with ids cut to their key prefix and token-looking runs removed."""
    if text is None:
        return None
    text = TOKEN_LIKE.sub("<redacted>", text)
    text = SF_ID.sub(
        lambda m: f"{m.group(1)}<id>" if any(c.isdigit() for c in m.group(0)) else m.group(0), text
    )
    return text[:limit]


def error_summary(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, esf.SalesforceError):
        return {
            "exception": type(exc).__name__,
            "status": exc.status,
            "error_code": exc.error_code,
            "error_codes": [d.error_code for d in exc.errors],
            "fields": list(exc.fields),
            "message_length": len(exc.message or ""),
        }
    return {"exception": type(exc).__name__}


def instance_kind(url: str) -> str:
    host = httpx.URL(url).host
    for marker, kind in (
        (".sandbox.my.salesforce.com", "sandbox"),
        (".scratch.my.salesforce.com", "scratch"),
        (".develop.my.salesforce.com", "developer"),
        (".my.salesforce.com", "production-or-developer"),
    ):
        if host.endswith(marker):
            return kind
    return "unknown"


# --- probes ----------------------------------------------------------------------------------


class Probe:
    def __init__(self, settings: Settings, args: argparse.Namespace) -> None:
        self.s = settings
        self.args = args
        self.tag = f"esf-sandbox-{uuid.uuid4().hex[:10]}"
        self.secrets: set[str] = set()
        self.sent_paths: list[str] = []

    def client(self, **kw: Any) -> esf.Salesforce:
        hooks = {"request": [lambda r: self.sent_paths.append(r.url.raw_path.decode("ascii", "replace"))]}
        kw.setdefault("http_client", httpx.Client(event_hooks=hooks, timeout=60))
        kw.setdefault("client_name", CLIENT_NAME)
        return esf.Salesforce(
            self.s.auth(),
            store=self.s.sync_store() if self.s.web_server else None,
            **self.s.client_kwargs(**kw),
        )

    def remember_secrets(self, tok: esf.Token | None) -> None:
        if tok is None:
            return
        for value in (tok.access_token, tok.refresh_token, tok.signature):
            if value:
                self.secrets.add(value)

    # V4 ------------------------------------------------------------------------------------

    def v4(self) -> dict[str, Any]:
        if self.s.mode == "env" and self.s.env.get("ESF_SANDBOX_ACCESS_TOKEN"):
            return {
                "status": "not configured",
                "why": "introspection needs a client id (not a bare access token)",
            }
        out: dict[str, Any] = {}
        with self.client() as sf:
            tok = sf.auth.token()
            self.remember_secrets(tok)
            info = sf.auth.introspect()
            out["access_token"] = self._introspect_fields(info)
            if self.s.web_server and tok.refresh_token:
                rt: Any = sf._runtime  # pyright: ignore[reportPrivateUsage]
                try:
                    out["refresh_token"] = self._introspect_fields(rt.introspect(tok, hint="refresh_token"))
                except esf.SalesforceError as exc:
                    out["refresh_token"] = {"error": error_summary(exc)}
        out["status"] = "recorded"
        out["exp_present"] = bool(out["access_token"].get("has_exp"))
        return out

    @staticmethod
    def _introspect_fields(info: dict[str, Any]) -> dict[str, Any]:
        types = {k: type(v).__name__ for k, v in sorted(info.items())}
        exp = info.get("exp")
        iat = info.get("iat")
        out: dict[str, Any] = {"fields": sorted(info), "types": types, "has_exp": "exp" in info}
        if isinstance(exp, (int, float)) and isinstance(iat, (int, float)):
            out["exp_minus_iat_seconds"] = int(exp - iat)  # the session timeout, not a secret
        if "active" in info:
            out["active"] = bool(info["active"])
        return out

    # V5b -----------------------------------------------------------------------------------

    def v5b(self) -> dict[str, Any]:
        cases: dict[str, Any] = {}
        any_configured = False
        with self.client() as sf:
            for name, (var, expected) in V5B_CASES.items():
                raw = self.s.env.get("ESF_SANDBOX_" + var, "").strip()
                if not raw:
                    cases[name] = {"status": "not configured", "env": "ESF_SANDBOX_" + var}
                    continue
                any_configured = True
                if self.s.read_only:
                    cases[name] = {"status": "skipped", "why": "ESF_SANDBOX_READ_ONLY=1"}
                    continue
                try:
                    spec = json.loads(raw)
                    sobject, record = str(spec["sobject"]), dict(spec["record"])
                except (ValueError, KeyError, TypeError) as exc:
                    cases[name] = {
                        "status": "bad config",
                        "why": f'{type(exc).__name__}: expected {{"sobject": "...", "record": {{...}}}}',
                    }
                    continue
                try:
                    new_id = sf.sobject(sobject).create(record)
                except esf.SalesforceError as exc:
                    summary = error_summary(exc)
                    cases[name] = {
                        "status": "recorded",
                        "sobject": sobject,
                        "expected_error_code": expected,
                        "matches_spec": exc.error_code == expected,
                        **summary,
                    }
                else:
                    with contextlib.suppress(esf.SalesforceError):
                        sf.sobject(sobject).delete(new_id)
                    cases[name] = {
                        "status": "no error",
                        "sobject": sobject,
                        "why": "the configured record was saved (and deleted again); check the automation",
                    }
        return {"status": "recorded" if any_configured else "not configured", "cases": cases}

    # V19 -----------------------------------------------------------------------------------

    def v19(self) -> dict[str, Any]:
        if self.s.external_id is None:
            return {"status": "not configured", "env": "ESF_SANDBOX_EXTERNAL_ID_FIELD=<Object>.<Field>"}
        if not (self.args.allow_writes or self.s.allow_writes) or self.s.read_only:
            return {"status": "skipped", "why": "needs --allow-writes or ESF_SANDBOX_ALLOW_WRITES=1"}
        obj, field = self.s.external_id
        value = f"{self.tag}.json"
        record: dict[str, Any] = {"LastName": self.tag, field: value}
        if obj == "Lead":
            record["Company"] = "esf-sandbox"
        out: dict[str, Any] = {"sobject": obj, "value_shape": "<tag>.json"}
        created: list[str] = []
        with self.client() as sf:
            api = sf.sobject(obj)
            try:
                rec_id = api.create(record)
            except esf.SalesforceError as exc:
                return {"status": "error", "step": "create", **error_summary(exc)}
            created.append(rec_id)
            try:
                out["library_get_by_external_id"] = self._v19_get(
                    lambda: api.get_by_external_id(field, value, ["Id"]), rec_id
                )
                out["library_sent_path"] = self._last_path(value)
                encoded = value.replace(".", "%2E")
                out["raw_get_dot_percent_encoded"] = self._v19_get(
                    lambda: sf.request("GET", f"sobjects/{obj}/{field}/{encoded}"), rec_id
                )
                out["raw_sent_path"] = self._last_path(value, encoded)
                try:
                    res = api.upsert(field, value, {"LastName": self.tag + "-upsert"})
                    if res.id and res.id not in created:
                        created.append(res.id)
                    out["library_upsert"] = {
                        "created": res.created,
                        "same_record": bool(res.id and res.id[:15] == rec_id[:15]),
                    }
                except esf.SalesforceError as exc:
                    out["library_upsert"] = {"error": error_summary(exc)}
            finally:
                for i in created:
                    with contextlib.suppress(esf.SalesforceError):
                        api.delete(i)
        out["status"] = "recorded"
        out["dot_suffix_breaks_lookup"] = not out["library_get_by_external_id"].get(
            "found_same_record", False
        )
        return out

    def _last_path(self, value: str, *alts: str) -> str | None:
        if not self.sent_paths:
            return None
        path = self.sent_paths[-1]
        for v in (*alts, value):
            path = path.replace(v, "<value>")
        return re.sub(r"/v\d+\.\d/", "/vXX.X/", path)

    @staticmethod
    def _v19_get(call: Callable[[], Any], rec_id: str) -> dict[str, Any]:
        try:
            body = call()
        except esf.SalesforceError as exc:
            return {"ok": False, **error_summary(exc)}
        got = body.get("Id") if isinstance(body, dict) else None
        return {
            "ok": True,
            "response_type": type(body).__name__,
            "found_same_record": bool(isinstance(got, str) and got[:15] == rec_id[:15]),
        }

    # V20 -----------------------------------------------------------------------------------

    def v20(self) -> dict[str, Any]:
        calls: dict[str, Callable[[esf.Salesforce], Any]] = {
            "limits": lambda sf: sf.limits(),
            "query": lambda sf: sf.query("SELECT Id FROM User LIMIT 1"),
            "describe_global": lambda sf: sf.describe_global(cached=False),
            "sobject_describe": lambda sf: sf.sobject("Account").describe(cached=False),
            "search": lambda sf: sf.search("FIND {esfverifynomatch} RETURNING Account(Id) LIMIT 1"),
            "composite_sobjects_retrieve": lambda sf: sf.collections.retrieve(
                "User", [sf.auth.token().user_id or ""], ["Id"]
            ),
            "raw_versions": lambda sf: sf.request("GET", "/services/data/"),
            "userinfo": lambda sf: sf.auth.userinfo(),
        }
        results: dict[str, Any] = {}
        events: list[esf.Event] = []

        class Keep:
            def on_event(self, event: esf.Event) -> None:
                events.append(event)

        with self.client(observers=[Keep()]) as sf:
            for name, call in calls.items():
                before = len(events)
                try:
                    call(sf)
                    status = next((e.get("status") for e in events[before:] if e.name == "sf.request"), None)
                    results[name] = {"accepted": True, "status": status}
                except esf.SalesforceError as exc:
                    results[name] = {"accepted": False, **error_summary(exc)}
        return {
            "status": "recorded",
            "header": f"Sforce-Call-Options: client={CLIENT_NAME}",
            "resources": results,
            "all_accepted": all(r.get("accepted") for r in results.values()),
            "where_admins_see_it": "manual: Event Monitoring ApiTotalUsage / API event logs (CLIENT_NAME)",
        }

    # V3 ------------------------------------------------------------------------------------

    def v3(self) -> dict[str, Any]:
        if not self.args.probe_v3:
            return {"status": "skipped", "why": "opt in with --probe-v3"}
        if not self.s.web_server:
            return {"status": "not configured", "why": "needs web-server mode (a refresh token)"}
        store = self.s.sync_store()
        flow = self.s.flow()
        # One normal refresh first: tells us whether rotation is on.
        with self.client() as sf:
            before = sf.auth.token()
            self.remember_secrets(before)
            rt: Any = sf._runtime  # pyright: ignore[reportPrivateUsage]
            after = rt.refresh(before)
            self.remember_secrets(after)
        rotation = bool(
            after.refresh_token and before.refresh_token and after.refresh_token != before.refresh_token
        )
        out: dict[str, Any] = {"rotation_enabled": rotation}
        if rotation and not self.args.allow_risky:
            return {
                **out,
                "status": "skipped",
                "why": "rotation is ON: concurrent reuse can revoke the chain; rerun with --allow-risky",
            }
        current = store.load(self.s.token_key)
        if current is None or not current.refresh_token:
            return {**out, "status": "error", "why": "no refresh token in the store"}
        form = flow.token_form(current)
        n = 8
        barrier = threading.Barrier(n)
        responses: list[tuple[int, Any, float]] = []
        lock = threading.Lock()

        def fire() -> None:
            with httpx.Client(timeout=60) as http:
                barrier.wait()
                t0 = time.perf_counter()
                r = http.post(flow.token_url, data=form, headers={"Accept": "application/json"})
                try:
                    body: Any = r.json()
                except ValueError:
                    body = None
                with lock:
                    responses.append((r.status_code, body, time.perf_counter() - t0))

        threads = [threading.Thread(target=fire) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        errors: list[dict[str, Any]] = []
        winners: list[dict[str, Any]] = []
        for status, body, _ in responses:
            if status == 200 and isinstance(body, dict):
                winners.append(body)
                for k in ("access_token", "refresh_token", "signature"):
                    if isinstance(body.get(k), str):
                        self.secrets.add(body[k])
            else:
                errors.append(
                    {
                        "status": status,
                        "error": redact_text(body.get("error")) if isinstance(body, dict) else None,
                        "error_description": redact_text(body.get("error_description"))
                        if isinstance(body, dict)
                        else None,
                        "body_keys": sorted(body) if isinstance(body, dict) else None,
                    }
                )
        out.update(
            {
                "concurrent_requests": n,
                "successes": len(winners),
                "errors": errors,
                "already_processing_seen": any(
                    "already being processed" in (e.get("error_description") or "").lower() for e in errors
                ),
            }
        )
        # Keep the chain alive: store the (last) successful response, then check it works.
        if winners:
            new = merge_token(parse_token_response(winners[-1]), current)
            store.save(self.s.token_key, new, expected_version=current.version)
        try:
            with self.client() as sf:
                tok = sf.auth.token()
                rt = sf._runtime  # pyright: ignore[reportPrivateUsage]
                rt.refresh(tok)
                sf.limits()
            out["chain_survived"] = True
        except esf.ReauthorizationRequired:
            out["chain_survived"] = False
            print(
                "V3: the token chain was revoked; log in again with scripts/sandbox_login.py", file=sys.stderr
            )
        out["status"] = "recorded"
        return out


# --- main ------------------------------------------------------------------------------------


def check_clean(text: str, secrets: set[str]) -> None:
    for s in secrets:
        if s and len(s) >= 8 and s in text:
            raise SystemExit("refusing to write evidence: it contains a token value (bug in the script)")
    if re.search(r"00D[A-Za-z0-9]{12,15}!", text):
        raise SystemExit("refusing to write evidence: it contains something that looks like a session id")


def summary_line(item: str, result: dict[str, Any]) -> str:
    status = result.get("status", "?")
    extra = ""
    if item == "V4" and status == "recorded":
        extra = (
            f"exp present: {result.get('exp_present')}; fields: {', '.join(result['access_token']['fields'])}"
        )
    elif item == "V5b" and status == "recorded":
        extra = "; ".join(
            f"{k}: {v.get('error_code') or v.get('status')}" for k, v in result.get("cases", {}).items()
        )
    elif item == "V19" and status == "recorded":
        extra = (
            f"'.json' value breaks lookup: {result.get('dot_suffix_breaks_lookup')}; "
            "%2E-encoded found it: "
            f"{result.get('raw_get_dot_percent_encoded', {}).get('found_same_record', False)}"
        )
    elif item == "V20" and status == "recorded":
        extra = f"accepted everywhere: {result.get('all_accepted')}"
    elif item == "V3" and status == "recorded":
        extra = (
            f"successes {result.get('successes')}/{result.get('concurrent_requests')}, "
            f"'already being processed' seen: {result.get('already_processing_seen')}, "
            f"chain survived: {result.get('chain_survived')}"
        )
    elif result.get("why"):
        extra = str(result["why"])
    elif result.get("env"):
        extra = f"set {result['env']}"
    return f"  {item:4} {status:15} {extra}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help=f"comma-separated subset of {','.join(ITEMS)}")
    ap.add_argument("--allow-writes", action="store_true", help="allow V19 to create/delete records")
    ap.add_argument("--probe-v3", action="store_true", help="fire concurrent refreshes to record V3")
    ap.add_argument("--allow-risky", action="store_true", help="run V3 even if rotation is on (may revoke)")
    ap.add_argument(
        "--out", type=Path, help="evidence file (default: tests/fixtures/recorded/verification/<date>.json)"
    )
    args = ap.parse_args(argv)

    settings = load_settings()
    if not settings.configured:
        print(f"not configured: {settings.reason}. See tests/sandbox/README.md", file=sys.stderr)
        return 2
    wanted = [i.strip() for i in args.only.split(",")] if args.only else list(ITEMS)
    unknown = [i for i in wanted if i not in ITEMS]
    if unknown:
        print(f"unknown items: {unknown}; choose from {ITEMS}", file=sys.stderr)
        return 2
    settings.prime()
    probe = Probe(settings, args)
    with probe.client() as sf:
        tok = sf.auth.token()
        probe.remember_secrets(tok)
        api_version = sf.api_version

    results: dict[str, Any] = {}
    for item in ITEMS:
        if item not in wanted:
            continue
        fn = getattr(probe, item.lower())
        try:
            results[item] = fn()
        except esf.SalesforceError as exc:
            results[item] = {"status": "error", **error_summary(exc)}
        except Exception as exc:  # keep going; record the failure shape only
            results[item] = {
                "status": "error",
                "exception": type(exc).__name__,
                "detail": redact_text(str(exc), 200),
            }
        print(summary_line(item, results[item]), flush=True)

    now = datetime.now(timezone.utc)
    evidence = {
        "schema": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "library_version": esf.__version__,
        "api_version": api_version,
        "python": platform.python_version(),
        "login_mode": settings.mode,
        "flow": type(settings.auth()).__name__,
        "org": {
            "org_id_prefix": (tok.org_id or "")[:3] or None,
            "instance_kind": instance_kind(tok.instance_url),
        },
        "items": results,
    }
    text = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    probe.remember_secrets(tok)
    if settings.client_secret:
        probe.secrets.add(settings.client_secret)
    check_clean(text, probe.secrets)
    out = args.out or OUT_DIR / f"{now.date().isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"\nevidence written to {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    print("review it, then commit it and link it from the §21 table / the ADR.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
