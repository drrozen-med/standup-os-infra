"""Static checks of the supervisor status board in static/index.html (docs/supervisor.md, "UI: the status board")."""
import re
import unittest
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text()


class SupervisorUiTests(unittest.TestCase):
    def test_pinned_row_after_decisions_opens_the_board(self):
        self.assertIn('id="supRow"', HTML)
        self.assertIn(">Supervisor</span>", HTML)
        # rendered after the Decisions row, before Ride mode
        self.assertLess(HTML.index('id="decRow"'), HTML.index('id="supRow"'))
        self.assertLess(HTML.index('id="supRow"'), HTML.index('href="/ride"'))
        # click and keyboard both open it, like Decisions
        self.assertIn("if (e.target.closest('#supRow')) return openSupervisor();", HTML)
        self.assertIn("if (row.id === 'supRow') return openSupervisor();", HTML)
        # one-line summary: owners, how many need a push, last cycle
        for frag in ("' owner'", "a push'", "'last cycle '"):
            self.assertIn(frag, HTML)

    def test_board_screen_replaces_the_main_pane_like_decisions(self):
        self.assertRegex(HTML, r"(?s)function openSupervisor\(\) \{\s*ui\.sup = true; ui\.inbox = false;")
        self.assertIn("if (!hit && ui.sup) { main.innerHTML = supHtml(); return; }", HTML)
        # opening anything else leaves the board
        self.assertRegex(HTML, r"ui\.inbox = true; ui\.today = false; ui\.sup = false;")
        self.assertRegex(HTML, r"ui\.inbox = false; ui\.today = false; ui\.sup = false;")
        self.assertIn("ui.today = true; ui.inbox = false; ui.sup = false;", HTML)

    def test_get_supervisor_rides_the_refresh_cycle(self):
        self.assertIn("fetch('/api/supervisor')", HTML)
        refresh = re.search(r"async function refresh\(force\) \{(.*?)\n  \}", HTML, re.S).group(1)
        self.assertIn("loadSupervisor()", refresh)
        # throttled when the board is closed; no new timer of its own
        self.assertIn("now - supAt < 30000", HTML)
        loader = re.search(r"async function loadSupervisor\(force\) \{(.*?)\n  \}", HTML, re.S).group(1)
        self.assertNotIn("setInterval", loader)
        self.assertEqual(HTML.count("setInterval("), 2)  # the two that existed before this feature

    def test_post_supervisor_actions(self):
        self.assertIn("fetch('/api/supervisor', { method: 'POST'", HTML)
        self.assertIn("data-sup-act=", HTML)  # header Pause / Resume
        self.assertIn("supPost({ action: a }", HTML)
        self.assertIn("supPost({ action: 'wake', project: proj }", HTML)
        self.assertIn("label: 'Wake now'", HTML)
        self.assertIn("label: 'Pause this project'", HTML)
        self.assertIn("label: 'Resume this project'", HTML)
        self.assertIn("supPost({ action: 'pause', project: proj }", HTML)
        self.assertIn("supPost({ action: 'resume', project: proj }", HTML)

    def test_night_stop_feedback_and_engine_controls(self):
        self.assertIn("data-sup-night=", HTML)      # header: night work on/off
        self.assertIn("supPost({ action: 'night', on }", HTML)
        self.assertIn("data-sup-stop", HTML)         # header: stop all, after a confirmation in the page's own dialog
        fn = re.search(r"async function supStop\(\) \{(.*?)\n  \}", HTML, re.S).group(1)
        self.assertIn("askConfirm(", fn)
        self.assertIn("supPost({ action: 'stop' }", fn)
        fn = re.search(r"async function supFeedback\(proj, name\) \{(.*?)\n  \}", HTML, re.S).group(1)
        self.assertIn("askText(", fn)
        self.assertIn("supPost({ action: 'feedback', project: proj, text: text.trim() }", fn)
        self.assertIn("label: 'Give feedback…'", HTML)
        self.assertIn("supPost({ action: 'engine', project: proj, engine: 'jobs' }", HTML)
        self.assertIn("supPost({ action: 'engine', project: proj, engine: 'session' }", HTML)
        self.assertNotIn("prompt(", HTML.replace("prompt()/confirm()", ""))      # blocked in embedded browsers

    def test_the_alarm_opens_the_morning_report(self):
        self.assertIn("l.kind === 'report'", HTML)                   # the alarm's Open button and the notification click
        fn = re.search(r"async function openAlert\(a\) \{(.*?)\n  \}", HTML, re.S).group(1)
        branch = fn[fn.index("l.kind === 'report'"):]
        self.assertIn("ackAlert(a.id)", branch[:200])                 # opening it counts as attending to it
        self.assertIn("openReport(l.ref)", branch[:200])
        self.assertIn("'Open report'", HTML)
        self.assertIn("fetch('/api/report'", HTML)
        # a screen of its own inside the supervisor area: back to the board, not re-rendered every minute
        self.assertIn("data-rep-back", HTML)
        self.assertIn("if (inReport()) return repHtml();", HTML)
        self.assertIn("if (ui.sup && !inReport()) lastDetailKey = null;", HTML)
        self.assertRegex(HTML, r"(?s)function openSupervisor\(\) \{\s*ui\.sup = true; ui\.inbox = false; ui\.report = null;")
        # and a way back to it after the alarm is gone
        self.assertIn("data-rep-open", HTML)
        self.assertIn("return openReport('')", HTML)
        # the text is escaped before any markup is added
        body = re.search(r"const repInline = (.*?);\n", HTML).group(1)
        self.assertTrue(body.startswith("s => esc(s)"))

    def test_jobs_show_on_the_board(self):
        for frag in ("data-sup-jobs", "new turns are held: ", "jb.running", "x.cooling ? ' (resting)'", "data-sup-turn",
                     "turn running ", "ended without a status", "'turns-failing': 'every model failed'", "holding: Mac busy"):
            self.assertIn(frag, HTML)
        self.assertIn(".sup-acts{margin-inline-start:auto", HTML)

    def test_brain_reuses_open_owner(self):
        fn = re.search(r"function openSupOwner\(proj\) \{(.*?)\n  \}", HTML, re.S).group(1)
        self.assertIn("openOwner(repo.key)", fn)
        self.assertIn("openChatSession(row.owner_key", fn)  # owner not in the tree yet
        self.assertIn("\U0001F9E0</span>", HTML)
        self.assertIn("data-sup-owner=", HTML)
        # no private copy of the open-the-chat logic
        self.assertNotIn("select(row.owner_key", HTML)

    def test_row_content(self):
        for frag in ("days_since_shipped", "decisions_open", "oldest_decision_days", "last_wake", "r.flags", "sup-flag",
                     "shipped today", "decisions open", "not woken yet", "data-sup-sum"):
            self.assertIn(frag, HTML)
        self.assertIn("No owners yet. Make a session the owner of a project from its menu.", HTML)
        self.assertIn("pi.all ? 'Paused' : 'Running'", HTML)

    def test_summary_is_ellipsized_and_phone_stacks(self):
        self.assertRegex(HTML, r"\.sup-sum\{[^}]*text-overflow:ellipsis[^}]*white-space:nowrap")
        # rows stack (name + buttons, then summary, then meta) in a narrow pane: phones and half-width windows
        narrow = HTML[HTML.index("@container supb (max-width:720px)"):]
        narrow = narrow[:narrow.index("\n}\n")]
        self.assertIn('grid-template-areas:"dot name brain menu" "sum sum sum sum"', narrow)
        self.assertIn("-webkit-line-clamp:2", narrow)
        self.assertIn("container:supb/inline-size", HTML)


if __name__ == "__main__":
    unittest.main()
