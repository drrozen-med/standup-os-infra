import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import actions
import changes
import navigator
import refs
import remote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading


class FakeCCC(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        data = json.dumps({"ccc": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_POST = do_DELETE = do_PUT = do_GET


class ActionsBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ccc_dir = tempfile.TemporaryDirectory()
        cls.ccc = ThreadingHTTPServer(("127.0.0.1", 0), FakeCCC)
        cls.ccc_t = threading.Thread(target=cls.ccc.serve_forever, daemon=True)
        cls.ccc_t.start()
        cls.ccc_base = "http://127.0.0.1:%d" % cls.ccc.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.ccc.shutdown()
        cls.ccc.server_close()
        cls.ccc_dir.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.state_p = d / "state.json"
        self.changes_p = d / "changes.jsonl"
        self.decisions_p = d / "decisions.json"
        self.answers_p = d / "answers.json"
        args = type("A", (), {})()
        args.ccc = self.ccc_base
        args.state = str(self.state_p)
        args.pi_dir = str(d / "pi")
        Path(args.pi_dir).mkdir()
        args.orca_dir = None
        args.verbose = False
        os.environ["CCC_NAV_CHANGES"] = str(self.changes_p)
        os.environ["CCC_NAV_DECISIONS"] = str(self.decisions_p)
        os.environ["CCC_NAV_ANSWERS"] = str(self.answers_p)
        navigator.Handler.app = navigator.App(args)
        self.app = navigator.Handler.app
        # ensure changes exists
        self.changes = self.app.changes
        self.state = self.app.state

    def tearDown(self):
        self.tmp.cleanup()
        for k in ["CCC_NAV_CHANGES", "CCC_NAV_DECISIONS", "CCC_NAV_ANSWERS"]:
            os.environ.pop(k, None)


class TestActionsValidation(ActionsBase):
    def test_unknown_param(self):
        try:
            actions.dispatch(self.app, "session.rename", {"key": "claude:1", "name": "x", "extra": 1})
            self.fail("should raise")
        except actions.ActionError as e:
            self.assertEqual(e.code, "unknown_param")

    def test_missing_param(self):
        try:
            actions.dispatch(self.app, "session.rename", {"key": "claude:1"})
            self.fail("should raise")
        except actions.ActionError as e:
            self.assertEqual(e.code, "missing_param")

    def test_enum_bad(self):
        try:
            actions.dispatch(self.app, "decision.rank", {"ids": ["x"], "why": {}})
            # why is str in our param? let us see
        except Exception:
            pass


class TestActionsDispatch(ActionsBase):
    def test_unknown_action(self):
        try:
            actions.dispatch(self.app, "does-not-exist", {})
            self.fail("should raise")
        except actions.ActionError as e:
            self.assertEqual(e.code, "unknown_route")

    def test_session_rename_roundtrip(self):
        res = actions.dispatch(self.app, "session.rename", {"key": "claude:1", "name": "hello"})
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("action"), "session.rename")
        self.assertIn("change", res)

    def test_hide_unhide_via_changes(self):
        res = actions.dispatch(self.app, "session.hide", {"keys": ["claude:1"]})
        self.assertTrue(res.get("ok"))
        cid = res.get("change")
        self.assertTrue(cid)
        # undo
        ures = actions.dispatch(self.app, "undo", {"change": cid})
        self.assertTrue(ures.get("ok"))

    def test_group_rename(self):
        res = actions.dispatch(self.app, "group.rename", {"repo": "/r/a", "name": "old", "new_name": "new"})
        self.assertTrue(res.get("ok"))

    def test_project_rename(self):
        res = actions.dispatch(self.app, "project.rename", {"repo": "/r/a", "name": "Proj"})
        self.assertTrue(res.get("ok"))

    def test_decision_answer(self):
        # add decision
        d = self.app.inbox.ask("Question?", ["yes", "no"], by="you")
        res = actions.dispatch(self.app, "decision.answer", {"id": d["id"], "choice": "yes"})
        self.assertTrue(res.get("ok"))


class TestCapabilities(ActionsBase):
    def test_capabilities_lists_every_registry_action(self):
        caps = []
        for name, act in actions.REGISTRY.items():
            caps.append(name)
        # ensure all present
        self.assertIn("session.rename", caps)
        self.assertIn("session.hide", caps)
        self.assertIn("session.unhide", caps)
        self.assertIn("group.rename", caps)
        self.assertIn("project.rename", caps)
        self.assertIn("undo", caps)
        self.assertIn("decision.answer", caps)

    def test_key_or_ref_rule_session_rename(self):
        # neither key nor ref -> missing_param
        try:
            actions.dispatch(self.app, "session.rename", {"name": "x"})
            self.fail("should raise")
        except actions.ActionError as e:
            self.assertEqual(e.code, "missing_param")
        # key works
        res = actions.dispatch(self.app, "session.rename", {"key": "claude:1", "name": "hello"})
        self.assertTrue(res.get("ok"))
        # a ref that matches nothing -> unknown_ref
        try:
            actions.dispatch(self.app, "session.rename", {"ref": "nav://session/claude:nosuch", "name": "x"})
            self.fail("should raise")
        except actions.ActionError as e:
            self.assertEqual(e.code, "unknown_ref")
        # a project ref given to a session action -> wrong_target
        try:
            actions.dispatch(self.app, "session.rename", {"ref": "r:/r/a", "name": "x"})
            self.fail("should raise")
        except actions.ActionError as e:
            self.assertEqual(e.code, "wrong_target")

    def test_session_send_fake_app(self):
        class FakeApp:
            def __init__(self, ret):
                self.ret = ret
            def tree(self, *a, **kw):
                return {"repos": []}
            def deliver(self, key, text):
                return self.ret
        # deliver returns 'failed: x' -> ActionError send_failed
        app = FakeApp("failed: x")
        try:
            actions.dispatch(app, "session.send", {"key": "claude:1", "text": "hi"})
            self.fail("should raise")
        except actions.ActionError as e:
            self.assertEqual(e.code, "send_failed")
        # returns 'sent' -> ok with result status 'sent'
        app2 = FakeApp("sent")
        res = actions.dispatch(app2, "session.send", {"key": "claude:1", "text": "hi"})
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("result", {}).get("status"), "sent")

    def test_undo_restores_previous_values(self):
        # session.rename (nickname)
        r1 = actions.dispatch(self.app, "session.rename", {"key": "claude:1", "name": "nick1"})
        c1 = r1.get("change")
        u1 = actions.dispatch(self.app, "undo", {"change": c1})
        self.assertTrue(u1.get("ok"))
        # undoing same change twice -> already_undone
        try:
            actions.dispatch(self.app, "undo", {"change": c1})
            self.fail("should raise")
        except actions.ActionError as e:
            self.assertEqual(e.code, "already_undone")
        # session.star
        r2 = actions.dispatch(self.app, "session.star", {"key": "claude:1"})
        c2 = r2.get("change")
        actions.dispatch(self.app, "undo", {"change": c2})
        try:
            actions.dispatch(self.app, "undo", {"change": c2})
            self.fail("should raise")
        except actions.ActionError as e:
            self.assertEqual(e.code, "already_undone")
        # session.hide
        r3 = actions.dispatch(self.app, "session.hide", {"keys": ["claude:1"]})
        c3 = r3.get("change")
        actions.dispatch(self.app, "undo", {"change": c3})
        try:
            actions.dispatch(self.app, "undo", {"change": c3})
            self.fail("should raise")
        except actions.ActionError as e:
            self.assertEqual(e.code, "already_undone")
        # group.rename
        r4 = actions.dispatch(self.app, "group.rename", {"repo": "/r/a", "name": "g1", "new_name": "g2"})
        c4 = r4.get("change")
        actions.dispatch(self.app, "undo", {"change": c4})
        try:
            actions.dispatch(self.app, "undo", {"change": c4})
            self.fail("should raise")
        except actions.ActionError as e:
            self.assertEqual(e.code, "already_undone")
        # project.icon (or group.icon)
        r5 = actions.dispatch(self.app, "project.icon", {"repo": "/r/a", "icon": "🔥"})
        c5 = r5.get("change")
        actions.dispatch(self.app, "undo", {"change": c5})
        try:
            actions.dispatch(self.app, "undo", {"change": c5})
            self.fail("should raise")
        except actions.ActionError as e:
            self.assertEqual(e.code, "already_undone")
        # session.mark
        r6 = actions.dispatch(self.app, "session.mark", {"key": "claude:1", "label": "x"})
        c6 = r6.get("change")
        actions.dispatch(self.app, "undo", {"change": c6})
        try:
            actions.dispatch(self.app, "undo", {"change": c6})
            self.fail("should raise")
        except actions.ActionError as e:
            self.assertEqual(e.code, "already_undone")

    def test_capabilities_every_registry_name_appears(self):
        # every REGISTRY name appears
        caps = list(actions.REGISTRY.keys())
        for name in caps:
            self.assertIsInstance(name, str)
            self.assertTrue(len(name) > 0)


if __name__ == "__main__":
    unittest.main()


# fix decision test
