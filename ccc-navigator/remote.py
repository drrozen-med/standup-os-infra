"""Remote (phone) access to the navigator through a Cloudflare Tunnel.

Every request that arrives through the tunnel must carry a session cookie.
Phones get one by opening a one-time pairing link made on the Mac
(`nav remote pair`). There is no standing password: codes are single-use and
expire after 15 minutes; sessions last 30 days and can be revoked. Only
sha256 hashes of codes and session ids are stored. See issue #5.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path

COOKIE = "nav_session"
PAIR_TTL = 15 * 60
SESSION_TTL = 30 * 86400
MAX_PAIRINGS = 3
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
CF_HEADERS = ("Cf-Connecting-Ip", "Cf-Ray", "Cf-Visitor")
_lock = threading.Lock()


@dataclass
class RemoteConfig:
    public_host: str
    state_dir: Path

    @property
    def enabled(self):
        return bool(self.public_host)


def load_config():
    host = (os.environ.get("CCC_NAV_PUBLIC_HOST") or "").strip().lower()
    state = Path(os.path.expanduser(os.environ.get("CCC_NAV_REMOTE_DIR", "~/.ccc-navigator")))
    return RemoteConfig(public_host=host, state_dir=state)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _host_of(headers):
    host = (headers.get("Host") or "").strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_remote(headers, public_host):
    """A request is remote when it names the public host or came through Cloudflare."""
    if public_host and _host_of(headers) == public_host:
        return True
    return any(headers.get(h) for h in CF_HEADERS)


def client_ip(headers):
    return (headers.get("Cf-Connecting-Ip") or "unknown").strip()[:64]


class Store:
    """Pairing codes and sessions, hashed, in two 0600 json files."""

    def __init__(self, state_dir):
        self.dir = Path(state_dir)
        self.pair_file = self.dir / "pairings.json"
        self.sess_file = self.dir / "sessions.json"
        self.log_file = self.dir / "remote-access.log"

    def _read(self, path):
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, path, data):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    # ---- pairing
    def create_pairing(self, ttl=PAIR_TTL, now=None):
        now = now or time.time()
        code = secrets.token_urlsafe(24)
        with _lock:
            pairs = {h: e for h, e in self._read(self.pair_file).items() if e > now}
            pairs[_hash(code)] = now + ttl
            for h, _ in sorted(pairs.items(), key=lambda kv: kv[1])[:-MAX_PAIRINGS]:
                pairs.pop(h)
            self._write(self.pair_file, pairs)
        return code, now + ttl

    def redeem_pairing(self, code, ua="", now=None):
        """Session id for a valid, unexpired, unused code; None otherwise. Single use."""
        now = now or time.time()
        if not code or not CODE_RE.match(code):
            return None
        want = _hash(code)
        with _lock:
            pairs = self._read(self.pair_file)
            hit = None
            for h, exp in pairs.items():
                if hmac.compare_digest(h, want):
                    hit = (h, exp)
            if not hit:
                return None
            pairs.pop(hit[0])
            self._write(self.pair_file, pairs)
            if hit[1] <= now:
                return None
        return self.create_session(ua, now=now)

    # ---- sessions
    def create_session(self, label="", now=None):
        now = now or time.time()
        sid = secrets.token_urlsafe(32)
        with _lock:
            sess = {h: s for h, s in self._read(self.sess_file).items() if s.get("expires", 0) > now}
            sess[_hash(sid)] = {"created": now, "last_seen": now, "expires": now + SESSION_TTL,
                                "label": (label or "")[:80]}
            self._write(self.sess_file, sess)
        return sid

    def check_session(self, cookie_header, now=None):
        """Hash of the valid session named by the cookie, else None."""
        now = now or time.time()
        try:
            jar = SimpleCookie(cookie_header or "")
        except Exception:
            return None
        morsel = jar.get(COOKIE)
        if not morsel or not morsel.value:
            return None
        want = _hash(morsel.value)
        with _lock:
            sess = self._read(self.sess_file)
            hit = next((h for h in sess if hmac.compare_digest(h, want)), None)
            if not hit or sess[hit].get("expires", 0) <= now:
                return None
            if now - sess[hit].get("last_seen", 0) > 60:
                sess[hit]["last_seen"] = now
                self._write(self.sess_file, sess)
        return hit

    def revoke(self, which):
        with _lock:
            sess = self._read(self.sess_file)
            if which == "all":
                n = len(sess)
                sess = {}
            else:
                gone = [h for h in sess if which and h.startswith(which)]
                n = len(gone)
                for h in gone:
                    sess.pop(h)
            self._write(self.sess_file, sess)
        return n

    def list_sessions(self, now=None):
        now = now or time.time()
        sess = self._read(self.sess_file)
        return [{"id": h[:8], "label": s.get("label", ""), "created": s.get("created"),
                 "last_seen": s.get("last_seen"), "expires": s.get("expires")}
                for h, s in sorted(sess.items(), key=lambda kv: -kv[1].get("last_seen", 0))
                if s.get("expires", 0) > now]

    def access_log(self, ip, method, path, status):
        line = "%s %s %s %s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), ip, method, path.split("?", 1)[0][:200], status)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as fh:
                fh.write(line)
        except OSError:
            pass


class RateLimiter:
    """Failed pairing attempts: per-IP and overall caps inside a sliding window."""

    def __init__(self, per_key=5, overall=20, window=15 * 60):
        self.per_key, self.overall, self.window = per_key, overall, window
        self.fails = []  # (time, key)
        self.lock = threading.Lock()

    def _trim(self, now):
        self.fails = [(t, k) for t, k in self.fails if now - t < self.window]

    def allow(self, key, now=None):
        now = now or time.time()
        with self.lock:
            self._trim(now)
            if len(self.fails) >= self.overall:
                return False
            return sum(1 for _, k in self.fails if k == key) < self.per_key

    def fail(self, key, now=None):
        now = now or time.time()
        with self.lock:
            self._trim(now)
            self.fails.append((now, key))


SECURITY_HEADERS = (
    ("Strict-Transport-Security", "max-age=31536000"),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    # same-origin, not no-referrer: with no-referrer browsers send "Origin: null" on POSTs, which the
    # exact-Origin check must then refuse, so no write from the phone (or the pairing button) could work
    ("Referrer-Policy", "same-origin"),
    ("Cache-Control", "no-store"),
)


def session_cookie(sid):
    # Lax, not Strict: Strict cookies are withheld when the page is opened from another app (a tapped link,
    # the home-screen icon), which signs the phone out. Writes stay protected by the exact-Origin check.
    return "%s=%s; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=%d" % (COOKIE, sid, SESSION_TTL)


PAIR_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Navigator</title><style>body{font:16px system-ui,sans-serif;background:#0f1020;color:#e8e8f0;display:flex;min-height:90vh;align-items:center;justify-content:center;margin:0;padding:24px}
div{max-width:360px}h1{font-size:20px}p{color:#a0a0b8;line-height:1.5}code{background:#22233a;padding:2px 6px;border-radius:4px}</style></head>
<body><div><h1>%s</h1><p>%s</p></div></body></html>"""


def page(title, text):
    return (PAIR_PAGE % (title, text)).encode()


def confirm_page(code):
    """Opening the link only shows a button; pressing it (a POST) uses the code. Link previews and
    prefetchers only GET, so they can no longer spend the one-time code before the owner does."""
    form = ('<form method="post" action="/__pair"><input type="hidden" name="code" value="%s">'
            '<button type="submit" style="font:600 17px system-ui;padding:14px 22px;border-radius:12px;border:0;'
            'background:#5b54e8;color:#fff;width:100%%">Pair this phone</button></form>') % code
    return page("Pair this phone", "This gives this phone access to the navigator for 30 days.</p>" + form + "<p>")
