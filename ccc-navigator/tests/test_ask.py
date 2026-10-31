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


if __name__ == '__main__':
    unittest.main()
