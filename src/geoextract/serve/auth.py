"""Single-password gate for the served map + API.

One password per deployment, no username — set ``GEOEXTRACT_WEB_PASSWORD``. Unset means
the gate is OFF (local development); set means every route needs one of:

* a valid session cookie, obtained by posting the password to ``/login`` (browsers), or
* the password as a bearer token: ``Authorization: Bearer <password>`` — or the
  ``X-Geo-Password`` header — so scripts (curl, requests, httr) keep working without a
  browser session.

Failed attempts are throttled per client address: after ``MAX_ATTEMPTS`` inside
``WINDOW`` seconds the address is locked out for ``LOCKOUT`` seconds. State is in-process
(the API is a single uvicorn process); a restart clears it.

Sessions are signed with HMAC-SHA256 over ``GEOEXTRACT_WEB_SECRET`` — stdlib only, no new
dependency. An unset secret is generated at import, which invalidates sessions on restart;
set it to keep logins across restarts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time

COOKIE_NAME = "geo_session"
SESSION_TTL = 7 * 24 * 3600          # a week
MAX_ATTEMPTS = 5                      # failures allowed per WINDOW
WINDOW = 300                          # 5 minutes
LOCKOUT = 900                         # 15 minutes locked after MAX_ATTEMPTS

# address -> (failure timestamps, locked_until)
_attempts: dict[str, list[float]] = {}
_locked: dict[str, float] = {}

_FALLBACK_SECRET = secrets.token_hex(32)


def password() -> str:
    """The configured password, or "" when the gate is disabled."""
    return os.environ.get("GEOEXTRACT_WEB_PASSWORD", "")


def enabled() -> bool:
    return bool(password())


def _secret() -> bytes:
    return (os.environ.get("GEOEXTRACT_WEB_SECRET") or _FALLBACK_SECRET).encode()


# --- sessions ---------------------------------------------------------------------------

def _sign(data: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(_secret(), data, hashlib.sha256).digest()).decode().rstrip("=")


def make_session() -> str:
    body = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + SESSION_TTL}).encode()).decode().rstrip("=")
    return f"{body}.{_sign(body.encode())}"


def valid_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(body.encode())):
        return False
    try:
        pad = "=" * (-len(body) % 4)
        exp = json.loads(base64.urlsafe_b64decode(body + pad))["exp"]
    except (ValueError, KeyError, TypeError):
        # untrusted cookie: bad base64 / json / missing claim all mean "no session"
        return False
    return time.time() < float(exp)


# --- throttling -------------------------------------------------------------------------

def client_ip(headers, fallback: str) -> str:
    """Real client address behind the shared Caddy proxy."""
    fwd = headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else fallback


def lock_seconds_left(ip: str) -> int:
    until = _locked.get(ip, 0.0)
    left = until - time.time()
    if left <= 0:
        _locked.pop(ip, None)
        return 0
    return int(left) + 1


def record_failure(ip: str) -> None:
    now = time.time()
    hits = [t for t in _attempts.get(ip, []) if now - t < WINDOW]
    hits.append(now)
    _attempts[ip] = hits
    if len(hits) >= MAX_ATTEMPTS:
        _locked[ip] = now + LOCKOUT
        _attempts.pop(ip, None)


def clear_failures(ip: str) -> None:
    _attempts.pop(ip, None)
    _locked.pop(ip, None)


def check_password(candidate: str, ip: str) -> bool:
    """Constant-time comparison plus throttling bookkeeping."""
    ok = bool(candidate) and hmac.compare_digest(candidate, password())
    if ok:
        clear_failures(ip)
    else:
        record_failure(ip)
    return ok


def bearer_password(headers) -> str:
    """Password offered by a non-browser client, if any."""
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("x-geo-password", "").strip()


# --- login page -------------------------------------------------------------------------

LOGIN_HTML = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>geoextract — Anmeldung</title>
<style>
  :root { color-scheme: light dark; --bg:#f6f7f8; --card:#fff; --fg:#1b1d1f; --mut:#6b7176;
          --line:#d9dcdf; --accent:#2c5c8f; --err:#a3242b; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#16181a; --card:#1f2225; --fg:#e8eaec; --mut:#9aa1a7; --line:#31363a;
            --accent:#6aa3dc; --err:#e5787e; }
  }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100dvh; display:grid; place-items:center; background:var(--bg);
         color:var(--fg); font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; padding:24px; }
  form { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:28px;
         width:100%; max-width:360px; box-shadow:0 1px 3px rgba(0,0,0,.06); }
  h1 { margin:0 0 4px; font-size:19px; }
  p.sub { margin:0 0 20px; color:var(--mut); font-size:13px; }
  label { display:block; font-size:13px; margin-bottom:6px; color:var(--mut); }
  input { width:100%; padding:10px 12px; font-size:15px; border:1px solid var(--line);
          border-radius:8px; background:var(--bg); color:var(--fg); }
  input:focus { outline:2px solid var(--accent); outline-offset:1px; border-color:var(--accent); }
  button { width:100%; margin-top:16px; padding:10px 12px; font-size:15px; font-weight:600;
           border:0; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; }
  button:hover { filter:brightness(1.08); }
  .err { margin:14px 0 0; padding:9px 11px; border-radius:8px; font-size:13px;
         background:color-mix(in srgb, var(--err) 12%, transparent); color:var(--err); }
</style>
<form method="post" action="/login">
  <h1>geoextract</h1>
  <p class="sub">Unternehmen an Standorten in Deutschland</p>
  <label for="pw">Passwort</label>
  <input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
  <button type="submit">Anmelden</button>
  __MESSAGE__
</form>
"""


def login_page(message: str = "") -> str:
    # messages are internal constants today; escape anyway so that stays true
    block = f'<p class="err">{html.escape(message)}</p>' if message else ""
    return LOGIN_HTML.replace("__MESSAGE__", block)
