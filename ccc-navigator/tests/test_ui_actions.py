import re
from pathlib import Path

import actions


def test_ui_actions_all_registered():
    html_path = Path(__file__).resolve().parent.parent / "static" / "index.html"
    html = html_path.read_text()
    names = re.findall(r"act\(\s*['\"]([^'\"]+)['\"]", html)
    assert len(names) >= 8
    for name in names:
        assert name in actions.REGISTRY, f"act({name}) not in actions.REGISTRY"
