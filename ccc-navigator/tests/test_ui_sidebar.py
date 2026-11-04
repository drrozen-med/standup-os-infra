"""Static checks of the compact sidebar header in static/index.html: one row of pills, View beside the search box."""
import re
import unittest
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text()


class SidebarLayoutTests(unittest.TestCase):
    def test_pinned_pills_are_one_row_on_desktop(self):
        css = re.search(r"#pinned\{([^}]*)\}", HTML).group(1)
        self.assertIn("flex-direction:row", css)
        # the second line of each pill is not drawn, it lives in the tooltip
        self.assertIn(".pin-row .ps{display:none}", HTML)
        for who in ("Co-pilot", "Decisions", "Supervisor", "Ride"):
            self.assertIn(who, HTML)
        self.assertRegex(HTML, r'id="decRow"[^>]*title="Decisions:')
        self.assertRegex(HTML, r'href="/ride" title="Ride mode:')

    def test_view_sits_beside_the_search_box(self):
        self.assertTrue(re.search(r'<div class="searchrow"><label class="search">.*?</label><span id="viewHost"></span></div>', HTML, re.S))
        # desktop: View goes into the search row; phones keep it in the chips row
        self.assertIn("host.innerHTML = phoneWidth() ? '' : VIEW_BTN", HTML)
        self.assertIn("(phoneWidth() ? VIEW_BTN : '')", HTML)
        self.assertIn("addEventListener('change', () => { if (tree) renderChips(); })", HTML)

    def test_phone_layout_keeps_search_hidden_until_used(self):
        media = HTML[HTML.index("@media (max-width: 760px)"):]
        self.assertIn(".searchrow{display:contents}#viewHost{display:none}", media)
        self.assertIn(".search{display:none}", media)

    def test_view_button_click_is_still_delegated(self):
        # the handler is bound on the document, so it works wherever the button lives
        self.assertIn("e.target.closest('#viewBtn')", HTML)


if __name__ == "__main__":
    unittest.main()
