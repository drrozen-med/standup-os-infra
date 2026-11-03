"""Acceptance tests for Navigator API v1 (issue #14)."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import actions
import nav
import navigator
import remote


class FakeCCCNoCall(BaseHTTPRequestHandler):
    """Fake CCC that records calls - used to assert proxy is never called."""
    calls = []

    def log_message(self, *a):
        pass

    def _record(self):
        FakeCCCNoCall.calls.append((self.command, self.path))
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        data = json.dumps({"ccc": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_DELETE = do_PUT = _record


class NavV1Acceptance(unittest.TestCase):
    def test_nav_do_argparse_builds_for_every_registry_action(self):
        """(2) 'nav do <name>' argparse builds for every registry action"""
        parser = nav.build_parser()
        for name in sorted(actions.REGISTRY.keys()):
            try:
                args = parser.parse_args(["do", name])
                self.assertEqual(args.command, "do")
                self.assertEqual(args.action, name)
            except SystemExit:
                self.fail(f"argparse failed to build/parse for action {name}")
            except Exception as e:
                self.fail(f"argparse failed for action {name}: {e}")

    def test_post_unknown_action_not_proxied_to_ccc(self):
        """(3) POST /nav/v1/actions/does-not-exist returns 404 unknown_route and CCC proxy never called"""
        tmp = tempfile.TemporaryDirectory()
        try:
            FakeCCCNoCall.calls = []
            ccc = ThreadingHTTPServer(("127.0.0.1", 0), FakeCCCNoCall)
            t = threading.Thread(target=ccc.serve_forever, daemon=True)
            t.start()
            ccc_base = "http://127.0.0.1:%d" % ccc.server_address[1]
            d = Path(tmp.name)
            args = type("A", (), {})()
            args.ccc = ccc_base
            args.state = str(d / "state.json")
            args.pi_dir = str(d / "pi")
            Path(args.pi_dir).mkdir()
            args.orca_dir = None
            args.verbose = False
            os.environ["CCC_NAV_CHANGES"] = str(d / "changes.jsonl")
            os.environ["CCC_NAV_DECISIONS"] = str(d / "decisions.json")
            os.environ["CCC_NAV_ANSWERS"] = str(d / "answers.json")
            navigator.Handler.app = navigator.App(args)
            HOST = "cmd-test.example.com"
            navigator.Handler.remote_cfg = remote.RemoteConfig(HOST, d)
            navigator.Handler.remote_store = remote.Store(d)
            navigator.Handler.limiter = remote.RateLimiter()
            navsrv = ThreadingHTTPServer(("127.0.0.1", 0), navigator.Handler)
            nt = threading.Thread(target=navsrv.serve_forever, daemon=True)
            nt.start()
            port = navsrv.server_address[1]
            import http.client
            # Pair like in GateTests
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/api/remote/pair", body=b"{}", headers={"Host": "127.0.0.1:%d" % port, "Content-Type": "application/json"})
            r = conn.getresponse()
            bodyr = r.read()
            conn.close()
            self.assertEqual(r.status, 200)
            link = json.loads(bodyr)["link"]
            code = link.split("code=", 1)[1]
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", link.split(HOST, 1)[1], headers={"Host": HOST})
            r = conn.getresponse()
            r.read()
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/__pair", body=urllib.parse.urlencode({"code": code}).encode(),
                         headers={"Host": HOST, "Content-Type": "application/x-www-form-urlencoded", "Origin": "https://" + HOST})
            r = conn.getresponse()
            hdrs = dict(r.getheaders())
            r.read()
            conn.close()
            cookie = hdrs.get("Set-Cookie", "").split(";", 1)[0]
            # Now make request as remote
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/nav/v1/actions/does-not-exist", body=b"{}",
                         headers={"Host": HOST, "Content-Type": "application/json",
                                  "Cf-Connecting-Ip": "1.2.3.4", "Cf-Ray": "x",
                                  "Cookie": cookie, "Origin": "https://" + HOST})
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            self.assertEqual(resp.status, 404)
            try:
                data = json.loads(body)
            except Exception:
                self.fail(f"bad json: {body}")
            self.assertFalse(data.get("ok", True))
            err = data.get("error") or {}
            self.assertEqual(err.get("code"), "unknown_route")
            self.assertEqual(len(FakeCCCNoCall.calls), 0, f"CCC was called: {FakeCCCNoCall.calls}")
            navsrv.shutdown()
            navsrv.server_close()
            ccc.shutdown()
            ccc.server_close()
        finally:
            for k in ["CCC_NAV_CHANGES", "CCC_NAV_DECISIONS", "CCC_NAV_ANSWERS"]:
                os.environ.pop(k, None)
            tmp.cleanup()

    def test_second_undo_of_same_change_returns_409(self):
        """(4) a second undo of the same change returns 409"""
        tmp = tempfile.TemporaryDirectory()
        try:
            d = Path(tmp.name)
            args = type("A", (), {})()
            args.ccc = "http://127.0.0.1:1"
            args.state = str(d / "state.json")
            args.pi_dir = str(d / "pi")
            Path(args.pi_dir).mkdir()
            args.orca_dir = None
            args.verbose = False
            os.environ["CCC_NAV_CHANGES"] = str(d / "changes.jsonl")
            os.environ["CCC_NAV_DECISIONS"] = str(d / "decisions.json")
            os.environ["CCC_NAV_ANSWERS"] = str(d / "answers.json")
            navigator.Handler.app = navigator.App(args)
            app = navigator.Handler.app
            res = actions.dispatch(app, "session.rename", {"key": "claude:1", "name": "hello"})
            self.assertTrue(res.get("ok"))
            cid = res.get("change")
            self.assertTrue(cid)
            u1 = actions.dispatch(app, "undo", {"change": cid})
            self.assertTrue(u1.get("ok"))
            try:
                u2 = actions.dispatch(app, "undo", {"change": cid})
                self.fail("second undo should raise")
            except actions.ActionError as e:
                self.assertIn(e.code, ("conflict", "already_undone"))
            except Exception as e:
                self.fail(f"wrong exception: {type(e)} {e}")
        finally:
            for k in ["CCC_NAV_CHANGES", "CCC_NAV_DECISIONS", "CCC_NAV_ANSWERS"]:
                os.environ.pop(k, None)
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
