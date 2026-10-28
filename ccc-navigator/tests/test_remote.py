"""Phone access (issue #5): pairing, sessions, rate limit, and the HTTP gate."""
import http.client
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import navigator  # noqa: E402
import remote  # noqa: E402

HOST = "cmd-test.example.com"
NOW = 2_000_000_000


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = remote.Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_pairing_is_single_use_and_hashed(self):
        code, exp = self.store.create_pairing(now=NOW)
        self.assertEqual(exp, NOW + remote.PAIR_TTL)
        raw = (Path(self.tmp.name) / "pairings.json").read_text()
        self.assertNotIn(code, raw)
        sid = self.store.redeem_pairing(code, "iPhone", now=NOW + 10)
        self.assertTrue(sid)
        self.assertIsNone(self.store.redeem_pairing(code, "iPhone", now=NOW + 11))

    def test_expired_and_malformed_codes_fail(self):
        code, _ = self.store.create_pairing(now=NOW)
        self.assertIsNone(self.store.redeem_pairing(code, now=NOW + remote.PAIR_TTL + 1))
        for bad in ("", "short", "x" * 100, "../../etc", "abc def ghi jkl mno pqr"):
            self.assertIsNone(self.store.redeem_pairing(bad, now=NOW))

    def test_at_most_three_live_codes(self):
        codes = [self.store.create_pairing(now=NOW + i)[0] for i in range(5)]
        self.assertIsNone(self.store.redeem_pairing(codes[0], now=NOW + 6))
        self.assertTrue(self.store.redeem_pairing(codes[4], now=NOW + 6))

    def test_session_cookie_check_expiry_and_files_are_private(self):
        sid = self.store.create_session("iPhone", now=NOW)
        self.assertTrue(self.store.check_session("nav_session=%s" % sid, now=NOW + 5))
        self.assertIsNone(self.store.check_session("nav_session=nope", now=NOW + 5))
        self.assertIsNone(self.store.check_session("", now=NOW + 5))
        self.assertIsNone(self.store.check_session("nav_session=%s" % sid, now=NOW + remote.SESSION_TTL + 1))
        f = Path(self.tmp.name) / "sessions.json"
        self.assertNotIn(sid, f.read_text())
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)

    def test_revoke_and_list(self):
        a = self.store.create_session("a", now=NOW)
        self.store.create_session("b", now=NOW)
        listed = self.store.list_sessions(now=NOW)
        self.assertEqual(len(listed), 2)
        self.assertTrue(all(len(x["id"]) == 8 for x in listed))
        self.assertEqual(self.store.revoke(listed[0]["id"]), 1)
        self.assertEqual(self.store.revoke("all"), 1)
        self.assertIsNone(self.store.check_session("nav_session=%s" % a, now=NOW))

    def test_access_log_never_has_query_strings(self):
        self.store.access_log("1.2.3.4", "GET", "/__pair?code=SECRETSECRETSECRETSECRET", 403)
        self.assertNotIn("SECRET", (Path(self.tmp.name) / "remote-access.log").read_text())


class HelperTests(unittest.TestCase):
    def test_is_remote(self):
        self.assertTrue(remote.is_remote({"Host": HOST}, HOST))
        self.assertTrue(remote.is_remote({"Host": HOST.upper() + ":443"}, HOST))
        self.assertTrue(remote.is_remote({"Host": "127.0.0.1:8095", "Cf-Ray": "x"}, HOST))
        self.assertTrue(remote.is_remote({"Host": "127.0.0.1:8095", "Cf-Connecting-Ip": "1.1.1.1"}, ""))
        self.assertFalse(remote.is_remote({"Host": "127.0.0.1:8095"}, HOST))
        self.assertFalse(remote.is_remote({"Host": "[::1]:8095"}, HOST))

    def test_rate_limiter(self):
        rl = remote.RateLimiter(per_key=2, overall=3, window=100)
        for _ in range(2):
            self.assertTrue(rl.allow("a", now=NOW))
            rl.fail("a", now=NOW)
        self.assertFalse(rl.allow("a", now=NOW))
        self.assertTrue(rl.allow("b", now=NOW))
        rl.fail("b", now=NOW)
        self.assertFalse(rl.allow("c", now=NOW))       # overall cap
        self.assertTrue(rl.allow("a", now=NOW + 101))  # window passed


class FakeCCC(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        data = json.dumps({"ccc": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_DELETE = _ok


class GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ccc = ThreadingHTTPServer(("127.0.0.1", 0), FakeCCC)
        threading.Thread(target=cls.ccc.serve_forever, daemon=True).start()
        state = navigator.State(Path(cls.tmp.name) / "state.json")
        navigator.Handler.app = SimpleNamespace(
            args=SimpleNamespace(verbose=False, pi_dir=cls.tmp.name), state=state,
            ccc=SimpleNamespace(base="http://127.0.0.1:%d" % cls.ccc.server_address[1]))
        navigator.Handler.remote_cfg = remote.RemoteConfig(HOST, Path(cls.tmp.name))
        navigator.Handler.remote_store = remote.Store(Path(cls.tmp.name))
        navigator.Handler.limiter = remote.RateLimiter()
        cls.nav = ThreadingHTTPServer(("127.0.0.1", 0), navigator.Handler)
        threading.Thread(target=cls.nav.serve_forever, daemon=True).start()
        cls.port = cls.nav.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.nav.shutdown()
        cls.ccc.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        navigator.Handler.limiter = remote.RateLimiter()

    def req(self, method, path, remote_=True, cookie=None, body=None, origin=None, ip="9.9.9.9", form=None):
        h = {"Host": HOST if remote_ else "127.0.0.1:%d" % self.port}
        if remote_:
            h.update({"Cf-Connecting-Ip": ip, "Cf-Ray": "abc"})
        if cookie:
            h["Cookie"] = cookie
        if origin:
            h["Origin"] = origin
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        c.close()
        return out

    def pair(self):
        st, _, body = self.req("POST", "/api/remote/pair", remote_=False, body={})
        self.assertEqual(st, 200)
        link = json.loads(body)["link"]
        self.assertTrue(link.startswith("https://%s/__pair?code=" % HOST))
        code = link.split("code=", 1)[1]
        st, _, page = self.req("GET", link.split(HOST, 1)[1])
        self.assertEqual(st, 200)
        self.assertIn(b'method="post"', page)
        st, hdrs, _ = self.req("POST", "/__pair", form={"code": code}, origin="https://" + HOST)
        self.assertEqual(st, 303)
        self.assertEqual(hdrs["Location"], "/")
        cookie = hdrs["Set-Cookie"]
        for flag in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
            self.assertIn(flag, cookie)
        return cookie.split(";", 1)[0], code

    def test_unpaired_remote_gets_nothing(self):
        for method, path in (("GET", "/"), ("GET", "/api/notes"), ("GET", "/api/conversations/x"),
                             ("POST", "/api/inject-input"), ("POST", "/api/pi/send"), ("POST", "/api/hide"),
                             ("DELETE", "/api/anything")):
            st, hdrs, body = self.req(method, path, body={} if method == "POST" else None)
            self.assertEqual(st, 401, (method, path))
            self.assertNotIn(b"ccc", body)
            self.assertEqual(hdrs.get("X-Frame-Options"), "DENY")
            self.assertIn("max-age", hdrs.get("Strict-Transport-Security", ""))
            self.assertEqual(hdrs.get("Referrer-Policy"), "same-origin")  # no-referrer makes browsers send Origin: null

    def test_pair_then_use_then_reuse_fails(self):
        cookie, code = self.pair()
        st, _, body = self.req("GET", "/api/conversations/abc", cookie=cookie)
        self.assertEqual((st, json.loads(body)["ccc"]), (200, True))
        st, _, _ = self.req("GET", "/api/notes", cookie=cookie)
        self.assertEqual(st, 200)
        st, _, _ = self.req("POST", "/__pair", form={"code": code}, origin="https://" + HOST)
        self.assertEqual(st, 403)

    def test_link_previews_cannot_spend_the_code(self):
        st, _, body = self.req("POST", "/api/remote/pair", remote_=False, body={})
        code = json.loads(body)["link"].split("code=", 1)[1]
        for _ in range(3):  # prefetchers and link previews only GET
            self.assertEqual(self.req("GET", "/__pair?code=" + code, ip="74.125.208.194")[0], 200)
        self.assertEqual(self.req("POST", "/__pair", form={"code": code})[0], 403)  # no Origin: refused, code kept
        st, hdrs, _ = self.req("POST", "/__pair", form={"code": code}, origin="https://" + HOST)
        self.assertEqual(st, 303)
        self.assertIn("nav_session=", hdrs["Set-Cookie"])

    def test_remote_post_needs_our_origin(self):
        cookie, _ = self.pair()
        st, _, _ = self.req("POST", "/api/note", cookie=cookie, body={"key": "claude:x", "text": "hi"},
                            origin="https://evil.example")
        self.assertEqual(st, 403)
        st, _, _ = self.req("POST", "/api/note", cookie=cookie, body={"key": "claude:x", "text": "hi"})
        self.assertEqual(st, 403)  # no Origin at all
        st, _, _ = self.req("POST", "/api/note", cookie=cookie, body={"key": "claude:x", "text": "hi"},
                            origin="https://" + HOST)
        self.assertEqual(st, 200)

    def test_pairing_endpoints_are_local_only(self):
        cookie, _ = self.pair()
        for method, path in (("POST", "/api/remote/pair"), ("GET", "/api/remote/sessions"),
                             ("POST", "/api/remote/revoke")):
            st, _, _ = self.req(method, path, cookie=cookie, body={} if method == "POST" else None,
                                origin="https://" + HOST)
            self.assertEqual(st, 403, path)

    def test_rate_limit_on_bad_codes(self):
        o = "https://" + HOST
        codes = [self.req("POST", "/__pair", form={"code": "x" * 30}, origin=o, ip="5.5.5.5")[0] for _ in range(6)]
        self.assertEqual(codes[:5], [403] * 5)
        self.assertEqual(codes[5], 429)
        self.assertEqual(self.req("POST", "/__pair", form={"code": "x" * 30}, origin=o, ip="6.6.6.6")[0], 403)

    def test_revoke_all_signs_out(self):
        cookie, _ = self.pair()
        st, _, body = self.req("POST", "/api/remote/revoke", remote_=False, body={"id": "all"})
        self.assertEqual(st, 200)
        self.assertGreaterEqual(json.loads(body)["revoked"], 1)
        self.assertEqual(self.req("GET", "/api/notes", cookie=cookie)[0], 401)

    def test_local_use_unchanged(self):
        st, hdrs, _ = self.req("GET", "/api/notes", remote_=False)
        self.assertEqual(st, 200)
        self.assertNotIn("X-Frame-Options", hdrs)
        st, _, _ = self.req("POST", "/api/note", remote_=False, body={"key": "claude:y", "text": "x"},
                            origin="https://evil.example")
        self.assertEqual(st, 403)

    def test_log_has_no_codes(self):
        self.pair()
        log = (Path(self.tmp.name) / "remote-access.log").read_text()
        self.assertIn("/__pair", log)
        self.assertNotIn("code=", log)

    def test_off_when_no_public_host(self):
        old = navigator.Handler.remote_cfg
        navigator.Handler.remote_cfg = remote.RemoteConfig("", Path(self.tmp.name))
        try:
            h = {"Host": "127.0.0.1:%d" % self.port, "Cf-Ray": "x"}
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            c.request("GET", "/api/notes", headers=h)
            self.assertEqual(c.getresponse().status, 403)
            c.close()
        finally:
            navigator.Handler.remote_cfg = old


if __name__ == "__main__":
    unittest.main()
