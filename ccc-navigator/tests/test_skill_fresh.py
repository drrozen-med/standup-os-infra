"""skill/SKILL.md must match what `nav.py skill --write` would write (issue #14, R4)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import actions
import nav


class SkillFresh(unittest.TestCase):
    def test_committed_skill_is_up_to_date(self):
        with open(nav.SKILL_PATH, encoding="utf-8") as f:
            committed = f.read()
        self.assertEqual(nav.skill_text(committed), committed,
                         "skill/SKILL.md is out of date: run python3 nav.py skill --write")

    def test_every_action_is_listed(self):
        with open(nav.SKILL_PATH, encoding="utf-8") as f:
            text = f.read()
        for name in actions.REGISTRY:
            self.assertIn("### `%s`" % name, text)

    def test_regenerating_keeps_the_hand_written_intro(self):
        text = nav.skill_text("intro line\n" + nav.SKILL_MARKER + "\nold generated stuff\n")
        self.assertTrue(text.startswith("intro line\n\n" + nav.SKILL_MARKER))
        self.assertNotIn("old generated stuff", text)


if __name__ == "__main__":
    unittest.main()
