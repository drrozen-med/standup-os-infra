import os
import sys
import tempfile
import time

os.environ["NAV_LLM"] = "off"   # tests never call the real model providers

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import briefs  # noqa: E402
import llm  # noqa: E402


class FakeClient:
    def __init__(self, items):
        self.items = items

    def _request(self, path):
        return {"repos": [{"key": "r", "name": "Proj", "groups": [{"name": "g", "items": self.items}]}]}


def test_llm_off_means_no_calls():
    assert llm.enabled() is False
    assert llm.complete("hi") is None


def test_strip_secrets_and_parse_json():
    assert "sk-" not in llm.strip_secrets("key sk-abcdefghijklmnopqrstu here")
    assert llm.parse_json('<think>x</think> {"title": "A B", "now": "c"}') == {"title": "A B", "now": "c"}
    assert llm.parse_json("no json") == {}


def test_pass_logs_state_changes_without_a_model():
    with tempfile.TemporaryDirectory() as d:
        db = briefs.connect(os.path.join(d, "b.db"))
        item = {"key": "claude:1", "sid": "1", "engine": "claude", "state": "resting", "triage": "idle", "mtime": 1.0}
        briefs.run_pass(FakeClient([item]), db=db, now=1000.0)
        assert briefs.session_brief("claude:1", db=db)["session"]["state"] == "idle"
        item.update(live=True, state="working")
        briefs.run_pass(FakeClient([item]), db=db, now=1100.0)
        ev = briefs.events_since(project="Proj", db=db)
        assert ev and ev[0]["kind"] == "status" and ev[0]["text"].startswith("working")
        assert briefs.project_brief("Proj", db=db)["sessions"][0]["key"] == "claude:1"


def test_summary_due_rules():
    row = {"summarised_at": 1000.0, "mtime": 50.0}
    assert briefs._due(None, {"mtime": 1}, 2000) is True
    assert briefs._due(row, {"mtime": 50.0}, 5000) is False          # nothing new
    assert briefs._due(row, {"mtime": 60.0}, 1100) is False          # too soon
    assert briefs._due(row, {"mtime": 60.0}, 1000 + briefs.SESSION_GAP) is True


def test_record_hook_logs_turn_and_session_end():
    with tempfile.TemporaryDirectory() as d:
        db = briefs.connect(os.path.join(d, "b.db"))
        sid = "cbb0b9ce-fa7e-4296-9709-721f0a411c68"
        assert briefs.record_hook(db, {"hook_event_name": "Stop", "session_id": sid, "cwd": "/x/Proj/"}) == "claude:" + sid
        assert briefs.record_hook(db, {"hook_event_name": "SessionEnd", "session_id": sid, "reason": "logout"})
        ev = db.execute("SELECT project, kind, text FROM events ORDER BY id").fetchall()
        assert [tuple(r) for r in ev] == [("Proj", "hook", "turn ended"), ("", "hook", "session ended (logout)")]


def test_record_hook_ignores_unknown_events_and_bad_ids():
    with tempfile.TemporaryDirectory() as d:
        db = briefs.connect(os.path.join(d, "b.db"))
        assert briefs.record_hook(db, {"hook_event_name": "PreToolUse", "session_id": "abcdef12"}) is None
        assert briefs.record_hook(db, {"hook_event_name": "Stop", "session_id": "../../etc"}) is None
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
