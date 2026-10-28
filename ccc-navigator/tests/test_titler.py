"""Tests for the standalone titler worker.

The navigator is replaced by a small fake HTTP server and the model is
replaced by a tiny script named through TITLER_CMD, so the tests never talk
to the real thing and never spend a token.
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import titler  # noqa: E402

FAKE_MODEL = '''#!/usr/bin/env python3
"""Fake titler model: dump the prompt, print a fixed title."""
import sys

prompt = sys.argv[-1]
if len(sys.argv) > 2:
    with open(sys.argv[-2], "w", encoding="utf-8") as fh:
        fh.write(prompt)
print("Fixture Alpha Title")
'''


def make_events(text="How do we fix the billing bug?", extra=1):
    """A transcript with a user message and `extra` assistant text messages."""
    events = [{"type": "user_text", "text": text}]
    for index in range(extra):
        events.append({
            "type": "assistant",
            "blocks": [
                {"kind": "thinking", "text": "internal monologue"},
                {"kind": "text", "text": "Answer %d: check the invoice code." % index},
                {"kind": "tool_use", "name": "Read", "detail": "{'path': 'x'}"},
            ],
        })
    return {"events": events}


def make_item(key, sid, **kwargs):
    item = {
        "key": key,
        "sid": sid,
        "engine": "claude",
        "nick": "",
        "hidden": False,
        "batch": 0,
        "mtime": 1_700_000_000,
        "original": "untitled",
    }
    item.update(kwargs)
    return item


def make_tree(items):
    return {"repos": [{"key": "/code/app", "name": "app", "groups": [{"name": "g", "items": items}]}]}


class FakeHandler(BaseHTTPRequestHandler):
    tree_payload = {"repos": []}
    transcripts = {}
    renames = []
    paths = []

    def log_message(self, *args):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        FakeHandler.paths.append(self.path)
        if parsed.path == "/api/tree":
            return self._send(200, FakeHandler.tree_payload)
        if parsed.path.startswith("/api/conversations/"):
            sid = urllib.parse.unquote(parsed.path[len("/api/conversations/"):])
            return self._send(200, FakeHandler.transcripts.get(sid, {"events": []}))
        if parsed.path == "/api/pi/transcript":
            sid = (urllib.parse.parse_qs(parsed.query).get("sid") or [""])[0]
            return self._send(200, FakeHandler.transcripts.get(sid, {"events": []}))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        if parsed.path == "/api/rename":
            FakeHandler.renames.append(body)
            return self._send(200, {"ok": True, "applied": ["navigator"]})
        return self._send(404, {"error": "not found"})


class FakeNavigator:
    """A navigator stub the titler can talk to."""

    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        host, port = self.server.server_address[:2]
        return "http://%s:%d" % (host, port)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class TitlerHelpers(unittest.TestCase):
    """Pure helpers: secret stripping, snippets, reply sanitizing."""

    def test_strip_secrets_removes_obvious_tokens(self):
        text = (
            "key sk-ABCDEFGHIJKLMNOPQRST and Bearer abc123XYZ_token-value "
            "deadbeef" * 4 + " " + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=" * 2
        )
        cleaned = titler.strip_secrets(text)
        self.assertNotIn("sk-ABCDEFGHIJKLMNOPQRST", cleaned)
        self.assertNotIn("abc123XYZ_token-value", cleaned)
        self.assertNotIn("deadbeef" * 4, cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_strip_secrets_keeps_ordinary_text(self):
        text = "Patient intake flow needs a retry on the billing webhook"
        self.assertEqual(titler.strip_secrets(text), text)

    def test_build_snippet_uses_first_user_and_last_three(self):
        messages = [
            ("user", "first user message " + "x" * 900),
            ("assistant", "answer one"),
            ("assistant", "answer two"),
            ("assistant", "answer three"),
            ("assistant", "answer four " + "y" * 700),
        ]
        snippet = titler.build_snippet(messages)
        self.assertIn("first user message", snippet)
        self.assertNotIn("answer one", snippet)          # only the last three
        self.assertIn("answer two", snippet)
        self.assertIn("answer four", snippet)
        self.assertLessEqual(len(snippet.split("USER (first message):\n")[1].split("\n\n")[0]), 700)
        self.assertLessEqual(len(snippet.split("ASSISTANT:\nanswer four ")[1]), 450)

    def test_build_snippet_strips_secrets(self):
        messages = [
            ("user", "use the key sk-ABCDEFGHIJKLMNOPQRST please"),
            ("assistant", "done"),
        ]
        snippet = titler.build_snippet(messages)
        self.assertNotIn("sk-ABCDEFGHIJKLMNOPQRST", snippet)

    def test_flatten_events_ignores_thinking_and_tools(self):
        events = [
            {"type": "user_text", "text": "hello"},
            {"type": "assistant", "blocks": [
                {"kind": "thinking", "text": "secret thoughts"},
                {"kind": "tool_use", "name": "Bash", "detail": "rm -rf /"},
                {"kind": "text", "text": "the visible answer"},
            ]},
            {"type": "system", "text": "started"},
        ]
        messages = titler.flatten_events(events)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0], ("user", "hello"))
        self.assertNotIn("secret thoughts", messages[1][1])
        self.assertNotIn("rm -rf /", messages[1][1])

    def test_sanitize_title_cleans_markdown_quotes_and_punctuation(self):
        self.assertEqual(titler.sanitize_title('**"Billing Retry Fix"**'), "Billing Retry Fix")
        self.assertEqual(titler.sanitize_title("- Invoice webhook debug."), "Invoice webhook debug")
        self.assertEqual(titler.sanitize_title("Title: Prescription Sync"), "Prescription Sync")
        self.assertEqual(titler.sanitize_title("  spaced   out  name  "), "spaced out name")

    def test_sanitize_title_rejects_unusable_replies(self):
        self.assertEqual(titler.sanitize_title(""), "")
        self.assertEqual(titler.sanitize_title("   \n  \n"), "")
        self.assertEqual(titler.sanitize_title("Error: model exploded"), "")
        self.assertEqual(titler.sanitize_title("one two three four five six seven eight"), "")

    def test_sanitize_title_truncates_long_titles(self):
        long_title = "Supercalifragilisticexpialidocious Extracordinarily Thorough"
        cleaned = titler.sanitize_title(long_title)
        self.assertLessEqual(len(cleaned), titler.TITLE_MAX_CHARS)
        self.assertTrue(cleaned)


class TitlerSelection(unittest.TestCase):
    """Which sessions may be renamed."""

    def setUp(self):
        self.state = {}

    def test_user_nickname_is_never_a_candidate(self):
        items = [make_item("claude:1", "s1", nick="My Own Name")]
        candidates, skipped = titler.select_candidates(items, self.state)
        self.assertEqual(candidates, [])
        self.assertEqual(skipped[0]["reason"], "user-titled")

    def test_hidden_and_batch_sessions_are_skipped(self):
        items = [
            make_item("claude:1", "s1", hidden=True),
            make_item("claude:2", "s2", batch=3),
        ]
        candidates, skipped = titler.select_candidates(items, self.state)
        self.assertEqual(candidates, [])
        self.assertEqual({entry["reason"] for entry in skipped}, {"hidden", "batch"})

    def test_already_titled_sessions_are_skipped_without_retitle(self):
        items = [make_item("claude:1", "s1", nick="Worker Title")]
        state = {"claude:1": {"title": "Worker Title", "at": "2026-01-01T00:00:00+00:00", "events": 10}}
        candidates, skipped = titler.select_candidates(items, state)
        self.assertEqual(candidates, [])
        self.assertEqual(skipped[0]["reason"], "already-titled")

    def test_user_renamed_session_is_skipped_even_with_retitle(self):
        items = [make_item("claude:1", "s1", nick="User Renamed It")]
        state = {"claude:1": {"title": "Worker Title", "at": "2026-01-01T00:00:00+00:00", "events": 10}}
        candidates, skipped = titler.select_candidates(items, state, retitle=True)
        self.assertEqual(candidates, [])
        self.assertEqual(skipped[0]["reason"], "user-renamed")

    def test_retitle_allows_worker_titled_sessions(self):
        items = [make_item("claude:1", "s1", nick="Worker Title")]
        state = {"claude:1": {"title": "Worker Title", "at": "2026-01-01T00:00:00+00:00", "events": 10}}
        candidates, _ = titler.select_candidates(items, state, retitle=True)
        self.assertEqual([entry["key"] for entry in candidates], ["claude:1"])

    def test_candidates_are_newest_first(self):
        items = [
            make_item("claude:1", "s1", mtime=100),
            make_item("claude:2", "s2", mtime=300),
            make_item("claude:3", "s3", mtime=200),
        ]
        candidates, _ = titler.select_candidates(items, self.state)
        self.assertEqual([entry["key"] for entry in candidates], ["claude:2", "claude:3", "claude:1"])

    def test_growth_rule(self):
        self.assertTrue(titler.grew_enough({"events": 10}, 14))
        self.assertFalse(titler.grew_enough({"events": 10}, 13))
        self.assertTrue(titler.grew_enough({}, 1))


class TitlerCycle(unittest.TestCase):
    """End to end against the fake navigator and the fake model."""

    def setUp(self):
        FakeHandler.tree_payload = make_tree([])
        FakeHandler.transcripts = {}
        FakeHandler.renames = []
        FakeHandler.paths = []
        self.nav = FakeNavigator()
        self.tmp = tempfile.mkdtemp(prefix="titler-test-")
        self.state_file = os.path.join(self.tmp, "titler.json")
        self.prompt_dump = os.path.join(self.tmp, "prompt.txt")
        script = os.path.join(self.tmp, "fake_model.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(FAKE_MODEL)
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
        self.cmd = "%s %s %s" % (shutil.which("python3") or "python3", script, self.prompt_dump)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.nav.stop)
        # the CLI path reads the environment, so point it at the fake model and
        # a throwaway state file for the duration of the test
        saved = {key: os.environ.get(key) for key in ("TITLER_CMD", "TITLER_STATE")}
        os.environ["TITLER_CMD"] = self.cmd
        os.environ["TITLER_STATE"] = self.state_file

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        self.items = []

    def options(self, **kwargs):
        kwargs.setdefault("url", self.nav.url)
        kwargs.setdefault("state_file", self.state_file)
        kwargs.setdefault("cmd", self.cmd)
        return titler.Options(**kwargs)

    def add_session(self, key, sid, events=None, **kwargs):
        FakeHandler.transcripts[sid] = events if events is not None else make_events()
        self.items.append(make_item(key, sid, **kwargs))
        FakeHandler.tree_payload = make_tree(list(self.items))

    def test_renames_visible_session_and_records_state(self):
        self.add_session("claude:1", "s1")
        summary = titler.run_cycle(self.options())
        self.assertEqual(summary["renamed"], 1)
        self.assertEqual(len(FakeHandler.renames), 1)
        payload = FakeHandler.renames[0]
        self.assertEqual(payload["kind"], "session")
        self.assertEqual(payload["key"], "claude:1")
        self.assertEqual(payload["sid"], "s1")
        self.assertEqual(payload["engine"], "claude")
        self.assertEqual(payload["name"], "Fixture Alpha Title")
        state = json.loads(Path(self.state_file).read_text())
        self.assertEqual(state["claude:1"]["title"], "Fixture Alpha Title")
        self.assertEqual(state["claude:1"]["events"], 2)

    def test_tree_request_uses_the_seven_day_window(self):
        self.add_session("claude:1", "s1")
        titler.run_cycle(self.options())
        self.assertIn("/api/tree?window=7d", FakeHandler.paths)

    def test_second_run_skips_the_session_it_already_titled(self):
        self.add_session("claude:1", "s1")
        titler.run_cycle(self.options())
        FakeHandler.renames = []
        summary = titler.run_cycle(self.options())
        self.assertEqual(summary["renamed"], 0)
        self.assertEqual(FakeHandler.renames, [])
        self.assertEqual(summary["skipped"][0]["reason"], "already-titled")

    def test_user_nickname_is_never_overwritten(self):
        self.add_session("claude:1", "s1", nick="Typed By Hand")
        summary = titler.run_cycle(self.options())
        self.assertEqual(summary["renamed"], 0)
        self.assertEqual(FakeHandler.renames, [])

    def test_hidden_session_is_never_touched(self):
        self.add_session("claude:1", "s1", hidden=True)
        summary = titler.run_cycle(self.options())
        self.assertEqual(summary["renamed"], 0)
        self.assertEqual(FakeHandler.renames, [])
        self.assertEqual(summary["skipped"][0]["reason"], "hidden")

    def test_dry_run_changes_nothing(self):
        self.add_session("claude:1", "s1")
        summary = titler.run_cycle(self.options(dry_run=True))
        self.assertEqual(summary["renamed"], 0)
        self.assertEqual(summary["proposed"], 1)
        self.assertEqual(summary["proposals"][0]["title"], "Fixture Alpha Title")
        self.assertEqual(FakeHandler.renames, [])
        self.assertFalse(os.path.exists(self.state_file))

    def test_max_limits_the_number_of_renames(self):
        for index in range(4):
            self.add_session("claude:%d" % index, "s%d" % index)
        summary = titler.run_cycle(self.options(max_sessions=2))
        self.assertEqual(summary["renamed"], 2)
        self.assertEqual(len(FakeHandler.renames), 2)

    def test_session_with_too_few_messages_is_skipped(self):
        self.add_session("claude:1", "s1", events={"events": [{"type": "user_text", "text": "hi"}]})
        summary = titler.run_cycle(self.options())
        self.assertEqual(summary["renamed"], 0)
        self.assertEqual(summary["results"][0]["reason"], "too-few-messages")

    def test_secrets_never_leave_the_machine(self):
        secret = "sk-ABCDEFGHIJKLMNOPQRSTUV"
        self.add_session("claude:1", "s1", events=make_events(text="the key is " + secret))
        titler.run_cycle(self.options())
        prompt = Path(self.prompt_dump).read_text()
        self.assertNotIn(secret, prompt)
        self.assertIn("[REDACTED]", prompt)
        self.assertIn(titler.PROMPT, prompt)

    def test_pi_session_uses_the_pi_transcript_endpoint(self):
        self.add_session("pi:1", "p1", engine="pi")
        summary = titler.run_cycle(self.options())
        self.assertEqual(summary["renamed"], 1)
        self.assertTrue(any(path.startswith("/api/pi/transcript?sid=p1") for path in FakeHandler.paths))

    def test_retitle_needs_the_transcript_to_grow(self):
        self.add_session("claude:1", "s1")
        titler.run_cycle(self.options())
        state = json.loads(Path(self.state_file).read_text())
        state["claude:1"]["events"] = 100
        Path(self.state_file).write_text(json.dumps(state))
        FakeHandler.renames = []
        summary = titler.run_cycle(self.options(retitle=True))
        self.assertEqual(summary["renamed"], 0)
        self.assertEqual(summary["results"][0]["reason"], "not-grown")
        self.assertEqual(FakeHandler.renames, [])

    def test_retitle_renames_when_the_transcript_grew(self):
        self.add_session("claude:1", "s1")
        titler.run_cycle(self.options())
        state = json.loads(Path(self.state_file).read_text())
        state["claude:1"]["events"] = 1
        Path(self.state_file).write_text(json.dumps(state))
        FakeHandler.renames = []
        summary = titler.run_cycle(self.options(retitle=True))
        self.assertEqual(summary["renamed"], 1)
        self.assertEqual(len(FakeHandler.renames), 1)

    def test_unreachable_navigator_is_reported_not_fatal(self):
        options = self.options(url="http://127.0.0.1:1")
        summary = titler.run_cycle(options)
        self.assertTrue(summary["unreachable"])
        self.assertEqual(summary["renamed"], 0)

    def test_cli_dry_run_prints_one_json_line(self):
        import contextlib
        import io

        self.add_session("claude:1", "s1")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = titler.main([
                "--dry-run", "--max", "2", "--url", self.nav.url,
            ])
        self.assertEqual(code, 0)
        lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        summary = json.loads(lines[0])
        self.assertEqual(summary["proposed"], 1)
        self.assertTrue(summary["dry_run"])
        self.assertEqual(FakeHandler.renames, [])


class PrivacyDefaultsTest(unittest.TestCase):
    """Nothing leaves the machine unless the owner opts in."""

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in ("TITLER_CMD", "TITLER_ALLOW_EXTERNAL")}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_external_model_is_off_by_default(self):
        self.assertFalse(titler.external_allowed())

    def test_explicit_opt_in_or_command_enables_it(self):
        os.environ["TITLER_ALLOW_EXTERNAL"] = "1"
        self.assertTrue(titler.external_allowed())
        del os.environ["TITLER_ALLOW_EXTERNAL"]
        self.assertTrue(titler.external_allowed("python3 stub.py"))

    def test_default_run_never_calls_the_model(self):
        calls = []
        original = titler.call_model
        titler.call_model = lambda *a, **k: calls.append(a) or "Should Not Happen"
        try:
            client = type("C", (), {"transcript": lambda self, e, s: {"events": [
                {"type": "user", "text": "Fix the login page for the airline demo"},
                {"type": "assistant", "text": "On it."}]}})()
            opts = titler.Options(dry_run=True)
            result = titler.title_candidate(client, {"key": "k", "item": {"sid": "s", "engine": "e"}, "prior": None}, opts, {})
        finally:
            titler.call_model = original
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["title"], "Fix Login Page Airline Demo")

    def test_has_real_title(self):
        self.assertTrue(titler.has_real_title({"original": "Vera Groq API key audit"}))
        self.assertFalse(titler.has_real_title({"original": "(untitled 01a16f3a)"}))
        self.assertFalse(titler.has_real_title({"original": "You are a delegated engineer"}))
        self.assertFalse(titler.has_real_title({"original": "Please look at the failing build on staging and tell me w…"}))

    def test_local_title_skips_slash_commands(self):
        msgs = [("user", "/model claude-sonnet-5-5"), ("user", "Curate the efforts register for Devin")]
        self.assertEqual(titler.local_title(msgs), "Curate Efforts Register Devin")
        self.assertEqual(titler.local_title([("user", "/model claude-sonnet-5-5")]), "")

    def test_local_title_needs_real_words(self):
        self.assertEqual(titler.local_title([("user", "ok")]), "")

    def test_personal_data_is_redacted(self):
        text = "Mail dr.who@example.com, call +972 54-123-4567, see https://x.example/secret?id=1 id 123456789"
        cleaned = titler.strip_secrets(text)
        for leaked in ("dr.who@example.com", "054", "4567", "https://", "123456789"):
            self.assertNotIn(leaked, cleaned)


if __name__ == "__main__":
    unittest.main()
