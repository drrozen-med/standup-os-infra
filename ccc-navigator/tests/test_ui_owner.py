"""Static checks of the project-owner UI in static/index.html (docs/owners.md, "UI")."""
import re
import unittest
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text()


class OwnerUiTests(unittest.TestCase):
    def test_owner_button_markup(self):
        self.assertIn('data-owner-open="', HTML)
        self.assertIn('aria-label="Open owner"', HTML)
        self.assertIn("\U0001F9E0", HTML)  # brain
        # only rendered when the repo has an owner
        self.assertRegex(HTML, r"function ownerBtn\(repo\) \{\s*const ow = repo\.owner; if \(!ow\) return ''")
        # the summary is in the tooltip
        self.assertIn("(ow.summary || l.txt)", HTML)

    def test_summary_line_and_warning_states(self):
        self.assertIn('class="osum', HTML)
        self.assertRegex(HTML, r"\.osum\{[^}]*text-overflow:ellipsis[^}]*white-space:nowrap")
        self.assertIn("ow.stale", HTML)
        self.assertIn("ow.problem", HTML)
        self.assertIn("ow.state === 'blocked'", HTML)
        self.assertIn("ow.state === 'waiting-on-uri'", HTML)

    def test_owner_click_does_not_toggle_the_row(self):
        m = re.search(r"const ob = e\.target\.closest\('\[data-owner-open\]'\);\s*if \(ob\) \{([^}]*)\}", HTML)
        self.assertTrue(m, "owner click handler missing")
        self.assertIn("stopPropagation()", m.group(1))
        self.assertIn("openOwner(", m.group(1))
        # handled before the generic expand/collapse
        self.assertLess(HTML.index("closest('[data-owner-open]')"), HTML.index("closest('[data-toggle]')"))

    def test_owner_open_uses_the_copilot_open_path(self):
        # one shared function opens a session's chat in the main pane ...
        shared = re.search(r"function openChatSession\(key, what\) \{(.*?)\n  \}", HTML, re.S).group(1)
        self.assertIn("select(key, true)", shared)
        # ... and both the co-pilot and the owner call it
        self.assertRegex(HTML, r"function openCopilot\(\) \{[^}]*openChatSession\(cp\.key")
        self.assertRegex(HTML, r"(?s)function openOwner\(repoKey\) \{.*?openChatSession\(ow\.key")
        # the pinned co-pilot row (click and keyboard) uses openCopilot, no private copy
        self.assertGreaterEqual(HTML.count("openCopilot()"), 3)
        self.assertNotIn("select(cp.key, true)", HTML)

    def test_owner_menus_use_api_owner(self):
        self.assertIn("'/api/owner'", HTML)
        self.assertIn("'Make this the owner of ' + here.name", HTML)
        self.assertIn("'Stop being the owner'", HTML)
        self.assertIn("{ repo: here.key, key: mine ? '' : s.key }", HTML)
        self.assertIn("label: 'Open owner'", HTML)
        self.assertIn("label: 'Remove owner'", HTML)
        self.assertIn("{ repo: repo.key, key: '' }", HTML)


if __name__ == "__main__":
    unittest.main()
