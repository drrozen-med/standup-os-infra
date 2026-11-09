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

    def test_only_the_app_the_session_runs_in(self):
        with mock.patch.object(navigator, "desktop_links", return_value={"mac_url": "claude://claude.ai/epitaxy/local_abc12345"}):
            ids = [t["id"] for t in navigator.open_in_targets("claude:1", orca_workspace="x")]
        self.assertEqual(ids, ["claude"])                      # started in the Claude app, even inside an Orca folder
        with mock.patch.object(navigator, "desktop_links", return_value={}):
            ids = [t["id"] for t in navigator.open_in_targets("claude:1", orca_workspace="x")]
        self.assertEqual(ids, ["orca"])                        # a CLI session in an Orca terminal

    def test_codex_app_thread_vs_codex_cli(self):
        import tempfile
        sid = "01a16ef4-bfc1-79d0-a86b-131df7bd9b7c"
        with tempfile.TemporaryDirectory() as d:
            app, cli = Path(d) / "a.jsonl", Path(d) / "c.jsonl"
            app.write_text(json.dumps({"type": "session_meta", "payload": {"originator": "Codex Desktop"}}) + "\n")
            cli.write_text(json.dumps({"type": "session_meta", "payload": {"originator": "codex_exec"}}) + "\n")
            self.assertEqual([t["id"] for t in navigator.open_in_targets("codex:" + sid, "x", transcript=str(app))], ["codex"])
            self.assertEqual([t["id"] for t in navigator.open_in_targets("codex:" + sid, "x", transcript=str(cli))], ["orca"])
            self.assertEqual(navigator.open_in_targets("codex:" + sid, None, transcript=str(cli)), [])

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


class DecisionOpeners(unittest.TestCase):
    def app(self, owners=None, copilot=""):
        a = navigator.App.__new__(navigator.App)
        a.state = mock.Mock()
        a.state.snapshot.return_value = {"owners": owners or {}, "copilot": copilot}
        return a

    def test_asked_with_a_session(self):
        self.assertEqual(self.app().decision_session({"session": "codex:abc", "source": "cdl-bridge-owner"}), "codex:abc")

    def test_project_owner_is_found_by_its_source(self):
        a = self.app(owners={"/Users/u/projects/cdl-bridge": "claude:own1", "/Users/u/projects/boardsBridge": "claude:own2"})
        self.assertEqual(a.decision_session({"session": "", "source": "cdl-bridge-owner"}), "claude:own1")
        self.assertEqual(a.decision_session({"asked_by": "boardsbridge-owner"}), "claude:own2")   # case does not matter
        self.assertEqual(a.decision_session({"source": "shlav-bet-owner"}), "")

    OWNERS = {"/p/cdl-bridge": "claude:cdl", "/p/shlav-bet-family": "claude:shlav", "/p/airline": "claude:air",
              "/p/shared-marketing": "claude:mkt", "/p/nurse_job_interview": "claude:nbi", "/p/vera": "claude:vera"}

    def test_decision_without_a_session_opens_its_lane_owner(self):
        a = self.app(owners=self.OWNERS)
        self.assertEqual(a.decision_session({"lane": "cdl-bridge", "source": "cli"}), "claude:cdl")
        self.assertEqual(a.decision_session({"lane": "Nurse_Job_Interview", "source": "issue #30"}), "claude:nbi")
        self.assertEqual(a.decision_session({"lane": "shlav-bet", "source": "owner day plan"}), "claude:shlav")   # shorter lane name
        self.assertEqual(a.decision_session({"lane": "airlinebridge", "source": "AirlineBridge lane"}), "claude:air")

    def test_owner_path_in_the_source_beats_the_lane(self):
        a = self.app(owners=self.OWNERS)
        d = {"lane": "vera", "source": "~/.ccc-navigator/owners/cdl-bridge/uri-clicks-now.md"}
        self.assertEqual(a.decision_session(d), "claude:cdl")

    def test_project_named_in_the_source_when_the_lane_is_a_topic(self):
        a = self.app(owners=self.OWNERS)
        self.assertEqual(a.decision_session({"lane": "gtm-crm", "source": "shared-marketing owner r20; verified"}), "claude:mkt")
        self.assertEqual(a.decision_session({"lane": "platform-cost", "source": "vera day plan 2026-11-09 / PR #535"}), "claude:vera")

    def test_unknown_or_ambiguous_lane_stays_without_a_button(self):
        a = self.app(owners=self.OWNERS)
        self.assertEqual(a.decision_session({"lane": "aba-cdl-launch", "source": "ABA+CDL lane"}), "")
        self.assertEqual(a.decision_session({"lane": "", "source": "cli"}), "")
        self.assertEqual(a.decision_session({"lane": "sh", "source": ""}), "")                                  # too short to guess from
        two = self.app(owners={"/p/shlav-bet-family": "claude:a", "/p/shlav-bet-cloud": "claude:b"})
        self.assertEqual(two.decision_session({"lane": "shlav-bet", "source": ""}), "")                        # two candidates: do not guess

    def test_copilot_and_unknown_sources(self):
        a = self.app(copilot="claude:pilot")
        self.assertEqual(a.decision_session({"source": "copilot"}), "claude:pilot")
        self.assertEqual(a.decision_session({"source": "daily check 11-05"}), "")

    def test_cards_carry_the_buttons_row(self):
        self.assertIn('data-dopen="${esc(d.open_session)}"', HTML)
        self.assertIn("fillDecisionOpeners();", HTML)


if __name__ == "__main__":
    unittest.main()
