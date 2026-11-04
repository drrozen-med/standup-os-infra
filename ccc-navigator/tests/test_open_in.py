"""Open-in buttons: where a session can be shown on this Mac (Claude app, Codex, Orca)."""
import json
import re
import unittest
from pathlib import Path
from unittest import mock

import navigator

HTML = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text()


class OpenInTargets(unittest.TestCase):
    def test_claude_session_gets_the_mac_app_link(self):
        with mock.patch.object(navigator, "desktop_links", return_value={"mac_url": "claude://claude.ai/epitaxy/local_abc12345", "url": "https://claude.ai/code/session_X"}):
            out = navigator.open_in_targets("claude:123")
        self.assertEqual(out, [{"id": "claude", "label": "Claude", "url": "claude://claude.ai/epitaxy/local_abc12345"}])

    def test_claude_session_unknown_to_the_desktop_app_gets_nothing(self):
        with mock.patch.object(navigator, "desktop_links", return_value={}):
            self.assertEqual(navigator.open_in_targets("claude:123"), [])

    def test_codex_thread_link(self):
        out = navigator.open_in_targets("codex:01a16ef4-bfc1-79d0-a86b-131df7bd9b7c")
        self.assertEqual(out, [{"id": "codex", "label": "Codex", "url": "codex://threads/01a16ef4-bfc1-79d0-a86b-131df7bd9b7c"}])

    def test_codex_id_with_odd_characters_is_refused(self):
        self.assertEqual(navigator.open_in_targets("codex:abc/../../x"), [])
        self.assertEqual(navigator.open_in_targets("codex:"), [])

    def test_orca_is_launch_only_with_the_workspace_as_a_note(self):
        out = navigator.open_in_targets("devin:xyz", orca_workspace="vera-wt-ci")
        self.assertEqual(out, [{"id": "orca", "label": "Orca", "note": "vera-wt-ci"}])
        self.assertNotIn("url", out[0])

    def test_claude_session_in_orca_gets_both(self):
        with mock.patch.object(navigator, "desktop_links", return_value={"mac_url": "claude://claude.ai/epitaxy/local_abc12345"}):
            ids = [t["id"] for t in navigator.open_in_targets("claude:1", orca_workspace="x")]
        self.assertEqual(ids, ["claude", "orca"])

    def test_away_from_the_mac_only_the_web_link_is_offered(self):
        links = {"mac_url": "claude://claude.ai/epitaxy/local_abc12345", "url": "https://claude.ai/code/session_X"}
        with mock.patch.object(navigator, "desktop_links", return_value=links):
            out = navigator.open_in_targets("claude:1", orca_workspace="x", remote=True)
        self.assertEqual(out, [{"id": "claude", "label": "Claude", "url": "https://claude.ai/code/session_X"}])
        self.assertEqual(navigator.open_in_targets("codex:01a16ef4-bfc1", remote=True), [])

    def test_open_orca_reports_a_missing_app(self):
        with mock.patch.object(navigator.subprocess, "run"), \
                mock.patch.object(navigator.os.path, "exists", return_value=False), \
                mock.patch.object(navigator.shutil, "which", return_value=None):
            res = navigator.open_orca()
        self.assertFalse(res["ok"])
        self.assertIn("not installed", res["error"])

    def test_open_orca_without_a_worktree_just_opens_the_app(self):
        done = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(navigator.os.path, "exists", return_value=True), \
                mock.patch.object(navigator.subprocess, "run", return_value=done) as run:
            res = navigator.open_orca()
        self.assertEqual((res["ok"], res["how"]), (True, "app"))
        self.assertIn(["/usr/local/bin/orca", "open"], [c[0][0] for c in run.call_args_list])

    def test_open_orca_switches_to_the_terminal_of_the_session(self):
        listing = json.dumps({"result": {"terminals": [
            {"handle": "t_old", "agentIdentity": "devin", "title": "Devin", "preview": "unrelated", "lastOutputAt": 1},
            {"handle": "t_hit", "agentIdentity": "devin", "title": "Devin ready", "preview": "invitation links production domain", "lastOutputAt": 0},
            {"handle": "t_shell", "title": "zsh", "preview": "", "lastOutputAt": 9}]}})
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ["open", "-a"]:
                return mock.Mock(returncode=0, stdout="", stderr="")
            out = listing if cmd[1:3] == ["terminal", "list"] else "{}"
            return mock.Mock(returncode=0, stdout=out, stderr="")
        with mock.patch.object(navigator.os.path, "exists", return_value=True), \
                mock.patch.object(navigator.subprocess, "run", side_effect=fake_run):
            res = navigator.open_orca("/w/shlav", "devin", "check the invitation links on the production domain")
        self.assertEqual((res["ok"], res["how"]), (True, "terminal"))
        switch = [c for c in calls if c[1:3] == ["terminal", "switch"]]
        self.assertEqual(switch[0][switch[0].index("--terminal") + 1], "t_hit")
        self.assertIn("path:/w/shlav", calls[1])

    def test_pick_terminal_prefers_the_same_agent_then_recent_output(self):
        terms = [{"handle": "a", "agentIdentity": "claude", "lastOutputAt": 5},
                 {"handle": "b", "agentIdentity": "devin", "lastOutputAt": 1},
                 {"handle": "c", "agentIdentity": "devin", "lastOutputAt": 3}]
        self.assertEqual(navigator.pick_orca_terminal(terms, "devin", "")["handle"], "c")
        self.assertEqual(navigator.pick_orca_terminal(terms, "claude", "")["handle"], "a")
        self.assertIsNone(navigator.pick_orca_terminal([], "devin", ""))

    def test_pick_terminal_skips_orphaned_terminals(self):
        terms = [{"handle": "x", "agentIdentity": "devin", "orphaned": True, "lastOutputAt": 9},
                 {"handle": "y", "agentIdentity": "devin", "lastOutputAt": 1}]
        self.assertEqual(navigator.pick_orca_terminal(terms, "devin", "")["handle"], "y")


class OpenInUi(unittest.TestCase):
    def test_button_row_and_handlers(self):
        self.assertIn("/api/open-in?session=", HTML)
        self.assertIn("data-open-in", HTML)
        # the buttons go in the same row as Rename and Organize
        self.assertRegex(HTML, r"const row = \$\('#briefLine'\);")
        # deep links open the app, Orca is launched through the server
        self.assertIn("location.href = tg.url", HTML)
        self.assertIn("method: 'POST'", HTML[HTML.index("data-open-in"):])

    def test_post_path_is_routed_not_proxied_to_ccc(self):
        self.assertIn("/api/open-in", navigator.Handler.NAV_POST)

    def test_post_is_refused_away_from_the_mac(self):
        src = Path(navigator.__file__).read_text()
        m = re.search(r'elif p == "/api/open-in":\s*if self\._rem:\s*return self\._send\(403', src)
        self.assertTrue(m, "POST /api/open-in must be Mac-only")


if __name__ == "__main__":
    unittest.main()
