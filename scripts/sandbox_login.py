#!/usr/bin/env python3
"""Log in to a sandbox org once and save the tokens for ``tests/sandbox`` and verify_sandbox.py.

Usage::

    export ESF_SANDBOX_DOMAIN=https://<mydomain>--<sandbox>.sandbox.my.salesforce.com   # or "test"
    export ESF_SANDBOX_CLIENT_ID=<consumer key>
    export ESF_SANDBOX_CLIENT_SECRET=<consumer secret>      # optional for PKCE-only public apps
    export ESF_SANDBOX_REDIRECT_URI=http://localhost:8765/callback   # must match the app's callback
    uv run python scripts/sandbox_login.py

It prints (and tries to open) the authorization URL. After you log in and approve, Salesforce
redirects to ``ESF_SANDBOX_REDIRECT_URI``:

- for ``http://localhost:<port>/...`` the script listens on that port and catches the code;
- otherwise (or with ``--paste``) paste the full URL you were redirected to (the page itself
  may fail to load; that's fine).

The code is exchanged with PKCE (``sf.auth.exchange_code``) and the tokens are saved in the
SQLite token store (``ESF_SANDBOX_TOKEN_DB``, default ``.esf-sandbox/tokens.sqlite3``, mode
0600) under ``ESF_SANDBOX_TOKEN_KEY``. No token is printed. You don't need
``ESF_SANDBOX_REFRESH_TOKEN`` afterwards; with rotation on the store is the only place that
knows the current refresh token.
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.sandbox.settings import load_settings  # noqa: E402

import even_simpler_salesforce as esf  # noqa: E402

# api: REST calls; refresh_token: offline access; openid + id: /services/oauth2/userinfo.
SCOPES = ("api", "refresh_token", "openid", "id")


def _wait_for_callback(redirect_uri: str, timeout: float) -> str:
    parsed = urlparse(redirect_uri)
    got: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if urlparse(self.path).path != (parsed.path or "/"):
                self.send_response(404)
                self.end_headers()
                return
            got["url"] = f"{parsed.scheme}://{parsed.netloc}{self.path}"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"even-simpler-salesforce: login received, you can close this tab.\n")
            done.set()

        def log_message(self, format: str, *args: object) -> None:
            pass  # keep the code out of the terminal

    server = http.server.HTTPServer((parsed.hostname or "localhost", parsed.port or 80), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not done.wait(timeout):
            raise SystemExit(f"no callback on {redirect_uri} within {timeout:.0f}s")
    finally:
        server.shutdown()
    return got["url"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--paste", action="store_true", help="paste the redirect URL instead of listening")
    ap.add_argument("--no-browser", action="store_true", help="only print the URL")
    ap.add_argument("--timeout", type=float, default=300, help="seconds to wait for the callback")
    args = ap.parse_args(argv)

    s = load_settings()
    if not (s.domain and s.client_id):
        print(
            "set ESF_SANDBOX_DOMAIN and ESF_SANDBOX_CLIENT_ID (see tests/sandbox/README.md)", file=sys.stderr
        )
        return 2
    flow = s.flow()
    start = flow.authorization_url(prompt="login consent", scopes=SCOPES)
    print("Open this URL, log in to the sandbox and approve the app:\n")
    print(f"  {start.url}\n")
    if not args.no_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(start.url)

    local = urlparse(s.redirect_uri)
    if not args.paste and local.scheme == "http" and local.hostname in ("localhost", "127.0.0.1"):
        print(f"Waiting for the redirect on {s.redirect_uri} ...")
        redirected = _wait_for_callback(s.redirect_uri, args.timeout)
    else:
        redirected = input("Paste the full URL you were redirected to: ").strip()
    params = parse_qs(urlparse(redirected).query)
    if "error" in params:
        print(f"authorization failed: {params['error'][0]}: {params.get('error_description', [''])[0]}")
        return 1
    if params.get("state", [""])[0] != start.state:
        print("state mismatch: ignoring this redirect (start again)", file=sys.stderr)
        return 1
    code = params.get("code", [""])[0]
    if not code:
        print("no ?code= in that URL", file=sys.stderr)
        return 1

    store = s.sync_store()
    with esf.Salesforce(flow, store=store, **s.client_kwargs()) as sf:
        token = sf.auth.exchange_code(flow, code, code_verifier=start.code_verifier)
        info = sf.auth.userinfo()
    if s.refresh_token:  # don't let a stale env value overwrite this login on the next run
        import hashlib

        store.set_meta(f"seeded:{s.token_key}", hashlib.sha256(s.refresh_token.encode()).hexdigest())
        print("note: ESF_SANDBOX_REFRESH_TOKEN is set; it is now ignored (you can unset it)")
    print(
        f"saved token under key {s.token_key!r} in {s.token_db} "
        f"(org {(token.org_id or '???')[:3]}..., refresh token: {'yes' if token.refresh_token else 'NO'}, "
        f"user info ok: {bool(info)})"
    )
    if not token.refresh_token:
        print("warning: no refresh token issued; add the 'refresh_token' scope to the app", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
