import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import activity
import navigator
import remote


def write_jsonl(path, lines):
    with open(path, 'w') as f:
        for l in lines:
            if isinstance(l, str):
                f.write(l + '\n')
            else:
                f.write(json.dumps(l) + '\n')


class TestAskActivity(unittest.TestCase):
    def test_wrap_up_with_actions_and_older_wrap_up_cleared(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 's.jsonl')
            now = time.time()
            # Older turn's wrap_up followed by a new text prompt -> turn_actions empty for current turn
            lines = [
                {"type": "user", "message": {"role": "user", "content": "first"}, "timestamp": now - 7200},
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_old", "name": "mcp__ccd_turn__wrap_up", 
                     "input": {"ask": "old question", "actions": [{"label": "old1", "message": "m1"}, {"label": "old2"}]}}
                ]}, "timestamp": now - 7100},
                {"type": "user", "message": {"role": "user", "content": "new prompt"}, "timestamp": now - 1000},
            ]
            write_jsonl(p, lines)
            act = activity.read_activity(p)
            self.assertIsNotNone(act)
            self.assertFalse(act['turn_wrapped'])  # wrap_up from old turn not in current turn
            self.assertEqual(act['turn_actions'], [])
            
            # Current turn has wrap_up with ask and actions
            lines2 = [
                {"type": "user", "message": {"role": "user", "content": "hello"}, "timestamp": now - 3600},
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "mcp__ccd_turn__wrap_up", 
                     "input": {"ask": "What should I do?", 
                               "actions": [{"label": "Option A", "message": "do A"}, {"label": "Option B"}]}}
                ]}, "timestamp": now - 100},
            ]
            write_jsonl(p, lines2)
            if p in activity._cache:
                del activity._cache[p]
            act = activity.read_activity(p)
            self.assertIsNotNone(act)
            self.assertTrue(act['turn_wrapped'])
            self.assertEqual(act['turn_ask'], "What should I do?")
            self.assertEqual(len(act['turn_actions']), 2)
            self.assertEqual(act['turn_actions'][0]['label'], "Option A")
            self.assertEqual(act['turn_actions'][0]['message'], "do A")
            self.assertEqual(act['turn_actions'][1]['label'], "Option B")
            self.assertNotIn('message', act['turn_actions'][1])

    def test_pending_ask_user_question(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 's.jsonl')
            now = time.time()
            # pending AskUserQuestion (no tool_result)
            lines = [
                {"type": "user", "message": {"role": "user", "content": "go"}, "timestamp": now - 1000},
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_q", "name": "AskUserQuestion", 
                     "input": {"questions": [{"question": "Pick one", "options": ["A", "B"]}]}}
                ]}, "timestamp": now - 500},
            ]
            write_jsonl(p, lines)
            if p in activity._cache:
                del activity._cache[p]
            act = activity.read_activity(p)
            self.assertIsNotNone(act)
            self.assertIsNotNone(act['pending_input'])
            self.assertIn('questions', act['pending_input'])
            self.assertEqual(len(act['pending_input']['questions']), 1)
            self.assertIn('options', act['pending_input']['questions'][0])
            
            # once tool_result follows with its id -> pending_input None
            lines2 = [
                {"type": "user", "message": {"role": "user", "content": "go"}, "timestamp": now - 1000},
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_q", "name": "AskUserQuestion", 
                     "input": {"questions": [{"question": "Pick one", "options": ["A", "B"]}]}}
                ]}, "timestamp": now - 500},
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_q", "content": "A"}
                ]}, "timestamp": now - 100},
            ]
            write_jsonl(p, lines2)
            if p in activity._cache:
                del activity._cache[p]
            act = activity.read_activity(p)
            self.assertIsNotNone(act)
            self.assertIsNone(act['pending_input'])


HOST = "cmd-test.example.com"
NOW = 2_000_000_000


class HandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        state = navigator.State(Path(cls.tmp.name) / "state.json")
        cls.app = SimpleNamespace(
            args=SimpleNamespace(verbose=False, pi_dir=cls.tmp.name), 
            state=state,
            ccc=SimpleNamespace(base="http://127.0.0.1:9999"),
            deliver=lambda *args, **kwargs: "sent",
            changes=SimpleNamespace(record=lambda *args, **kwargs: "chg1")
        )
        navigator.Handler.app = cls.app
        navigator.Handler.remote_cfg = remote.RemoteConfig(HOST, Path(cls.tmp.name))
        navigator.Handler.remote_store = remote.Store(Path(cls.tmp.name))
        navigator.Handler.limiter = remote.RateLimiter()
        cls.nav = ThreadingHTTPServer(("127.0.0.1", 0), navigator.Handler)
        threading.Thread(target=cls.nav.serve_forever, daemon=True).start()
        cls.port = cls.nav.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.nav.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        navigator.Handler.limiter = remote.RateLimiter()
        # reset app methods
        navigator.Handler.app.deliver = lambda *args, **kwargs: "sent"
        navigator.Handler.app.changes = SimpleNamespace(record=lambda *args, **kwargs: "chg1")

    def req(self, method, path, cookie=None, body=None, origin=None, ip="9.9.9.9", form=None, content_type=None):
        h = {"Host": HOST}
        h.update({"Cf-Connecting-Ip": ip, "Cf-Ray": "abc"})
        if cookie:
            h["Cookie"] = cookie
        if origin:
            h["Origin"] = origin
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            h["Content-Type"] = content_type or "application/x-www-form-urlencoded"
        elif body is not None:
            if content_type == "text/plain" or (content_type is None and isinstance(body, str)):
                data = body.encode() if isinstance(body, str) else body
                h["Content-Type"] = content_type or "text/plain"
            else:
                data = json.dumps(body).encode()
                h["Content-Type"] = content_type or "application/json"
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        c.close()
        return out

    def pair(self):
        # Create pairing locally
        code, exp = navigator.Handler.remote_store.create_pairing()
        st, hdrs, page = self.req("GET", "/__pair?code=%s" % code)
        self.assertEqual(st, 200)
        st, hdrs, _ = self.req("POST", "/__pair", form={"code": code}, origin="https://" + HOST)
        self.assertEqual(st, 303)
        cookie = hdrs["Set-Cookie"]
        return cookie.split(";", 1)[0]

    def test_ask_answer_wrap_failed(self):
        cookie = self.pair()
        navigator.Handler.app.deliver = lambda key, text: "failed: x"
        st, hdrs, body = self.req("POST", "/api/ask/answer", cookie=cookie, 
                                   body={"key": "claude:abc123", "source": "wrap", "text": "hi"},
                                   origin="https://" + HOST)
        self.assertNotEqual(st, 200)
        res = json.loads(body)
        self.assertFalse(res.get("ok", False)) or self.assertIn("error", res)

    def test_ask_answer_wrap_sent(self):
        cookie = self.pair()
        navigator.Handler.app.deliver = lambda key, text: "sent"
        st, hdrs, body = self.req("POST", "/api/ask/answer", cookie=cookie,
                                   body={"key": "claude:abc123", "source": "wrap", "text": "hi"},
                                   origin="https://" + HOST)
        self.assertEqual(st, 200)
        res = json.loads(body)
        self.assertTrue(res.get("ok"))

    def test_ask_answer_content_type_text_plain_415(self):
        cookie = self.pair()
        navigator.Handler.app.deliver = lambda key, text: "sent"
        st, hdrs, body = self.req("POST", "/api/ask/answer", cookie=cookie,
                                   body="not json", origin="https://" + HOST,
                                   content_type="text/plain")
        self.assertEqual(st, 415)

    def test_answer_to_moved_session_is_handed_off(self):
        cookie = self.pair()
        navigator.Handler.app.deliver = lambda key, text: "moved"
        navigator.Handler.app.moved_reply = lambda sid, text: {"ok": False, "error": "moved", "text": text, "sid": sid}
        st, _, body = self.req("POST", "/api/ask/answer", cookie=cookie,
                               body={"key": "claude:abc123", "source": "wrap", "text": "Yes"},
                               origin="https://" + HOST)
        self.assertEqual(st, 409)
        self.assertEqual(json.loads(body), {"ok": False, "error": "moved", "text": "Yes", "sid": "abc123"})

    def test_reply_box_to_moved_session_never_reaches_ccc(self):
        cookie = self.pair()
        navigator.Handler.app.moved = lambda sid: sid == "abc123"
        navigator.Handler.app.moved_reply = lambda sid, text: {"ok": False, "error": "moved", "text": text}
        st, _, body = self.req("POST", "/api/inject-input", cookie=cookie,
                               body={"session_id": "abc123", "text": "hold on", "mode": "send"},
                               origin="https://" + HOST)
        self.assertEqual(st, 409)
        self.assertEqual(json.loads(body)["text"], "hold on")
        # a session with one copy goes on to CCC (unreachable here, so the proxy answers 502)
        st, _, _ = self.req("POST", "/api/inject-input", cookie=cookie,
                            body={"session_id": "other", "text": "x", "mode": "send"},
                            origin="https://" + HOST)
        self.assertEqual(st, 502)


class MovedSessionTests(unittest.TestCase):
    """A session's log copied into a second project folder (the session moved): read the newest
    copy, and never resume it through CCC, which picks the old copy and forks the session."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "old").mkdir(); (root / "new").mkdir(); (root / "solo").mkdir()
        self.old, self.new = root / "old" / "s1.jsonl", root / "new" / "s1.jsonl"
        self.old.write_text("{}\n"); self.new.write_text("{}\n")
        os.utime(self.old, (time.time() - 3600,) * 2)
        (root / "solo" / "s2.jsonl").write_text("{}\n")
        self.copies = navigator.transcript_copies(str(root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_sessions_with_two_copies_are_listed_newest_first(self):
        self.assertEqual(self.copies, {"s1": [str(self.new), str(self.old)]})

    def test_rows_point_at_newest_copy(self):
        rows = [{"session_id": "s1", "engine": "claude", "jsonl_path": str(self.old)},
                {"session_id": "s2", "engine": "claude", "jsonl_path": "/x/s2.jsonl"},
                {"session_id": "s1", "engine": "codex", "jsonl_path": str(self.old)}]
        out = navigator.use_newest_copy(rows, self.copies)
        self.assertEqual(out[0]["jsonl_path"], str(self.new))
        self.assertEqual(out[0]["ccc_jsonl_path"], str(self.old))
        self.assertEqual(out[1], rows[1])
        self.assertEqual(out[2], rows[2])

    def test_ccc_patch_detected_on_newest_version_only(self):
        root = Path(self.tmp.name)
        for ver, body in (("5.9.0", "# standup-os patch: newest copy\n"), ("5.10.0", "plain\n")):
            (root / ver).mkdir()
            (root / ver / "server.py").write_text(body)
        self.assertFalse(navigator.ccc_patched(str(root / "*" / "server.py")))   # 5.10 is newest and unpatched
        (root / "5.10.0" / "server.py").write_text("x\n# standup-os patch: newest copy\n")
        self.assertTrue(navigator.ccc_patched(str(root / "*" / "server.py")))
        self.assertFalse(navigator.ccc_patched(str(root / "none" / "*.py")))

    def test_deliver_refuses_moved_session(self):
        fake = SimpleNamespace(args=SimpleNamespace(pi_dir=self.tmp.name), moved=lambda sid: sid == "s1",
                               ccc=SimpleNamespace(base="http://127.0.0.1:9"))
        self.assertEqual(navigator.App.deliver(fake, "claude:s1", "hi"), "moved")
        self.assertTrue(navigator.App.deliver(fake, "claude:s2", "hi").startswith("failed"))


if __name__ == '__main__':
    unittest.main()
